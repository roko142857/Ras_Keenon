"""Latest-input actuators. No enable, readiness, source selection or epoch gating."""
import math
import threading
import time
from collections import OrderedDict

from .channel import DirectChannel
from .rosbridge import Rosbridge

KEYS = frozenset('wsadhlikrf')


class KeyLatch:
    def __init__(self, timeout=.3):
        self.timeout = timeout
        self.lock = threading.Lock()
        self.pressed = frozenset()
        self.received = -math.inf
        self.sequences = OrderedDict()

    def update(self, keys, client, sequence, now):
        if not isinstance(keys, list) or any(k not in KEYS for k in keys):
            raise ValueError('invalid key list')
        if not isinstance(client, str) or not client or len(client) > 100 or type(sequence) is not int:
            raise ValueError('invalid input identity')
        with self.lock:
            if sequence <= self.sequences.get(client, -1): return False, False
            self.sequences[client] = sequence
            self.sequences.move_to_end(client)
            while len(self.sequences) > 64: self.sequences.popitem(last=False)
            value = frozenset(keys)
            changed = value != self.pressed
            self.pressed, self.received = value, now
            return True, changed

    def clear(self):
        with self.lock: self.pressed = frozenset()

    def read(self, now):
        with self.lock:
            if now-self.received > self.timeout: self.pressed = frozenset()
            return self.pressed


class Ticket:
    def __init__(self, instruction):
        self.instruction = instruction
        self.done, self.cancel = threading.Event(), threading.Event()
        self.success, self.result, self.message = False, {}, ''
        self.finish_lock = threading.Lock()

    def finish(self, success, message, result=None):
        with self.finish_lock:
            if not self.done.is_set():
                self.success, self.message, self.result = success, message, result or {}
                self.done.set()


class DirectBase:
    def __init__(self, hardware, period=.02, host='192.168.64.20', port=9091, timeout=.25):
        self.hardware, self.period = hardware, period
        self.timeout = timeout
        self.host, self.port = host, port
        self.target, self.sent = (0., 0.), (0., 0.)
        self.updated = time.monotonic()
        self.until = None
        self.zero_tail = 0.
        self.error = ''
        self.lock = threading.Lock()
        self.wake, self.done = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def set(self, x=0., yaw=0., force=False, until=None):
        with self.lock:
            previous = self.target
            self.target, self.updated, self.until = (float(x), float(yaw)), time.monotonic(), until
            if self.target == (0., 0.) and (previous != self.target or force):
                self.zero_tail = time.monotonic()+.2
            changed = previous != self.target or force
        if changed: self.wake.set()

    def run(self):
        ws = None
        try:
            if self.hardware:
                ws = Rosbridge(self.host, self.port)
                ws.sock.settimeout(.001)
                ws.send({'op': 'advertise', 'topic': '/cmd_vel', 'type': 'geometry_msgs/Twist',
                         'queue_size': 1, 'latch': False})
            while not self.done.is_set() or time.monotonic() < self.zero_tail:
                if ws: ws.receive()  # Drain protocol traffic; no robot-state admission checks.
                with self.lock:
                    now = time.monotonic()
                    # A ticket's absolute expiry outlives a stalled cycle(): even if no new
                    # set() arrives, this thread zeroes at the deadline it was given.
                    expiry = self.updated+self.timeout
                    if self.until is not None: expiry = min(expiry, self.until)
                    x, yaw = self.target if now <= expiry and not self.done.is_set() else (0., 0.)
                    transmit = (x, yaw) != (0., 0.) or now < self.zero_tail or self.sent != (0., 0.)
                    if transmit:
                        # Snapshot, publish and sent-update share the lock: a stop's set(0)
                        # cannot finish between choosing a nonzero value and sending it.
                        # The socket's 1 ms send timeout bounds the hold; an error leaves
                        # this block and releases the lock before the run() cleanup.
                        if ws:
                            ws.send({'op': 'publish', 'topic': '/cmd_vel', 'msg': {
                                'linear': {'x': x, 'y': 0., 'z': 0.},
                                'angular': {'x': 0., 'y': 0., 'z': yaw}}})
                        self.sent = (x, yaw)
                self.wake.wait(self.period)
                self.wake.clear()
        except Exception as exc:
            self.error = str(exc)
        finally:
            if ws:
                try:
                    ws.send({'op': 'publish', 'topic': '/cmd_vel', 'msg': {
                        'linear': {'x': 0., 'y': 0., 'z': 0.}, 'angular': {'x': 0., 'y': 0., 'z': 0.}}})
                except OSError: pass
                ws.close()
            self.sent = (0., 0.)

    def close(self):
        self.set(force=True)
        self.done.set(); self.wake.set(); self.thread.join(timeout=3)


class DirectBody:
    def __init__(self, hardware, executable, keys, period=.05, head_rate=5., squat_height=.03,
                 lock_fds=()):
        self.hardware, self.executable, self.keys = hardware, executable, keys
        self.period, self.head_rate, self.squat_height = period, head_rate, squat_height
        self.lock_fds = tuple(lock_fds)
        self.lock = threading.Lock()
        self.pending, self.active = None, None
        self.generation, self.direction = 0, -1
        self.error = ''
        self.sample_at = None
        self.data = dict(yaw_deg=-4., pitch_deg=-.13, right=[0.]*7, left=[0.]*7,
                         waist=[0.]*6, waist_height_m=0., waist_direction=-1, feedback_kind='mock',
                         arm_angle_unit='rad', waist_angle_unit='rad')
        if hardware: self.data = dict(yaw_deg=None, pitch_deg=None, right=[], left=[], waist=[])
        self.done, self.wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def submit(self, ticket):
        with self.lock:
            if self.done.is_set():
                ticket.finish(False, self.error or 'SDK connection closed'); return
            if self.pending:
                self.pending.cancel.set(); self.pending.finish(False, 'replaced by newer instruction')
            if self.active: self.active.cancel.set()
            self.pending = ticket
            self.generation += 1
        self.wake.set()

    def interrupt(self):
        with self.lock:
            self.generation += 1
            if self.pending:
                self.pending.cancel.set(); self.pending.finish(False, 'stopped'); self.pending = None
            if self.active: self.active.cancel.set()
        self.wake.set()

    def read(self, channel):
        if channel: self.data = channel.request('READ')
        self.sample_at = time.monotonic()
        return dict(self.data)

    def send(self, channel, op, values=()):
        if channel: return channel.request(op, values)
        if op == 'HEAD': self.data.update(yaw_deg=values[0], pitch_deg=values[1])
        if op == 'WAIST': self.data.update(waist_height_m=values[0], waist_direction=values[1])
        if op == 'ARM': self.data[values[0]] = list(values[1:8])
        return {'ok': True}

    def execute(self, channel, ticket):
        op, values = ticket.instruction.op, ticket.instruction.values
        current = self.read(channel)
        if ticket.cancel.is_set(): ticket.finish(False, 'stopped'); return
        if op in ('h', 'v', 'head'):
            target = list(values) if op == 'head' else [values[0], current['pitch_deg']] if op == 'h' else [current['yaw_deg'], values[0]]
            result = self.send(channel, 'HEAD', target)
        elif op == 's':
            height, direction = values
            result = self.send(channel, 'WAIST', [height, self.direction if direction is None else direction])
        elif op == 'arm': result = self.send(channel, 'ARM', values)
        elif op == 'g': result = current
        else: raise ValueError('unsupported body command')
        interrupted = ticket.cancel.wait(ticket.instruction.wait)
        if interrupted:
            self.send(channel, 'HOLD')
            ticket.finish(False, 'stop event received; no more commands issued')
        else:
            self.error = ''
            ticket.finish(True, 'SDK command returned; not an independent arrival check', result)

    def run(self):
        channel = None
        seen_generation, head_active, waist_previous = -1, False, None
        head_target = None
        previous_time = time.monotonic()
        try:
            if self.hardware: channel = DirectChannel(self.executable, self.lock_fds)
            self.read(channel)
            while not self.done.is_set():
                with self.lock:
                    generation = self.generation
                    ticket, self.pending = self.pending, None
                    self.active = ticket
                if generation != seen_generation:
                    if head_active: self.send(channel, 'HOLD')
                    seen_generation, head_active = generation, False
                if ticket:
                    try: self.execute(channel, ticket)
                    except Exception as exc: self.error = str(exc); ticket.finish(False, str(exc))
                    with self.lock: self.active = None
                    previous_time = time.monotonic()
                    continue
                pressed = self.keys()
                yaw = int('h' in pressed)-int('l' in pressed)
                pitch = int('i' in pressed)-int('k' in pressed)
                now = time.monotonic(); dt = min(.1, now-previous_time); previous_time = now
                if yaw or pitch:
                    if not head_active:
                        s = self.read(channel); head_target = [s['yaw_deg'], s['pitch_deg']]
                    head_target = [max(-20., min(20., head_target[0]+yaw*self.head_rate*dt)),
                                   max(-20., min(20., head_target[1]+pitch*self.head_rate*dt))]
                    self.send(channel, 'HEAD', head_target)
                    head_active = True
                elif head_active:
                    self.send(channel, 'HOLD'); head_active = False
                waist = 0. if 'r' in pressed else self.squat_height if 'f' in pressed else None
                if waist is not None and waist != waist_previous:
                    self.send(channel, 'WAIST', [waist, -1])
                waist_previous = waist
                self.read(channel)
                self.wake.wait(self.period); self.wake.clear()
        except Exception as exc:
            self.error = str(exc)
            with self.lock:
                self.done.set()
                if self.pending: self.pending.finish(False, self.error)
                if self.active: self.active.finish(False, self.error)
        finally:
            if channel:
                try: self.send(channel, 'HOLD')
                except Exception: pass
                channel.close()

    def close(self):
        self.interrupt(); self.done.set(); self.wake.set(); self.thread.join(timeout=6)
