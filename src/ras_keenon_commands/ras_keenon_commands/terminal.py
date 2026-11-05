"""Line commands via /dev/tty, or one instruction supplied as a ROS parameter."""
import json
import os
import select
import signal
import time
import uuid

import rclpy
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from rclpy._rclpy_pybind11 import RCLError
from std_srvs.srv import Trigger
from ras_keenon_command_interfaces.srv import RunCommand
from .instructions import HELP, parse


class Terminal(Node):
    def __init__(self):
        super().__init__('command_terminal')
        self.declare_parameter('command', '')
        self.declare_parameter('request_timeout', 215.)
        self.requests = []
        self.client = self.create_client(RunCommand, 'execute')
        self.stop_client = self.create_client(Trigger, 'stop')

    def ready(self, timeout=20.):
        until = time.monotonic()+timeout
        while rclpy.ok() and time.monotonic() < until:
            rclpy.spin_once(self, timeout_sec=.05)
            if self.client.service_is_ready(): return True
        return False

    def submit(self, text):
        instruction = parse(text)
        req = RunCommand.Request()
        req.request_id = str(uuid.uuid4())
        req.instruction = text
        future = self.client.call_async(req)
        self.requests.append((future, text, time.monotonic()))
        return future

    def completed(self, output):
        pending = []
        code = 0
        for future, text, started in self.requests:
            if not future.done():
                if time.monotonic()-started > self.get_parameter('request_timeout').value:
                    self.stop()
                    raise TimeoutError('command result unknown; no automatic retry')
                pending.append((future, text, started))
                continue
            reply = future.result()
            output(('OK ' if reply.success else 'ERROR ')+text+': '+reply.message+'\n')
            result = json.loads(reply.result_json or '{}')
            output((result['help'] if 'help' in result else json.dumps(result, ensure_ascii=False, indent=2))+'\n')
            if not reply.success: code = 1
        self.requests = pending
        return code

    def stop(self):
        try:
            if self.stop_client.service_is_ready() and rclpy.ok():
                future = self.stop_client.call_async(Trigger.Request())
                rclpy.spin_until_future_complete(self, future, timeout_sec=.5)
        except (RCLError, ExternalShutdownException, KeyboardInterrupt):
            pass


def main(args=None):
    rclpy.init(args=args)
    node = Terminal()
    fd, code = None, 0
    try:
        command = node.get_parameter('command').value.strip()
        if command: parse(command)
        if not node.ready(): raise RuntimeError('command server unavailable; check launch and component ownership')
        if command:
            node.submit(command)
            while rclpy.ok() and node.requests:
                rclpy.spin_once(node, timeout_sec=.03)
                code = max(code, node.completed(lambda text: print(text, end='', flush=True)))
        else:
            fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
            output = lambda text: os.write(fd, text.encode())
            output(HELP+'\nras> ')
            buffer = ''
            while rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.)
                before = len(node.requests)
                node.completed(output)
                if len(node.requests) < before: output('ras> ')
                if not select.select([fd], [], [], .03)[0]: continue
                chunk = os.read(fd, 4096)
                if not chunk: break
                buffer += chunk.decode(errors='replace')
                if len(buffer) > 4096: buffer='';output('ERROR line too long\nras> ');continue
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if not line: output('ras> ');continue
                    try:
                        instruction = parse(line)
                        if instruction.op in ('q', 'quit'):
                            node.stop()
                            return 0
                        node.submit(line)
                    except ValueError as exc:
                        output('ERROR '+str(exc)+'\nras> ')
    except (KeyboardInterrupt, ExternalShutdownException):
        node.stop()
    except Exception as exc:
        node.stop()
        print('ERROR '+str(exc), flush=True)
        code = 1
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if fd is not None: os.close(fd)
        node.destroy_node()
        if rclpy.ok(): rclpy.shutdown()
    return code
