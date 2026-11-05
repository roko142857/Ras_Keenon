"""Verify installed manual config, namespace matching and CLI precedence without hardware.

Source the built ROS environment, then run this script with the system Python.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

from ament_index_python.packages import get_package_share_directory
import yaml


def wait(check, timeout=10):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        try:
            if check(): return
        except (OSError, ValueError):
            pass
        time.sleep(.02)
    raise AssertionError('condition timed out')


@contextmanager
def controller(*arguments):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryFile(mode='w+') as log:
        process = subprocess.Popen(['ros2', 'launch', 'ras_keenon_control', 'manual.launch.py',
            'backend:=mock', 'components:=base', 'namespace:=manual_config_test',
            'bind:=127.0.0.1', f'port:={port}', *arguments],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        url = f'http://127.0.0.1:{port}'
        try:
            wait(lambda: readings(url)['backend'] == 'mock')
            yield url
        finally:
            if process.poll() is None: os.killpg(process.pid, signal.SIGINT)
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            log.seek(0)
            output = log.read()
            assert process.returncode == 0, output
            assert 'Traceback' not in output, output


def readings(url):
    with urllib.request.urlopen(url+'/readings', timeout=1) as reply:
        return json.load(reply)


def check_motion(url, speed, turn_speed):
    def send(keys, sequence):
        data = json.dumps(dict(client='config-test', seq=sequence, keys=keys)).encode()
        request = urllib.request.Request(url+'/keys', data, {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=1) as reply:
            assert json.load(reply)['accepted']
    send(['w', 'a'], 1)
    wait(lambda: readings(url)['base_output'] == [speed, turn_speed], timeout=2)
    send([], 2)
    wait(lambda: readings(url)['base_output'] == [0., 0.], timeout=2)


def check_parameters(expected):
    reply = subprocess.run(['ros2', 'param', 'dump', '/manual_config_test/direct_controller',
                            '--no-daemon', '--spin-time', '2'],
                           capture_output=True, text=True, timeout=15)
    assert reply.returncode == 0, reply.stdout+reply.stderr
    actual = yaml.safe_load(reply.stdout)['/manual_config_test/direct_controller']['ros__parameters']
    for key, value in expected.items():
        assert actual[key] == value, (key, actual[key], value)


def main():
    path = Path(get_package_share_directory('ras_keenon_commands'))/'config/manual.yaml'
    document = yaml.safe_load(path.read_text())
    defaults = document['/**/direct_controller']['ros__parameters']
    with controller() as url:
        check_parameters(defaults)
        check_motion(url, defaults['speed'], defaults['turn_speed'])
    print('PASS installed default config and custom namespace', flush=True)

    with tempfile.TemporaryDirectory() as directory:
        custom = dict(speed=.015, turn_speed=.011, cycle_ms=4., base_period_ms=10.,
                      key_timeout=.65, base_timeout=.5, body_period_ms=30.,
                      head_rate=3., squat_height=.04)
        document['/**/direct_controller']['ros__parameters'] = custom
        config = Path(directory)/'manual.yaml'
        config.write_text(yaml.safe_dump(document))
        with controller(f'config:={config}') as url:
            check_parameters(custom)
            check_motion(url, .015, .011)
        print('PASS custom file controls motion and all configured parameters', flush=True)
        with controller(f'config:={config}', 'speed:=0.0') as url:
            check_parameters(dict(custom, speed=0.))
            check_motion(url, 0., .011)
        print('PASS explicit CLI override preserves other file values', flush=True)

        custom.update(speed=.1, turn_speed=.1)
        document['/**/direct_controller']['ros__parameters'] = custom
        config.write_text(yaml.safe_dump(document))
        with controller(f'config:={config}') as url:
            check_parameters(custom)
            check_motion(url, .1, .1)
        print('PASS speed=0.1 and turn_speed=0.1 from file, above old 0.03 cap', flush=True)
        with controller(f'config:={config}', 'speed:=0.12', 'turn_speed:=0.15') as url:
            check_parameters(dict(custom, speed=.12, turn_speed=.15))
            check_motion(url, .12, .15)
        print('PASS CLI magnitudes above 0.03, held input and release', flush=True)

        for args, error in [([f'config:={config}.missing'], 'manual control config not found'),
                            ([f'config:={config}', 'speed:=-0.1'], 'speed=-0.1 must be finite and nonnegative'),
                            ([f'config:={config}', 'turn_speed:=-0.1'], 'turn_speed=-0.1 must be finite and nonnegative'),
                            ([f'config:={config}', 'speed:=nan'], 'speed=nan must be finite and nonnegative'),
                            ([f'config:={config}', 'base_timeout:=0.0'], 'base_timeout must be positive')]:
            reply = subprocess.run(['ros2', 'launch', 'ras_keenon_control', 'manual.launch.py',
                                    'backend:=mock', 'components:=base', *args],
                                   capture_output=True, text=True, timeout=15)
            output = reply.stdout+reply.stderr
            # ROS launch may exit cleanly after its child reports a validation error.
            assert error in output and 'Direct control:' not in output, output
            if 'missing' in args[0]: assert reply.returncode != 0, output
        print('PASS missing config and invalid motion values fail at startup', flush=True)


if __name__ == '__main__': main()
