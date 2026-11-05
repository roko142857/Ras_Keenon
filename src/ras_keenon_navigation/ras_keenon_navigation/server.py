"""Loopback API for mock exploration or an explicit factory read-only viewer."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
from urllib.parse import urlparse
from .manager import Manager, png


def create_server(manager, host='127.0.0.1', port=18780):
    if host not in ('127.0.0.1','localhost','::1'):raise ValueError('本地部署仅允许回环监听')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,value,kind='application/json',etag=None):
            body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            if etag:self.send_header('ETag',etag)
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):
            try:
                path=urlparse(self.path).path
                if path=='/api/health':self.respond(200,dict(service='ras_keenon_navigation',backend=manager.kind if hasattr(manager,'kind') else 'mock',local_only=not hasattr(manager,'kind'),read_only=hasattr(manager,'kind'),robot_connection=getattr(manager,'connected',False)))
                elif path=='/api/state':self.respond(200,manager.snapshot())
                elif path in ('/api/map','/api/map.png'):
                    with manager.lock:g=manager.map_grid() if hasattr(manager,'map_grid') else manager.backend.snapshot()['grid']
                    if path.endswith('.png'):self.respond(200,png(g),'image/png',g.version)
                    else:self.respond(200,dict(**g.meta(),data=g.data.reshape(-1).tolist()))
                elif path=='/api/exports':
                    result=[]
                    for p in sorted((manager.root/'maps').glob('*/exploration_report.json'),reverse=True):
                        v=json.loads(p.read_text());result.append(dict(id=p.parent.name,map=v['map'],simulated=v['simulated'],native_resume_available=False))
                    self.respond(200,result)
                elif path.startswith('/api/exports/'):
                    parts=path.split('/');allowed={'map.pgm','map.yaml','map.json','exploration_report.json','sha256.json'}
                    if len(parts)!=5 or parts[4] not in allowed or not parts[3] or any(c not in '0123456789abcdef_' for c in parts[3]):raise ValueError('导出路径无效')
                    p=manager.root/'maps'/parts[3]/parts[4]
                    self.respond(200,p.read_bytes(),'application/octet-stream')
                else:self.respond(404,dict(error='not found'))
            except RuntimeError as exc:self.respond(503,dict(error=str(exc)))
            except (ValueError,KeyError,FileNotFoundError) as exc:self.respond(400,dict(error=str(exc)))
            except Exception as exc:self.respond(500,dict(error=str(exc)))
        def do_POST(self):
            try:
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers.get('Host',''):self.respond(403,dict(error='cross-origin request'));return
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=65536:raise ValueError('请求大小无效')
                v=json.loads(self.rfile.read(size))
                if not isinstance(v,dict):raise ValueError('请求需为对象')
                path=urlparse(self.path).path
                if hasattr(manager,'kind') and path!='/api/save':manager.denied()
                if path=='/api/region':result=manager.configure_region(v.get('boundary'),v.get('exclusions'))
                elif path=='/api/preview':result=manager.preview()
                elif path=='/api/start':result=manager.start(v)
                elif path=='/api/control':result=manager.control(v['action'])
                elif path=='/api/manual-preempt':
                    with manager.lock:
                        result=manager.control('pause') if manager.task and manager.task['state'] in ('exploring','navigating') else dict(paused=False)
                        manager.emit('本地手动接管事件；未连接硬件')
                elif path=='/api/waypoints':result=manager.waypoint(v)
                elif path=='/api/waypoints/remove':result=manager.delete_waypoint(v['id'])
                elif path=='/api/routes':result=manager.save_route(v['name'],v['steps'])
                elif path=='/api/save':result=manager.save_map()
                elif path=='/api/load':result=manager.load_map(v['id'])
                else:self.respond(404,dict(error='not found'));return
                self.respond(200,result)
            except (ValueError,KeyError,TypeError) as exc:self.respond(400,dict(error=str(exc)))
            except RuntimeError as exc:self.respond(409,dict(error=str(exc)))
            except Exception as exc:self.respond(500,dict(error=str(exc)))
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    return server


def main(args=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=18780)
    parser.add_argument('--data-dir',default=str(Path.home()/'ras_keenon_ws/run/local_exploration/navigation'))
    parser.add_argument('--backend',choices=['mock','native-readonly'],default='mock')
    parser.add_argument('--rosbridge-host',default=None);parser.add_argument('--rosbridge-port',type=int,default=9091)
    cfg=parser.parse_args(args)
    if cfg.backend=='native-readonly':
        if not cfg.rosbridge_host:parser.error('原厂只读模式需要显式指定 --rosbridge-host')
        from .live import LiveView
        manager=LiveView(cfg.data_dir,cfg.rosbridge_host,cfg.rosbridge_port)
    else:manager=Manager(cfg.data_dir)
    server=None;done=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:done.set())
    try:
        server=create_server(manager,cfg.host,cfg.port)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        print(f'Navigation {cfg.backend}: http://127.0.0.1:{server.server_port}',flush=True)
        done.wait()
    finally:
        if server:server.shutdown();server.server_close()
        manager.close()


if __name__=='__main__':main()
