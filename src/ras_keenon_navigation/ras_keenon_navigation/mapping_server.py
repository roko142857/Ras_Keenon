"""Loopback mapping API. Listening never starts SLAM or moves the robot."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
from urllib.parse import urlparse
from .mapping import Mapping


def create_server(manager, host='127.0.0.1', port=18781):
    if host not in ('127.0.0.1', '::1', 'localhost'): raise ValueError('建图后台只监听回环地址')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, code, value, kind='application/json', version=None):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code); self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(raw)))
            if version: self.send_header('ETag', version)
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_GET(self):
            try:
                path = urlparse(self.path).path
                if path == '/api/health': self.respond(200, dict(backend=manager.backend, service='ras_keenon_mapping', autonomous_exploration=False, map_management=True, overwrite_types=['map']))
                elif path == '/api/state': self.respond(200, manager.snapshot())
                elif path == '/api/maps':
                    state = manager.snapshot(); self.respond(200, {k:state[k] for k in ('backend', 'maps', 'maps_updated_at', 'maps_error')})
                elif path == '/api/map.png':
                    raw, version = manager.preview(); self.respond(200, raw, 'image/png', version)
                elif path.startswith('/api/exports/'):
                    parts = path.split('/')
                    if len(parts) != 5 or len(parts[3]) != 32 or any(c not in '0123456789abcdef' for c in parts[3]) or parts[4] not in ('map.png', 'record.json', 'report.json', 'sha256.json'):
                        raise ValueError('导出路径无效')
                    self.respond(200, (manager.root/'exports'/parts[3]/parts[4]).read_bytes(), 'application/octet-stream')
                else: self.respond(404, dict(error='not found'))
            except (ValueError, KeyError, FileNotFoundError) as exc: self.respond(400, dict(error=str(exc)))
            except RuntimeError as exc: self.respond(503, dict(error=str(exc)))
        def do_POST(self):
            try:
                origin = self.headers.get('Origin')
                if origin and origin != 'http://'+self.headers.get('Host', ''):
                    self.respond(403, dict(error='cross-origin request')); return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192: raise ValueError('请求长度无效')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict): raise ValueError('请求需为对象')
                path = urlparse(self.path).path
                if path not in ('/api/start', '/api/finish', '/api/cancel', '/api/export', '/api/refresh', *('/api/'+x for x in manager.management_actions)):
                    self.respond(404, dict(error='not found')); return
                action = path.rsplit('/', 1)[1]
                if action == 'refresh' and 'management_action' in value:
                    action = value.pop('management_action')
                    if action not in manager.management_actions: raise ValueError('未知地图管理操作')
                self.respond(202, manager.submit(action, value))
            except (ValueError, TypeError, KeyError) as exc: self.respond(400, dict(error=str(exc)))
            except RuntimeError as exc: self.respond(409, dict(error=str(exc)))

    server = ThreadingHTTPServer((host, port), Handler); server.daemon_threads = True
    return server


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('mock', 'hardware'), default='mock')
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=18781)
    parser.add_argument('--rosbridge-host', default='192.168.64.20'); parser.add_argument('--rosbridge-port', type=int, default=9090)
    parser.add_argument('--data-dir', default=str(Path.home()/'ras_keenon_ws/run/mapping'))
    cfg = parser.parse_args(args)
    manager = Mapping(Path(cfg.data_dir)/cfg.backend, cfg.backend, cfg.rosbridge_host, cfg.rosbridge_port)
    server = None; done = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM): signal.signal(s, lambda *_:done.set())
    try:
        server = create_server(manager, cfg.host, cfg.port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'Manual mapping {cfg.backend}: http://{cfg.host}:{cfg.port}', flush=True)
        done.wait()
    finally:
        if server: server.shutdown(); server.server_close()
        manager.close()


if __name__ == '__main__': main()
