"""Hardware connections live only here; mock mode constructs none of them."""
import json
import math
import os
import queue
import select
import subprocess
import threading
import time

from .core import BaseLease, Refused, SentCommands, numbers, twist


class NativeChannel:
    def __init__(self, executable):
        self.proc = subprocess.Popen([executable, '--hardware'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, start_new_session=True)
        self.pending = b''
        self.sequence = 0
        try:
            if not self.receive(0, 30).get('ready'):
                raise RuntimeError('native SDK did not become ready')
        except Exception:
            self.proc.stdin.close()
            self.proc.terminate()
            raise

    def receive(self, identity, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            while b'\n' in self.pending:
                line, self.pending = self.pending.split(b'\n', 1)
                if line.startswith(b'@RAS '):
                    result = json.loads(line[5:])
                    if result.get('id') == identity:
                        if not result.get('ok'):
                            if result.get('refused'):
                                raise Refused(result.get('error', 'native request refused'))
                            raise RuntimeError(result.get('error', 'native SDK failure'))
                        return result
            if self.proc.poll() is not None:
                raise RuntimeError('native SDK owner exited')
            if select.select([self.proc.stdout], [], [], .05)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 8192)
                if not chunk:
                    raise RuntimeError('native SDK channel closed')
                self.pending += chunk
                if len(self.pending) > 65536:
                    raise RuntimeError('native response exceeded bound')
        raise TimeoutError('native SDK timeout; motion completion UNKNOWN, no retry')

    def request(self, op, values=()):
        self.sequence += 1
        line = ' '.join([str(self.sequence), op, *map(str, values)])+'\n'
        self.proc.stdin.write(line.encode())
        self.proc.stdin.flush()
        return self.receive(self.sequence, 30)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b'999999 QUIT\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                # Do not SIGKILL an SDK during an unknown blocking trajectory.
                self.proc.terminate()
                return False
        return True


class BodyWorker:
    def __init__(self, hardware, executable, admit, trace=None):
        self.hardware = hardware
        self.executable = executable
        self.admit = admit
        self.trace = trace or (lambda *a, **k: None)
        self.queue = queue.Queue(maxsize=1)
        self.done = threading.Event()
        self.busy = False
        self.executing = False
        self.gate = threading.Lock()
        self.fault = ''
        self.last_rejection = ''
        self.state = {'yaw_deg': -4., 'pitch_deg': -.13, 'left': [0.]*7,
                      'right': [0.]*7, 'waist_height_m': 0., 'waist': [0.]*6,
                      'feedback_kind': 'mock', 'head_feedback_fresh': not hardware,
                      'head_feedback_age_s': 0. if not hardware else None, 'ready': not hardware}
        if hardware:
            self.state.update(yaw_deg=None, pitch_deg=None, left=[], right=[], waist=[],
                              waist_height_m=None, feedback_kind='unavailable')
        self.last_read = time.monotonic() if not hardware else -math.inf
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def submit(self, epoch, op, values):
        if self.fault or not self.state.get('ready') or time.monotonic()-self.last_read > .5:
            raise Refused('body feedback unavailable; '+self.fault)
        with self.gate:
            if self.busy:
                raise Refused('body is busy; no motion queueing')
            try:
                self.queue.put_nowait((epoch, time.monotonic()+.4, op, values))
                self.busy = True
            except queue.Full:
                raise Refused('body command already pending')

    def clear(self):
        with self.gate:
            try:
                while True:
                    self.queue.get_nowait()
            except queue.Empty:
                pass
            self.busy = self.executing

    def run(self):
        channel = None
        try:
            if self.hardware:
                channel = NativeChannel(self.executable)
            while not self.done.is_set():
                if channel:
                    self.state.update(channel.request('READ'))
                    self.state['ready'] = True
                self.last_read = time.monotonic()
                try:
                    epoch, expires, op, values = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                if not self.admit(epoch) or time.monotonic() > expires:
                    with self.gate:
                        self.busy = False
                    continue
                with self.gate:
                    self.executing = self.busy = True
                    # Recheck ownership inside the execution gate after queue removal.
                    admitted = self.admit(epoch) and time.monotonic() <= expires
                try:
                    if not admitted:
                        continue
                    if channel:
                        result = channel.request(op, values)
                        self.trace('body_result', op=op, result=result)
                    elif op == 'HEAD':
                        self.state.update(yaw_deg=values[0], pitch_deg=values[1])
                    elif op == 'WAIST':
                        self.state['waist_height_m'] = values[0]
                    elif op == 'ARM':
                        self.state[values[0]] = list(values[1:8])
                    self.last_rejection = ''
                except Refused as exc:
                    self.last_rejection = str(exc)
                    self.trace('body_refused', op=op, reason=str(exc))
                finally:
                    with self.gate:
                        self.executing = self.busy = False
        except Exception as exc:
            self.fault = str(exc)
            self.state['ready'] = False
        finally:
            if channel and not channel.close():
                self.fault = 'SDK owner still exiting: physical supervision required'

    def close(self):
        self.clear()
        self.done.set()
        self.thread.join(timeout=6)
        return not self.thread.is_alive()


class BaseLink:
    TOPICS = {'/motor_speed': 'geometry_msgs/Twist', '/cmd_vel': 'geometry_msgs/Twist',
              '/move_base/status': 'actionlib_msgs/GoalStatusArray',
              '/urgency_button_status': 'std_msgs/Bool',
              '/motor_lock_status': 'keenon_motor_msgs/lock_status'}

    def __init__(self, hardware, limits, host, port, trace=None):
        self.hardware, self.limits = hardware, limits
        self.host, self.port = host, port
        self.trace = trace or (lambda *a, **k: None)
        self.history = SentCommands()
        self.notice = ''
        self.late_echo_count = 0
        self.last_nonzero_seen = -math.inf
        self.lease = BaseLease(limits)
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.connected = not hardware
        self.fault = ''
        self.received = {}
        self.times = {}
        self.stationary_since = None
        self.stationary_count = 0
        self.last_competing = -math.inf
        self.output = (0., 0.)
        self.zero_until = 0.
        self.was_commanded = False
        self.stop_feedback_verified = not hardware
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def ready(self):
        if not self.hardware:
            return True
        now = time.monotonic()
        return (self.connected and now >= self.zero_until and self.fresh(now) and self.stationary_since is not None
                and now-self.stationary_since >= .5 and self.stationary_count >= 10
                and now-self.last_competing >= .5 and now-self.last_nonzero_seen >= .5)

    def fresh(self, now):
        return (now-self.times.get('/motor_speed', -math.inf) <= .2
                and now-self.times.get('/move_base/status', -math.inf) <= 1.2
                and now-self.times.get('/urgency_button_status', -math.inf) <= 5.
                and now-self.times.get('/motor_lock_status', -math.inf) <= 5.
                and self.received.get('/urgency_button_status', {}).get('data') is False
                and self.received.get('/motor_lock_status', {}).get('status') == 1
                and not any(s.get('status') in (0, 1, 6, 7) for s in
                            self.received.get('/move_base/status', {}).get('status_list', [])))

    def command(self, target, continuous=False):
        with self.lock:
            if target != (0., 0.):
                if self.fault:
                    raise Refused('base fault latched: '+self.fault)
                if self.lease.target == (0., 0.) and not self.ready():
                    raise Refused('base preflight: fresh stationary feedback required')
                if self.lease.target not in ((0., 0.), target):
                    self.stop()
                    self.lease.needs_release = True
                    raise Refused('release direction key, wait for stationary feedback before changing direction')
                self.stop_feedback_verified = False
            else:
                if self.was_commanded or self.lease.target != (0., 0.):
                    self.zero_until = max(self.zero_until, time.monotonic()+2.)
                self.was_commanded = False
            self.lease.set(target, time.monotonic(), continuous=continuous)

    def stop(self):
        with self.lock:
            self.lease.stop()
            if self.was_commanded:
                self.zero_until = max(self.zero_until, time.monotonic()+2.)
            self.was_commanded = False

    def reset(self):
        with self.lock:
            if not self.ready():
                raise Refused('base not ready; cannot clear fault')
            self.fault = ''
            self.lease.needs_release = False

    def observe(self, msg, now):
        topic = msg.get('topic')
        if topic not in self.TOPICS:
            return
        data = msg.get('msg')
        if not isinstance(data, dict):
            raise Refused('malformed base feedback')
        if topic == '/motor_speed':
            v = numbers([data[a][b] for a in ('linear', 'angular') for b in ('x', 'y', 'z')], 6)
            if now-self.times.get(topic, now) > .2:
                self.stationary_since, self.stationary_count = None, 0
            if max(abs(x) for x in v) <= .0005:
                self.stationary_since = now if self.stationary_since is None else self.stationary_since
                self.stationary_count += 1
            else:
                self.stationary_since, self.stationary_count = None, 0
            if max(abs(x) for x in v[:3]) > .06 or max(abs(x) for x in v[3:]) > .1:
                raise Refused('excess base speed feedback')
        elif topic == '/cmd_vel':
            v = numbers([data[a][b] for a in ('linear', 'angular') for b in ('x', 'y', 'z')], 6)
            classification = self.history.classify(v, self.output, now)
            self.trace('cmd_vel_received', values=v, classification=classification,
                       output=self.output, receipt_s=now)
            if classification != 'zero':
                self.last_nonzero_seen = now
            if classification == 'foreign':
                self.last_competing = now
                raise Refused('competing or late nonzero cmd_vel; source identity unknown')
            if classification == 'drain':
                # Stop and verify. The payload/time match is NOT a source identity claim.
                self.stop()
                self.lease.needs_release = True
                self.zero_until = max(self.zero_until, now+2.)
                self.stop_feedback_verified = False
                self.late_echo_count += 1
                self.notice = 'recent matching nonzero echo: stopped; release key and wait for stationary feedback'
                self.trace('echo_drain', values=v, zero_until=self.zero_until)
        elif topic == '/move_base/status':
            if not isinstance(data.get('status_list'), list):
                raise Refused('invalid navigation feedback')
            if any(type(s.get('status')) is not int or s['status'] not in range(10)
                   for s in data['status_list']):
                raise Refused('invalid navigation status code')
        elif topic == '/urgency_button_status' and type(data.get('data')) is not bool:
            raise Refused('invalid emergency state')
        elif topic == '/motor_lock_status' and type(data.get('status')) is not int:
            raise Refused('invalid motor state')
        self.received[topic], self.times[topic] = data, now

    def run(self):
        ws = None
        try:
            if self.hardware:
                from .rosbridge import Rosbridge
                ws = Rosbridge(self.host, self.port)
                for topic, typ in self.TOPICS.items():
                    ws.send({'op': 'subscribe', 'topic': topic, 'type': typ, 'queue_length': 1})
                ws.send({'op': 'advertise', 'topic': '/cmd_vel', 'type': 'geometry_msgs/Twist',
                         'queue_size': 1, 'latch': False})
                self.connected = True
            next_send = 0.
            next_latched_refresh = time.monotonic()+2.
            while not self.done.is_set() or time.monotonic() < self.zero_until:
                if ws:
                    if time.monotonic() >= next_latched_refresh:
                        # These OEM values are latched. Receipt freshness is not hardware sample age.
                        for topic in ('/urgency_button_status', '/motor_lock_status'):
                            ws.send({'op': 'unsubscribe', 'topic': topic})
                            ws.send({'op': 'subscribe', 'topic': topic, 'type': self.TOPICS[topic],
                                     'queue_length': 1})
                        next_latched_refresh = time.monotonic()+2.
                    try:
                        msg = ws.receive()
                        if msg:
                            with self.lock:
                                self.observe(msg, time.monotonic())
                    except Refused as exc:
                        with self.lock:
                            if self.fault != str(exc):
                                self.trace('base_fault', reason=str(exc))
                            self.fault = str(exc)
                            self.stop()
                else:
                    self.done.wait(.02)
                with self.lock:
                    now = time.monotonic()
                    output = self.lease.output(now)
                    if self.hardware and output != (0., 0.) and not self.fresh(now):
                        self.fault = 'base status or motion feedback stale/unsafe'
                        self.trace('base_fault', reason=self.fault)
                    if self.fault or self.done.is_set():
                        self.lease.stop()
                        output = (0., 0.)
                    # Drain before the absolute deadline; echoes after zero still latch faults.
                    if self.lease.deadline is not None and now >= self.lease.deadline-.1:
                        output = (0., 0.)
                    if output == (0., 0.) and self.output != (0., 0.):
                        self.zero_until = max(self.zero_until, now+2.)
                        self.was_commanded = False
                    self.output = output
                    if output != (0., 0.):
                        self.was_commanded = True
                    if now >= next_send and (output != (0., 0.) or now < self.zero_until):
                        if ws:
                            ws.send({'op': 'publish', 'topic': '/cmd_vel', 'msg': twist(*output)})
                            self.history.record(output, time.monotonic())
                        self.trace('cmd_vel_sent', output=output, hardware=self.hardware)
                        next_send = now+.05
                    if output == (0., 0.) and self.ready():
                        self.stop_feedback_verified = True
        except Exception as exc:
            self.fault = 'base connection failure, no automatic reconnect: '+str(exc)
        finally:
            if ws:
                # Best effort only. A dead host/network is outside this software guarantee.
                for _ in range(3):
                    try:
                        ws.send({'op': 'publish', 'topic': '/cmd_vel', 'msg': twist()})
                    except OSError:
                        break
                ws.close()
            self.connected = False

    def close(self):
        self.stop()
        self.done.set()
        self.thread.join(timeout=3)
        return not self.thread.is_alive()
