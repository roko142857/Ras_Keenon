"""Isolated RGB-D mapping service. Owns a capture child, never robot motion."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .cloud import Cloud


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def capture_errors(run, returncode):
    errors = [] if returncode == 0 else [f'采集进程退出码 {returncode}']
    camera = read_json(run/'closed.json')
    slam = read_json(run/'slam/closed.json')
    if not run.exists():
        log = run.with_suffix('.log')
        detail = log.read_text()[-2000:] if log.exists() else ''
        if 'insufficient output disk space' in detail:
            return ['采集盘空间不足，请缩短采集时长；原始数据未删除']
        return errors + ['采集未启动：'+(detail.strip().split('\n')[-1] or '没有采集文件')]
    if camera and camera.get('stop_reason') == 'low_disk_space':
        errors.append('采集盘空间不足，已请求正常保存；原始数据未删除')
    elif camera and camera.get('errors'):
        errors.extend(camera['errors'])
    if not camera or not (camera.get('camera') or {}).get('pipeline_stopped'):
        errors.append('相机未正常关闭，检查 closed.json')
    elif camera.get('camera_exitcode') != 0:
        errors.append('相机因异常结束采集，关闭管线已确认')
    if not slam or slam.get('errors') or slam.get('returncode') != 0 or not (slam.get('summary') or {}).get('frames'):
        errors.append('SLAM 未正常完成，检查 slam/closed.json')
    return errors


class Manager:
    def __init__(self, config):
        self.cfg = config
        self.lock = threading.RLock()
        self.process = None
        self.worker = None
        self.run = None
        self.phase = 'idle'
        self.cloud = Cloud()
        self.payload = self.cloud.snapshot()
        self.errors = []
        self.started = 0.
        self.seconds = config.get('default_seconds', 30)

    def start(self, seconds=None):
        with self.lock:
            if (self.phase in ('starting', 'running', 'stopping')
                    or (self.worker and self.worker.is_alive())
                    or (self.process and self.process.poll() is None)):
                raise RuntimeError('已有采集正在运行或收尾')
            if seconds is not None and (type(seconds) is not int or not 5 <= seconds <= 120):
                raise ValueError('采集时长必须为5至120秒的整数')
            self.run = Path(self.cfg['output_root']) / (time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
            self.cloud = Cloud()
            self.payload = self.cloud.snapshot()
            self.errors = []
            self.seconds = seconds
            self.started = time.monotonic()
            self.phase = 'starting'
            self.worker = threading.Thread(target=self.capture, daemon=True)
            self.worker.start()

    def capture(self):
        log = None
        try:
            self.run.parent.mkdir(parents=True, exist_ok=True)
            log = self.run.with_suffix('.log').open('x')
            c = self.cfg
            command = [c['python'], '-m', 'recording.live_session', '--output', str(self.run),
                       '--port', str(c['capture_port']),
                       '--camera', c['camera'], '--rtabmap', c['rtabmap'],
                       '--parameters', c['parameters'], '--profile', c['profile'],
                       '--robot-root', c['robot_root']]
            command.extend(['--until-stop'] if self.seconds is None else ['--seconds', str(self.seconds)])
            with self.lock:
                self.process = subprocess.Popen(command, env=dict(os.environ, PYTHONPATH=c['tools']),
                                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                if self.phase != 'stopping':
                    self.phase = 'running'
            while self.process.poll() is None:
                with self.lock:
                    if self.phase == 'stopping':
                        self.request_camera_stop()
                self.cloud.update(self.run)
                payload = self.cloud.snapshot()
                with self.lock:
                    self.payload = payload
                time.sleep(1)
            if self.process.returncode != 0 or not (self.run/'closed.json').exists():
                self.request_camera_stop()
            self.cloud.update(self.run, closed=True)
            with self.lock:
                self.payload = self.cloud.snapshot()
                self.errors.extend(capture_errors(self.run, self.process.returncode))
                self.phase = 'error' if self.errors else 'saved'
        except Exception as exc:  # noqa: BLE001 -- visible worker boundary; close our capture on failure.
            # A visible failure stops only our capture, preserving camera closure.
            with self.lock:
                self.errors.append(f'{type(exc).__name__}: {exc}')
            if self.process and self.process.poll() is None:
                self.request_camera_stop()
                try:
                    self.process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    with self.lock:
                        self.errors.append('采集收尾超时，原进程仍在；禁止再次启动')
            with self.lock:
                self.phase = 'error'
        finally:
            if log:
                log.close()
            if self.run and self.run.exists():
                (self.run/'panel-result.json').write_text(json.dumps(self.state(), ensure_ascii=False, indent=2))

    def stop(self, expected_run_id=None):
        with self.lock:
            if expected_run_id is not None and (not self.run or self.run.name != expected_run_id):
                raise RuntimeError('capture run changed; stop refused')
            if self.phase not in ('starting', 'running'):
                return
            self.phase = 'stopping'
            self.request_camera_stop()

    def request_camera_stop(self):
        # A process signal can interrupt live_session after camera handlers restore.
        # The native stop file ends the camera, allowing SLAM to finish its database.
        if self.run and self.run.is_dir():
            (self.run/'finish-camera').write_text('RTAB panel requested stop\n')

    def state(self):
        with self.lock:
            pose = read_json(self.run/'slam/current-pose.json') if self.run else None
            receipt = (pose or {}).get('receipt_monotonic_s')
            age = max(0., time.monotonic()-receipt) if receipt is not None else None
            health = read_json(self.run/'capture-health.json') if self.run else None
            return {'backend': 'hardware', 'phase': self.phase, 'run': str(self.run) if self.run else None,
                        'run_id': self.run.name if self.run else None, 'errors': self.errors.copy(),
                        'seconds': self.seconds, 'until_stop': self.seconds is None,
                        'remaining_s': None if self.seconds is None else max(0, int(self.seconds-(time.monotonic()-self.started))),
                        'elapsed_s': max(0., time.monotonic()-self.started) if self.started else 0.,
                        'stop_reason': (health or {}).get('stop_reason'),
                        'disk_free_bytes': (health or {}).get('disk_free_bytes'),
                        'tracking': bool(pose and pose.get('tracked') and age is not None and age < 2),
                        'pose_age_s': age, 'points': len(self.payload['points']),
                        'tracked_frames': self.payload['tracked_frames'], 'lost_frames': self.payload['lost_frames'],
                        'basis': self.payload['basis'], 'version': self.payload['version'], 'motion_enabled': False}


def create_server(manager, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data):
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ('/rtab/state', '/rtab/health'):
                self.respond(200, manager.state())
            elif path == '/rtab/cloud':
                with manager.lock:
                    data = dict(manager.payload, run_id=manager.run.name if manager.run else None)
                self.respond(200, data)
            else:
                self.respond(404, {'error': 'not found'})

        def do_POST(self):
            try:
                if self.headers.get('Origin') not in (None, 'http://'+self.headers.get('Host', '')):
                    self.respond(403, {'error': 'cross-origin request'})
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('invalid body length')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise TypeError('expected object')
                if self.path == '/rtab/start':
                    if set(data)-{'seconds', 'until_stop'}:
                        raise ValueError('unknown fields')
                    if 'until_stop' in data and type(data['until_stop']) is not bool:
                        raise ValueError('until_stop must be boolean')
                    if data.get('until_stop'):
                        if 'seconds' in data:
                            raise ValueError('seconds and until_stop are mutually exclusive')
                        manager.start(None)
                    else:
                        seconds = data.get('seconds', manager.cfg.get('default_seconds', 30))
                        if seconds is None:
                            raise ValueError('continuous capture requires until_stop:true')
                        manager.start(seconds)
                elif self.path == '/rtab/stop':
                    if set(data)-{'run_id'}:
                        raise ValueError('unknown fields')
                    if 'run_id' in data and (not isinstance(data['run_id'], str) or not data['run_id']):
                        raise ValueError('run_id must be a nonempty string')
                    manager.stop(expected_run_id=data.get('run_id'))
                else:
                    self.respond(404, {'error': 'not found'})
                    return
                self.respond(202, manager.state())
            except (ValueError, TypeError, RuntimeError) as exc:
                self.respond(409, {'error': str(exc)})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('settings.json'))
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    with Path(cfg['service_lock']).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = Manager(cfg)
        server = create_server(manager, cfg['port'])
        done = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: done.set())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'RTAB panel ready: 127.0.0.1:{cfg["port"]}; capture idle', flush=True)
        done.wait()
        manager.stop()
        if manager.worker:
            manager.worker.join(45)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
