import fcntl
import json
import math
import os
from pathlib import Path
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger
from sensor_msgs.msg import JointState
from ament_index_python.packages import get_package_prefix
from ras_keenon_interfaces.msg import BaseCommand, HeadCommand, WaistCommand, ArmCommand
from ras_keenon_interfaces.srv import SelectSource

from .backends import BaseLink, BodyWorker
from .core import (Session, Limits, Refused, base_target, head_target, waist_target,
                   arm_target, startup_head_window)
from .trace import Trace


def conflicting_processes():
    names = {'arm_daemon', 'native_roundtri', 'native_roundtrip', 'action_node', 'head_bridge',
             'head_waist_tool', 'remote_control_', 'teaching_mode', 'sdk_bridge'}
    found = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            comm = (p/'comm').read_text().strip()
            if comm in names or comm.startswith(('remote_control', 'command_sdk')):
                found.append(f'{p.name}:{comm}')
        except (OSError, PermissionError):
            pass
    return found


class Driver(Node):
    def __init__(self):
        super().__init__('controller')
        defaults = dict(backend='mock', allow_motion=True, allow_arm=False, demo_mode=True,
                        allow_reverse=False, allow_turn=False,
                        base_host='192.168.64.20', base_port=9091,
                        head_window_mode='startup_relative', head_yaw_span=1., head_pitch_span=.1,
                        trace_path=str(Path.home()/'ras_keenon_ws/run/control_events.jsonl'))
        defaults.update(Limits().__dict__)
        for key, value in defaults.items():
            self.declare_parameter(key, value)
        self.params = {key: self.get_parameter(key).value for key in defaults}
        if self.params['backend'] not in ('mock', 'hardware'):
            raise ValueError('backend must be mock or hardware')
        self.hardware = self.params['backend'] == 'hardware'
        self.limits = Limits(**{key: self.params[key] for key in Limits().__dict__})
        self.session = Session(self.limits)
        self.auto_enable_pending = bool(self.params['demo_mode'])
        if self.params['head_window_mode'] not in ('startup_relative', 'absolute'):
            raise ValueError('head_window_mode must be startup_relative or absolute')
        startup_head_window(0., 0., self.params['head_yaw_span'], self.params['head_pitch_span'])
        self.head_reference = None
        self.trace = Trace(str(Path(self.params['trace_path']).expanduser()))
        self.trace.record('startup', parameters=self.params)
        self.last_error = ''
        self.locks = []
        native = ''
        if self.hardware:
            conflicts = conflicting_processes()
            if conflicts:
                raise RuntimeError('SDK owner conflict; close it normally first: '+', '.join(conflicts))
            for path in (Path.home()/'ras_keenon_ws/run/hardware.lock',
                         Path.home()/'homebody/commissioning/run/chassis.lock'):
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open('a')
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.locks.append(handle)
            native = str(Path(get_package_prefix('ras_keenon_native'))/'lib/ras_keenon_native/sdk_bridge')
        self.base = BaseLink(self.hardware, self.limits, self.params['base_host'], self.params['base_port'], self.trace.record)
        self.body = BodyWorker(self.hardware, native,
                               lambda epoch: self.session.armed and self.session.epoch == epoch, self.trace.record)
        qos = QoSProfile(depth=1)
        self.subscriptions_kept = []
        for source in ('manual', 'vision'):
            for topic, typ, handler in (
                    ('cmd_vel', BaseCommand, self.base_command),
                    ('head', HeadCommand, self.head_command),
                    ('waist', WaistCommand, self.waist_command),
                    ('arm', ArmCommand, self.arm_command)):
                self.subscriptions_kept.append(self.create_subscription(
                    typ, f'{source}/{topic}',
                    lambda msg, s=source, h=handler: self.dispatch(s, msg, h), qos))
        self.create_service(SetBool, 'enable', self.enable)
        self.create_service(Trigger, 'stop', self.stop_service)
        self.create_service(SelectSource, 'select_source', self.select_source)
        self.status_pub = self.create_publisher(String, 'state',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.joints_pub = self.create_publisher(JointState, 'joint_states', 1)
        self.last_joint_read = 0.
        self.create_timer(.1, self.tick)
        self.get_logger().info(f'Backend={self.params["backend"]}; demo_mode={self.params["demo_mode"]}; '
                               'waiting for readiness; SPACE stops. No command is generated automatically.')

    def dispatch(self, source, msg, handler):
        try:
            self.session.admit(source, msg.epoch,
                msg.header.stamp.sec+msg.header.stamp.nanosec*1e-9,
                self.get_clock().now().nanoseconds*1e-9)
            handler(msg)
            is_idle = isinstance(msg, BaseCommand) and not any((msg.twist.linear.x, msg.twist.linear.y,
                msg.twist.linear.z, msg.twist.angular.x, msg.twist.angular.y, msg.twist.angular.z))
            if not is_idle:
                self.last_error = ''
                self.trace.record('command_accepted', source=source, epoch=msg.epoch,
                                  message_type=type(msg).__name__)
        except (Refused, KeyError, TypeError, ValueError) as exc:
            if self.last_error != str(exc):
                self.trace.record('command_refused', source=source, epoch=msg.epoch,
                                  message_type=type(msg).__name__, reason=str(exc))
            self.last_error = str(exc)

    def base_command(self, msg):
        t = msg.twist
        values = [t.linear.x, t.linear.y, t.linear.z, t.angular.x, t.angular.y, t.angular.z]
        target = base_target(values, self.limits,
                             not self.hardware or self.params['allow_reverse'],
                             not self.hardware or self.params['allow_turn'])
        if self.body.busy and target != (0., 0.):
            raise Refused('body operation running; base motion refused')
        self.base.command(target, continuous=self.params['demo_mode'] and self.session.source == 'manual')

    def body_submit(self, op, values):
        if self.base.output != (0., 0.) or self.base.lease.target != (0., 0.):
            raise Refused('stop base before a body command')
        if not self.base.stop_feedback_verified:
            raise Refused('stationary base feedback not confirmed')
        self.body.submit(self.session.epoch, op, values)

    def head_command(self, msg):
        if self.head_reference is None:
            raise Refused('head baseline not ready; wait for fresh head feedback')
        if self.hardware:
            age = self.body.state.get('head_feedback_age_s')
            if (not self.body.state.get('head_feedback_fresh') or age is None
                    or age+time.monotonic()-self.body.last_read > .25):
                raise Refused('fresh head CSP feedback required')
        target = head_target([msg.yaw_deg, msg.pitch_deg], self.body.state, msg.relative, self.limits)
        self.body_submit('HEAD', target)

    def waist_command(self, msg):
        self.body_submit('WAIST', [waist_target(msg.height_m, self.limits)])

    def arm_command(self, msg):
        if self.hardware and not self.params['allow_arm']:
            raise Refused('arm motion requires allow_arm:=true; blocking SDK is not preemptible')
        q = arm_target(msg.side, msg.joints_rad, self.body.state,
                       msg.relative, msg.speed_factor, self.limits)
        self.body_submit('ARM', [msg.side, *q, msg.speed_factor])

    def stop(self):
        self.auto_enable_pending = False
        self.trace.record('stop_requested', epoch=self.session.epoch, body_busy=self.body.busy)
        self.session.stop()
        self.body.clear()
        self.base.stop()

    def enable(self, request, response):
        try:
            if request.data and self.session.armed and not (self.base.fault or self.body.fault):
                response.success, response.message = True, 'already enabled'
                return response
            self.stop()
            if request.data:
                if self.hardware and not self.params['allow_motion']:
                    raise Refused('hardware launch requires allow_motion:=true')
                if self.body.fault or not self.body.state.get('ready'):
                    raise Refused('SDK not ready: '+self.body.fault)
                self.base.reset()
                self.session.arm()
            response.success = True
            response.message = f'armed={self.session.armed}; epoch={self.session.epoch}'
            self.last_error = ''
            self.trace.record('enable', armed=self.session.armed, epoch=self.session.epoch)
        except Refused as exc:
            response.success, response.message = False, str(exc)
            self.last_error = str(exc)
        return response

    def stop_service(self, request, response):
        keep_enabled = self.params['demo_mode'] and self.session.source == 'manual' and self.session.armed
        self.stop()
        self.session.armed = keep_enabled
        response.success = True
        response.message = f'Stopped, armed={keep_enabled}; old commands discarded, base zero requested. '
        response.message += 'An in-flight blocking SDK trajectory is NOT interrupted.'
        return response

    def select_source(self, request, response):
        try:
            self.stop()
            self.session.select(request.source)
            self.auto_enable_pending = self.params['demo_mode'] and request.source != 'idle'
            self.trace.record('source_selected', source=request.source, epoch=self.session.epoch)
            response.success = True
            response.message = ('Source selected; will enable when ready.' if self.auto_enable_pending
                                else 'Source selected; call enable before sending fresh commands.')
        except Refused as exc:
            response.success, response.message = False, str(exc)
        response.epoch = self.session.epoch
        return response

    def tick(self):
        if self.head_reference is None and self.body.state.get('ready'):
            s = self.body.state
            if not self.hardware or s.get('head_feedback_fresh'):
                try:
                    window = startup_head_window(s['yaw_deg'], s['pitch_deg'],
                        self.params['head_yaw_span'], self.params['head_pitch_span'])
                    if self.params['head_window_mode'] == 'startup_relative':
                        for key, value in window.items():
                            setattr(self.limits, key, value)
                    self.head_reference = [s['yaw_deg'], s['pitch_deg']]
                    self.trace.record('head_reference', measured=self.head_reference, window=window)
                    self.get_logger().info(f'Head baseline measured: {self.head_reference}; '
                        f'yaw [{self.limits.yaw_min:.3f}, {self.limits.yaw_max:.3f}], '
                        f'pitch [{self.limits.pitch_min:.3f}, {self.limits.pitch_max:.3f}] deg')
                except (Refused, KeyError, TypeError) as exc:
                    self.last_error = str(exc)
        if self.session.armed and (self.base.fault or self.body.fault):
            self.last_error = self.base.fault or self.body.fault
            self.get_logger().error('Disarmed: '+self.last_error)
            self.stop()
        if (self.auto_enable_pending and not self.session.armed and self.session.source != 'idle'
                and self.body.state.get('ready') and not self.body.fault and not self.base.fault
                and self.base.ready() and (not self.hardware or self.params['allow_motion'])):
            response = self.enable(SetBool.Request(data=True), SetBool.Response())
            if response.success:
                self.get_logger().info('Demo ready: motion keys are active; E is optional.')
        age = time.monotonic()-self.body.last_read
        status = dict(backend=self.params['backend'], demo_mode=self.params['demo_mode'], armed=self.session.armed,
                      source=self.session.source, epoch=self.session.epoch,
                      error=self.last_error or self.body.last_rejection, body_busy=self.body.busy,
                      body_fault=self.body.fault, base_fault=self.base.fault,
                      body=dict(self.body.state), body_receipt_age_s=age if math.isfinite(age) else None,
                      base_ready=self.base.ready(), base_output=self.base.output,
                      base_notice=self.base.notice, late_echo_count=self.base.late_echo_count,
                      head_reference=self.head_reference,
                      auto_enable_pending=self.auto_enable_pending,
                      head_window={key: getattr(self.limits, key) for key in
                                   ('yaw_min', 'yaw_max', 'pitch_min', 'pitch_max')},
                      trace_dropped=self.trace.dropped,
                      stop_feedback_verified=self.base.stop_feedback_verified,
                      sdk_motion_preemptible=False)
        self.status_pub.publish(String(data=json.dumps(status, allow_nan=False)))
        if (self.body.state.get('ready') and self.body.last_read != self.last_joint_read
                and age <= .5):
            self.last_joint_read = self.body.last_read
            s = self.body.state
            values = [math.radians(s['yaw_deg']), math.radians(s['pitch_deg']), *s['left'], *s['right']]
            if len(values) == 16 and all(type(v) in (int, float) and math.isfinite(v) for v in values):
                msg = JointState()
                msg.header.stamp = self.get_clock().now().to_msg()
                msg.header.frame_id = s['feedback_kind']
                msg.name = ['head_yaw', 'head_pitch']+[f'{side}_joint_{i+1}' for side in ('left', 'right') for i in range(7)]
                msg.position = values
                self.joints_pub.publish(msg)

    def close(self):
        self.stop()
        self.base.close()
        if not self.body.close():
            self.get_logger().error('SDK worker still busy; physical supervision required. No kill/retry issued.')
        for handle in self.locks:
            handle.close()
        self.trace.close()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = Driver()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node:
            node.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
