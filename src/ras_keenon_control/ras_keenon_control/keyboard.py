"""Read /dev/tty directly so ros2 launch need not forward its child's stdin."""
import json
import os
import select
import termios
import time
import tty

import rclpy
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from rclpy._rclpy_pybind11 import RCLError
from rclpy.qos import QoSProfile, DurabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger
from ras_keenon_interfaces.msg import BaseCommand, HeadCommand, WaistCommand, ArmCommand
from ras_keenon_interfaces.srv import SelectSource


HELP = '''
RAS KEENON: mock by default. Hardware requires explicit launch flags.
Demo: enabled automatically when ready. E enable (optional) | Z/X disable
SPACE stop (demo remains enabled) | Q quit+disable
W/S forward/reverse | A/D yaw left/right | key inactivity timeout 0.60 s
H/L head yaw +/-0.5 deg | I/K pitch +/-0.05 deg
R stand(height=0) | F squat(configured height)
1/2 select left/right arm | [/] select joint | +/- jog joint 0.02 rad
M manual source | V vision source (demo re-enables when ready)
Head/waist/arm keys also stop base input. Physical direction must be commissioned.
'''


class Keyboard(Node):
    def __init__(self):
        super().__init__('keyboard')
        self.declare_parameter('speed', .02)
        self.declare_parameter('yaw_speed', .02)
        self.declare_parameter('squat_height', .03)
        self.declare_parameter('key_timeout', .6)
        if not .1 <= self.get_parameter('key_timeout').value <= .6:
            raise ValueError('key_timeout must be 0.1..0.6 seconds')
        self.state = dict(epoch=0, armed=False, source='manual')
        self.motion = (0., 0.)
        self.last_key = -1.
        self.side, self.joint = 'right', 3
        self.pubs = {key: self.create_publisher(typ, f'manual/{key}', 1) for key, typ in
                     [('cmd_vel', BaseCommand), ('head', HeadCommand),
                      ('waist', WaistCommand), ('arm', ArmCommand)]}
        self.create_subscription(String, 'state', self.status,
                                 QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.enable_client = self.create_client(SetBool, 'enable')
        self.stop_client = self.create_client(Trigger, 'stop')
        self.source_client = self.create_client(SelectSource, 'select_source')
        self.pending = []
        self.last_error = ''
        self.last_notice = ''

    def status(self, msg):
        previous = self.state
        self.state = json.loads(msg.data)
        if ((self.state.get('epoch') != previous.get('epoch') and previous.get('armed')) or not self.state.get('armed')
                or self.state.get('source') != 'manual'):
            self.motion, self.last_key = (0., 0.), -1.
        summary = (self.state.get('armed'), self.state.get('source'), self.state.get('epoch'))
        if summary != (previous.get('armed'), previous.get('source'), previous.get('epoch')):
            self.get_logger().info(f'CONTROL armed={summary[0]} source={summary[1]} epoch={summary[2]}')
        if self.state.get('base_ready') != previous.get('base_ready'):
            self.get_logger().info(f'BASE ready={self.state.get("base_ready")}; '
                                  'release movement key and wait for ready=True before another motion')
        notice = self.state.get('base_notice', '')
        if notice and notice != self.last_notice:
            self.get_logger().warning(notice)
        self.last_notice = notice
        err = self.state.get('error') or self.state.get('base_fault') or self.state.get('body_fault')
        if err and err != self.last_error:
            self.get_logger().warning(err)
        self.last_error = err

    def send(self, topic, msg):
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.epoch = self.state.get('epoch', 0)
        self.pubs[topic].publish(msg)

    def call(self, client, request):
        try:
            if client.service_is_ready():
                future = client.call_async(request)
                future.add_done_callback(self.reply)
                self.pending.append(future)
                self.pending = [f for f in self.pending if not f.done()]
            else:
                self.get_logger().warning('controller service not ready')
        except RCLError:
            pass  # The launch shutdown may invalidate the context between the two calls.

    def reply(self, future):
        if rclpy.ok() and not future.cancelled():
            self.get_logger().info(str(future.result()))

    def stop(self, hard=False):
        self.motion = (0., 0.)
        self.last_key = -1.
        self.call(self.stop_client, Trigger.Request())
        if hard:
            self.call(self.enable_client, SetBool.Request(data=False))

    def key(self, key):
        if not key:
            return
        speed = self.get_parameter('speed').value
        yaw = self.get_parameter('yaw_speed').value
        movement = {'w': (speed, 0.), 's': (-speed, 0.), 'a': (0., yaw), 'd': (0., -yaw)}
        if key in movement:
            if not self.state.get('armed'):
                self.get_logger().warning('Waiting for interface readiness' if self.state.get('auto_enable_pending')
                                          else 'Disabled or faulted; E enables after the fault is resolved')
            self.motion, self.last_key = movement[key], time.monotonic()
        elif key in (' ', 'x', 'z', 'q'):
            self.stop(hard=key != ' ')
        elif key == 'e':
            self.motion, self.last_key = (0., 0.), -1.
            req = SetBool.Request()
            req.data = True
            self.call(self.enable_client, req)
        elif key in ('m', 'v'):
            self.motion = (0., 0.)
            req = SelectSource.Request()
            req.source = 'manual' if key == 'm' else 'vision'
            self.call(self.source_client, req)
        elif key in 'hlikrf+-':
            self.motion = (0., 0.)
            if key in 'hlik':
                msg = HeadCommand(relative=True)
                msg.yaw_deg = .5 if key == 'h' else -.5 if key == 'l' else 0.
                msg.pitch_deg = .05 if key == 'i' else -.05 if key == 'k' else 0.
                self.send('head', msg)
            elif key in 'rf':
                self.send('waist', WaistCommand(height_m=0. if key == 'r' else self.get_parameter('squat_height').value))
            else:
                values = [0.]*7
                values[self.joint] = .02 if key == '+' else -.02
                self.send('arm', ArmCommand(side=self.side, relative=True, joints_rad=values, speed_factor=.1))
        elif key in '12[]':
            if key in '12':
                self.side = 'left' if key == '1' else 'right'
            else:
                self.joint = (self.joint+(1 if key == ']' else -1)) % 7
            self.get_logger().info(f'Arm selected: {self.side}, joint {self.joint+1}')

    def velocity(self):
        if time.monotonic()-self.last_key > self.get_parameter('key_timeout').value:
            self.motion = (0., 0.)
        if self.state.get('armed') and self.state.get('source') == 'manual':
            msg = BaseCommand()
            msg.twist.linear.x, msg.twist.angular.z = self.motion
            self.send('cmd_vel', msg)


def main(args=None):
    rclpy.init(args=args)
    node = Keyboard()
    fd, original = None, None
    try:
        fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
        original = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        os.write(fd, HELP.encode())
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.)
            if select.select([fd], [], [], .03)[0]:
                data = os.read(fd, 1)
                if not data:
                    break
                key = data.decode(errors='ignore').lower()
                node.key(key)
                if key == 'q':
                    for _ in range(5):
                        rclpy.spin_once(node, timeout_sec=.05)
                    break
            node.velocity()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except OSError as exc:
        node.get_logger().error(f'No controlling terminal: {exc}. Use SSH -t or run keyboard separately.')
    finally:
        node.motion = (0., 0.)
        try:
            if rclpy.ok():
                node.stop(hard=True)
                for _ in range(3):
                    rclpy.spin_once(node, timeout_sec=.03)
        except (RCLError, ExternalShutdownException):
            pass
        if fd is not None:
            if original is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, original)
            os.close(fd)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
