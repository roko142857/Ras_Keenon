"""Exercise the real launch and /dev/tty keyboard against mock in an isolated ROS domain."""
import fcntl
import json
import os
import pty
import signal
import subprocess
import sys
import termios
import time

import rclpy
from rclpy.qos import QoSProfile, DurabilityPolicy
from std_msgs.msg import String

master, slave = pty.openpty()
initial_terminal = termios.tcgetattr(slave)


def terminal_session():
    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)


process = subprocess.Popen(
    ['ros2', 'launch', 'ras_keenon_control', 'manual.launch.py', 'backend:=mock',
     'demo_mode:='+('true' if '--demo' in sys.argv else 'false')],
    stdin=slave, stdout=slave, stderr=slave, preexec_fn=terminal_session)
os.close(slave)
rclpy.init()
node = rclpy.create_node('keyboard_launch_test')
state = {}
node.create_subscription(String, '/ras_keenon/state', lambda msg: state.update(json.loads(msg.data)),
    QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))


def wait(check, timeout=7):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        rclpy.spin_once(node, timeout_sec=.02)
        if check(): return
        if process.poll() is not None:
            break
    raise AssertionError('keyboard condition timeout: '+str(state))


def key(value): os.write(master, value.encode())


def move(value, expected):
    key(value)
    wait(lambda: state.get('base_output') == expected)
    wait(lambda: state.get('base_output') == [0., 0.])
    # Allow the keyboard's zero publication and controller state to settle.
    for _ in range(8): rclpy.spin_once(node, timeout_sec=.03)


try:
    wait(lambda: state.get('body', {}).get('ready'))
    assert state['backend'] == 'mock'
    demo = '--demo' in sys.argv
    if demo:
        wait(lambda: state.get('armed'))  # No enable key or service is needed.
    key('e'); wait(lambda: state.get('armed'))
    if '--interrupt' in sys.argv:
        key('w'); wait(lambda: state['base_output'] == [.02, 0.])
        os.killpg(process.pid, signal.SIGINT)
    else:
        # A single press must bridge a typical 0.5 s desktop key-repeat delay.
        key('w'); wait(lambda: state.get('base_output') == [.02, 0.])
        hold_until = time.monotonic()+.35
        while time.monotonic() < hold_until: rclpy.spin_once(node, timeout_sec=.02)
        assert state['base_output'] == [.02, 0.], 'single press expired before key repeat starts'
        wait(lambda: state.get('base_output') == [0., 0.])
        if demo:
            until = time.monotonic()+2.5
            while time.monotonic() < until:
                key('w')
                for _ in range(5): rclpy.spin_once(node, timeout_sec=.02)
            assert state['base_output'] == [.02, 0.], 'demo hold stopped at old two-second limit'
            key(' ')
            wait(lambda: state['base_output'] == [0., 0.] and state['armed'])
            key('z'); wait(lambda: not state['armed'])
            key('w')
            until = time.monotonic()+.8
            while time.monotonic() < until: rclpy.spin_once(node, timeout_sec=.02)
            assert state['base_output'] == [0., 0.], 'explicit disable was bypassed'
            key('e'); wait(lambda: state['armed'])
        move('w', [.02, 0.]); move('s', [-.02, 0.])
        move('a', [0., .02]); move('d', [0., -.02])
        key('h'); wait(lambda: state['body']['yaw_deg'] == -3.5)
        key('l'); wait(lambda: state['body']['yaw_deg'] == -4.)
        key('i'); wait(lambda: abs(state['body']['pitch_deg']+.08) < 1e-8)
        key('k'); wait(lambda: abs(state['body']['pitch_deg']+.13) < 1e-8)
        key('f'); wait(lambda: state['body']['waist_height_m'] == .03)
        key('r'); wait(lambda: state['body']['waist_height_m'] == 0.)
        key('+'); wait(lambda: state['body']['right'][3] == .02)
        key('v'); wait(lambda: state['source'] == 'vision' and state['armed'] == demo)
        key('m'); wait(lambda: state['source'] == 'manual' and state['armed'] == demo)
        key('e'); wait(lambda: state['armed'])
        before = state['epoch']
        key(' '); wait(lambda: state['epoch'] > before and state['armed'] == demo)
        key('q')
    process.wait(timeout=10)
    assert process.returncode == 0, process.returncode
    assert termios.tcgetattr(master)[3] == initial_terminal[3], 'terminal flags not restored'
    print('PASS: launch + controlling TTY + keys/Ctrl+C + terminal restoration; mock only')
finally:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
    os.set_blocking(master, False)
    logs = b''
    while True:
        try:
            chunk = os.read(master, 65536)
            if not chunk: break
            logs += chunk
        except OSError: break
    print(logs.decode(errors='replace')[-10000:])
    if process.returncode == 0:
        assert b'Traceback' not in logs, logs.decode(errors='replace')
    os.close(master)
    node.destroy_node(); rclpy.shutdown()
