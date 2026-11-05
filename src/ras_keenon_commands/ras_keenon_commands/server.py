"""Direct, latest-input control: no enable/readiness/source/epoch state machine."""
import fcntl
import json
import math
import signal
from pathlib import Path
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor, ExternalShutdownException
from rclpy.callback_groups import ReentrantCallbackGroup
from std_msgs.msg import String
from std_srvs.srv import Trigger
from ras_keenon_command_interfaces.srv import RunCommand
from ament_index_python.packages import get_package_prefix

from .direct_io import KeyLatch, Ticket, DirectBase, DirectBody
from .instructions import HELP, parse
from .battery import BatteryMonitor, mock_battery
from .mapping_proxy import MappingProxy
from .rtab_proxy import RtabProxy

# Entry points that can move the robot; read-only, help and stop stay session-free.
# `sd` only sets the persistent squat direction, but it changes the next `s`, so it is gated too.
MOTION_OPS = frozenset({'base', 'h', 'v', 'head', 's', 'arm', 'sd'})


class StaleSession(ValueError):
    """A motion request carried no session or one a stop has revoked."""


class ExpiredTicket(ValueError):
    """A base request whose absolute deadline had already passed on arrival."""


def unpack_command(text):
    """A ROS String may carry the instruction with its session and absolute deadlines."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text, None, None, None
    if isinstance(payload, dict) and isinstance(payload.get('instruction'), str):
        return (payload['instruction'], payload.get('session'),
                payload.get('ticket_until'), payload.get('guard_until'))
    return text, None, None, None


class CommandServer(Node):
    def __init__(self):
        super().__init__('direct_controller')
        defaults = dict(backend='mock', components='all', timeout=185., cycle_ms=2.,
                        base_period_ms=20., body_period_ms=50., web=False, bind='0.0.0.0',
                        port=8765, speed=.02, turn_speed=.02, head_rate=5., squat_height=.03,
                        key_timeout=.3, base_timeout=.25, mapping_url='http://127.0.0.1:18781')
        for k, v in defaults.items(): self.declare_parameter(k, v)
        cfg = {k: self.get_parameter(k).value for k in defaults}
        if cfg['backend'] not in ('mock', 'hardware') or cfg['components'] not in ('body', 'base', 'all'):
            raise ValueError('backend=mock|hardware, components=body|base|all')
        for k in ('timeout', 'cycle_ms', 'base_period_ms', 'body_period_ms', 'head_rate',
                  'key_timeout', 'base_timeout'):
            if not math.isfinite(cfg[k]) or cfg[k] <= 0: raise ValueError(k+' must be positive')
        # These are configured key magnitudes, not the old commissioning burst limits.
        for k, unit in (('speed', 'm/s'), ('turn_speed', 'rad/s')):
            if not math.isfinite(cfg[k]) or cfg[k] < 0:
                raise ValueError(f'{k}={cfg[k]!r} must be finite and nonnegative ({unit})')
        if not math.isfinite(cfg['squat_height']) or not 0 <= cfg['squat_height'] <= .273:
            raise ValueError(f"squat_height={cfg['squat_height']!r} outside 0..0.273 m")
        self.cfg, self.hardware = cfg, cfg['backend'] == 'hardware'
        self.lock = threading.RLock()
        # Unique to this process start, so a panel that cached the previous one never matches.
        self.session = uuid.uuid4().hex
        self.keys = KeyLatch(timeout=cfg['key_timeout'])
        self.previous_keys = frozenset()
        self.base_ticket = None
        self.base_until = 0.
        self.tickets = OrderedDict()
        self.resources = []
        self.base, self.body, self.http, self.battery = None, None, None, None
        self.closed = False
        self.started = self.last_tick = time.monotonic()
        self.tick_count, self.tick_total, self.tick_max, self.overruns = 0, 0., 0., 0
        # Exclusive SDK/socket ownership, unrelated to enabling motion or robot feedback.
        if self.hardware:
            paths = [Path.home()/'ras_keenon_ws/run/hardware.lock']
            if cfg['components'] in ('base', 'all'):
                paths.append(Path.home()/'homebody/commissioning/run/chassis.lock')
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open('a')
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    raise RuntimeError('another controller owns '+str(path)+'; close its launch first')
                self.resources.append(handle)
        try:
            if self.hardware: self.battery = BatteryMonitor()
            if cfg['components'] in ('base', 'all'):
                self.base = DirectBase(self.hardware, cfg['base_period_ms']/1000.,
                                       timeout=cfg['base_timeout'])
            if cfg['components'] in ('body', 'all'):
                exe = ''
                if self.hardware:
                    exe = str(Path(get_package_prefix('ras_keenon_command_native'))/
                              'lib/ras_keenon_command_native/command_sdk_bridge')
                self.body = DirectBody(self.hardware, exe, lambda: self.keys.read(time.monotonic()),
                                       cfg['body_period_ms']/1000., cfg['head_rate'], cfg['squat_height'],
                                       lock_fds=[h.fileno() for h in self.resources])
            self.group = ReentrantCallbackGroup()
            self.create_service(RunCommand, 'execute', self.execute, callback_group=self.group)
            self.create_service(Trigger, 'stop', self.stop_service, callback_group=self.group)
            self.create_subscription(String, 'keys', self.key_message, 1)
            self.create_subscription(String, 'command', self.command_message, 1)
            # A single timer callback at a time. Services run in the reentrant group.
            self.create_timer(cfg['cycle_ms']/1000., self.cycle)
            if cfg['web']: self.start_web()
            self.get_logger().info('Direct control: no enable/base_ready/source/epoch gates; '
                f"input {cfg['cycle_ms']} ms, base {cfg['base_period_ms']} ms, body {cfg['body_period_ms']} ms; "
                f"backend={cfg['backend']}; speed={cfg['speed']} m/s, turn_speed={cfg['turn_speed']} rad/s; "
                f"key_timeout={cfg['key_timeout']} s, base_timeout={cfg['base_timeout']} s")
            if self.http: self.get_logger().info(f"Open http://192.168.3.20:{cfg['port']} for keydown/keyup control")
        except Exception:
            self.close()
            raise

    def cancel_base(self, message='replaced by newer input'):
        if self.base_ticket:
            self.base_ticket.cancel.set(); self.base_ticket.finish(False, message)
            self.base_ticket = None
        if self.base: self.base.set(force=True)

    def stop(self):
        with self.lock:
            # Revoke every caller that was admitted under the previous session.
            self.session = uuid.uuid4().hex
            self.keys.clear(); self.previous_keys = frozenset()
            self.cancel_base('stopped')
            if self.body: self.body.interrupt()
            return self.session

    def accept_keys(self, data):
        with self.lock:
            session = data.get('session')
            if type(session) is not str or session != self.session:
                raise StaleSession('missing or revoked session '+repr(session))
            accepted, changed = self.keys.update(data['keys'], data['client'], data['seq'], time.monotonic())
            if accepted and changed:
                # Release events request zero immediately, before the next scheduled tick.
                self.cancel_base('keyboard input replaced command')
                if self.body: self.body.interrupt()
                self.previous_keys = self.keys.read(time.monotonic())
            return accepted, self.session

    def key_message(self, msg):
        try: self.accept_keys(json.loads(msg.data))
        except (ValueError, TypeError, KeyError) as exc: self.get_logger().error(str(exc))

    def command_message(self, msg):
        try:
            text, session, ticket_until, guard_until = unpack_command(msg.data)
            self.submit(text, session=session, ticket_until=ticket_until,
                        guard_until=guard_until)
        except (ValueError, TypeError, KeyError) as exc: self.get_logger().error(str(exc))

    def snapshot(self):
        return dict(backend=self.cfg['backend'], session=self.session,
                    pressed=sorted(self.keys.read(time.monotonic())),
                    base_output=list(self.base.sent) if self.base else None,
                    base_error=self.base.error if self.base else '',
                    body=dict(self.body.data) if self.body else None,
                    body_error=self.body.error if self.body else '',
                    body_command=self.body.active.instruction.op if self.body and self.body.active else None,
                    body_sample_age_s=time.monotonic()-self.body.sample_at if self.body and self.body.sample_at else None,
                    cycle=self.statistics(), battery=self.battery_snapshot())

    def battery_snapshot(self):
        return self.battery.snapshot() if self.battery else mock_battery()

    def statistics(self):
        return dict(requested_ms=self.cfg['cycle_ms'], ticks=self.tick_count,
                    mean_ms=1000*self.tick_total/max(1, self.tick_count),
                    max_ms=1000*self.tick_max, intervals_over_twice_period=self.overruns,
                    elapsed_s=time.monotonic()-self.started)

    def submit(self, text, identity=None, session=None, ticket_until=None, guard_until=None):
        instruction = parse(text)
        with self.lock:
            # Admission and ticket/base_ticket assignment share the stop() lock.
            if instruction.op in MOTION_OPS and (
                    type(session) is not str or session != self.session):
                raise StaleSession('missing or revoked session '+repr(session))
            until = None
            if instruction.op == 'base' and (ticket_until is not None or guard_until is not None):
                # The actor and this server share one host and one monotonic clock. An
                # expired request is refused here; a late one never regains a full window.
                now = time.monotonic()
                if (type(ticket_until) not in (int, float)
                        or type(guard_until) not in (int, float)):
                    raise ExpiredTicket('base needs absolute ticket_until and guard_until')
                if ticket_until <= now or guard_until <= now:
                    raise ExpiredTicket('request expired before admission')
                until = min(now+instruction.values[2], ticket_until, guard_until)
            identity = identity or str(uuid.uuid4())
            if identity in self.tickets:
                old_text, ticket = self.tickets[identity]
                if old_text != text: raise ValueError('request_id reused with different instruction')
                return identity, ticket
            ticket = Ticket(instruction)
            self.tickets[identity] = (text, ticket)
            while len(self.tickets) > 128:
                key = next((k for k, (_, t) in self.tickets.items() if t.done.is_set()), None)
                if key is None:
                    self.tickets.pop(identity)
                    raise ValueError('too many pending requests')
                del self.tickets[key]
            op = instruction.op
            if op == 'help': ticket.finish(True, 'help', {'help': HELP})
            elif op == 'g': ticket.finish(True, 'current readings and output; no arrival guarantee', self.snapshot())
            elif op in ('stop', 'q', 'quit'):
                self.stop(); ticket.finish(True, 'stop event sent; blocking SDK calls finish before returning')
            elif op == 'sd':
                if not self.body: ticket.finish(False, 'launch components:=body or all')
                else:
                    self.body.direction = instruction.values[0]
                    ticket.finish(True, 'default squat direction changed', {'direction': self.body.direction})
            else:
                self.keys.clear(); self.previous_keys = frozenset()
                renewing = (op == 'base' and until is not None and self.base_ticket is not None
                            and not self.base_ticket.cancel.is_set()
                            and time.monotonic() < self.base_until)
                if renewing:
                    self.base_ticket.cancel.set()
                    self.base_ticket.finish(False, 'replaced by newer bounded base target')
                    self.base_ticket = None
                else:
                    self.cancel_base()
                    if self.body: self.body.interrupt()
                if op == 'base':
                    if not self.base: ticket.finish(False, 'launch components:=base or all')
                    else:
                        self.base_ticket = ticket
                        self.base_until = (until if until is not None
                                           else time.monotonic()+instruction.values[2])
                        if until is not None:
                            self.base.set(*instruction.values[:2], until=self.base_until)
                elif not self.body: ticket.finish(False, 'launch components:=body or all')
                else: self.body.submit(ticket)
            return identity, ticket

    def cycle(self):
        now = time.monotonic()
        dt, self.last_tick = now-self.last_tick, now
        self.tick_count += 1; self.tick_total += dt; self.tick_max = max(self.tick_max, dt)
        if dt > 2*self.cfg['cycle_ms']/1000.: self.overruns += 1
        with self.lock:
            pressed = self.keys.read(now)
            if self.previous_keys and not pressed:
                self.cancel_base('key released or heartbeat lost')
                if self.body: self.body.interrupt()
            self.previous_keys = pressed
            x, yaw = 0., 0.
            if self.base_ticket:
                if now >= self.base_until or self.base_ticket.cancel.is_set():
                    if self.base: self.base.set(force=True)
                    self.base_ticket.finish(not self.base.error, self.base.error or 'duration finished; zero requested',
                                            {'arrival_verified': False})
                    self.base_ticket = None
                else: x, yaw = self.base_ticket.instruction.values[:2]
            elif pressed:
                x = (int('w' in pressed)-int('s' in pressed))*self.cfg['speed']
                yaw = (int('a' in pressed)-int('d' in pressed))*self.cfg['turn_speed']
            if self.base: self.base.set(x, yaw, until=self.base_until if self.base_ticket else None)

    def execute(self, request, response):
        try:
            text, session, ticket_until, guard_until = unpack_command(request.instruction)
            _, ticket = self.submit(text, request.request_id or None, session=session,
                                    ticket_until=ticket_until, guard_until=guard_until)
            if not ticket.done.wait(self.cfg['timeout']):
                self.stop()
                ticket.finish(False, 'result timed out; blocking SDK call may still be running')
            response.success, response.message = ticket.success, ticket.message
            response.result_json = json.dumps(ticket.result, allow_nan=False)
        except Exception as exc:
            response.success, response.message, response.result_json = False, str(exc), '{}'
        return response

    def stop_service(self, request, response):
        self.stop(); response.success, response.message = True, 'stop event sent'
        return response

    def start_web(self):
        node = self
        mapping = MappingProxy(self.cfg['mapping_url'], self.cfg['backend'])
        rtab = RtabProxy(self.cfg['backend'])
        page = Path(__file__).with_name('panel.html').read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def respond(self, code, data, content='application/json'):
                body = data if isinstance(data, bytes) else json.dumps(data, allow_nan=False).encode()
                self.send_response(code); self.send_header('Content-Type', content+'; charset=utf-8')
                self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                if self.path == '/': self.respond(200, page, 'text/html')
                elif self.path in ('/rtab.js', '/rtab.css'):
                    name = self.path[1:]
                    self.respond(200, Path(__file__).with_name(name).read_bytes(), 'text/javascript' if name.endswith('.js') else 'text/css')
                elif self.path.startswith('/rtab/'):
                    try:
                        code, body, kind = rtab.request(self.path)
                        self.respond(code, body, kind)
                    except (OSError, ValueError) as exc: self.respond(503, {'error': 'RTAB 后台不可用：'+str(exc)})
                elif self.path == '/battery.js':
                    self.respond(200, Path(__file__).with_name('battery.js').read_bytes(), 'text/javascript')
                elif self.path in ('/mapping.js', '/mapping.css'):
                    name = self.path[1:]
                    self.respond(200, Path(__file__).with_name(name).read_bytes(), 'text/javascript' if name.endswith('.js') else 'text/css')
                elif self.path.startswith('/mapping/'):
                    try:
                        code, body, kind = mapping.request(self.path)
                        self.respond(code, body, kind)
                    except Exception as exc: self.respond(503, {'error': '建图后台不可用：'+str(exc)})
                elif self.path == '/battery': self.respond(200, node.battery_snapshot())
                elif self.path == '/readings': self.respond(200, node.snapshot())
                elif self.path.startswith('/result/'):
                    with node.lock: entry = node.tickets.get(self.path[8:])
                    if not entry: self.respond(404, {'error': 'unknown request'})
                    else:
                        t = entry[1]
                        self.respond(200, dict(done=t.done.is_set(), success=t.success, message=t.message, result=t.result))
                else: self.respond(404, {'error': 'not found'})
            def do_POST(self):
                try:
                    origin = self.headers.get('Origin')
                    if origin and origin != 'http://'+self.headers.get('Host', ''):
                        self.respond(403, {'error': 'cross-origin request'}); return
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8192: raise ValueError('invalid body length')
                    data = json.loads(self.rfile.read(length))
                    if self.path in ('/rtab/start', '/rtab/stop'):
                        try:
                            code, body, kind = rtab.request(self.path, data)
                            self.respond(code, body, kind)
                        except (OSError, ValueError) as exc: self.respond(503, {'error': 'RTAB 后台不可用：'+str(exc)})
                    elif self.path == '/keys':
                        accepted, session = node.accept_keys(data)
                        self.respond(200, {'accepted': accepted, 'session': session})
                    elif self.path == '/stop':
                        self.respond(200, {'ok': True, 'session': node.stop()})
                    elif self.path == '/command':
                        identity, _ = node.submit(
                            data['instruction'], session=data.get('session'),
                            ticket_until=data.get('ticket_until'),
                            guard_until=data.get('guard_until'))
                        self.respond(200, {'id': identity})
                    elif self.path in ('/mapping/start', '/mapping/finish', '/mapping/cancel', '/mapping/export', '/mapping/refresh', '/mapping/map_preview', '/mapping/map_export', '/mapping/map_switch', '/mapping/map_update'):
                        controller_session = node.stop() if self.path in ('/mapping/finish', '/mapping/cancel') else None
                        try:
                            code, body, kind = mapping.request(self.path, data)
                            if controller_session and code < 300:
                                result = json.loads(body); result['controller_session'] = controller_session
                                body = json.dumps(result, allow_nan=False).encode()
                            self.respond(code, body, kind)
                        except Exception as exc: self.respond(503, {'error': '建图后台不可用：'+str(exc)})
                    else: self.respond(404, {'error': 'not found'})
                except StaleSession as exc: self.respond(409, {'error': str(exc)})
                except (ValueError, TypeError, KeyError) as exc: self.respond(400, {'error': str(exc)})
        self.http = ThreadingHTTPServer((self.cfg['bind'], self.cfg['port']), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self):
        if self.closed: return
        self.closed = True
        self.stop()
        if self.http: self.http.shutdown(); self.http.server_close()
        if self.battery: self.battery.close()
        if self.base: self.base.close()
        if self.body: self.body.close()
        for handle in self.resources: handle.close()
        path = Path.home()/'ras_keenon_ws/run/direct_cycle_stats.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.statistics(), indent=2))


def main(args=None):
    rclpy.init(args=args)
    node, executor = None, None
    try:
        node = CommandServer()
        executor = MultiThreadedExecutor(num_threads=4)
        executor.add_node(node); executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException): pass
    finally:
        # ros2 launch may forward a second SIGINT after the process group received one.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if node: node.stop()
        if executor: executor.shutdown(timeout_sec=3.)
        if node: node.close(); node.destroy_node()
        if rclpy.ok(): rclpy.shutdown()
