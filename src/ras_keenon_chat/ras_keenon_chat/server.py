"""Local web console and JSON API; API keys stay in backend memory."""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
import urllib.request
import urllib.error
from urllib.parse import unquote,urlparse

from ras_keenon_demos import tool_schemas
from ras_keenon_demos.transport import request_json
from .engine import ChatEngine
from .store import Store
from .navigation import TOOLS as NAVIGATION_TOOLS


def create_server(engine,host='127.0.0.1',port=8770,controller_url='http://127.0.0.1:8765',battery_reader=None):
    static=Path(__file__).with_name('static')
    def navigation(path, data=None):
        if not engine.navigation:raise RuntimeError('本地导航服务未配置')
        suffix=path[len('/api/navigation/'):]
        allowed={'state','health','map','map.png','exports','region','preview','start','control','manual-preempt','waypoints','waypoints/remove','routes','save','load'}
        parts=suffix.split('/')
        export=(len(parts)==3 and parts[0]=='exports' and parts[1] and all(c in '0123456789abcdef_' for c in parts[1]) and parts[2] in {'map.pgm','map.yaml','map.json','exploration_report.json','sha256.json'})
        if suffix not in allowed and not export:raise ValueError('未知导航路径')
        req=urllib.request.Request(engine.navigation.url+'/api/'+suffix,
            None if data is None else json.dumps(data,allow_nan=False).encode(),{'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=8) as response:
                body=response.read(20_000_001)
                if len(body)>20_000_000:raise ValueError('导航回执超过大小上限')
                return response.status,body,response.headers.get('Content-Type','application/json')
        except urllib.error.HTTPError as exc:return exc.code,exc.read(65536),'application/json'

    def battery():
        if engine.public_config()['mode']=='demo':
            return dict(status='mock',percentage=None,updated_at=None,age_s=None,quality='mock',error='')
        try:
            value=battery_reader() if battery_reader else request_json(controller_url.rstrip('/')+'/battery',timeout=2.)
            if not isinstance(value,dict) or 'status' not in value:raise ValueError('控制器未提供电量接口。')
            return value
        except Exception as exc:
            return dict(status='unavailable',percentage=None,updated_at=None,age_s=None,
                        quality='unknown',error='无法读取控制器电量：'+str(exc))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,value,kind='application/json'):
            body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):
            path=urlparse(self.path).path
            try:
                assets={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css'),'/battery.js':('battery.js','text/javascript'),'/navigation':('navigation.html','text/html'),'/navigation.js':('navigation.js','text/javascript'),'/navigation.css':('navigation.css','text/css')}
                if path in assets:
                    name,kind=assets[path];self.respond(200,(static/name).read_bytes(),kind)
                elif path.startswith('/api/navigation/'):
                    status,body,kind=navigation(path);self.respond(status,body,kind)
                elif path=='/api/config':self.respond(200,engine.public_config())
                elif path=='/api/battery':self.respond(200,battery())
                elif path=='/api/sessions':self.respond(200,engine.store.sessions())
                elif path.startswith('/api/sessions/'):
                    self.respond(200,engine.store.session(unquote(path[14:])))
                elif path=='/api/tools':self.respond(200,([] if engine.navigation_only else tool_schemas())+(engine.navigation.schemas() if engine.navigation else []))
                elif path=='/api/health':self.respond(200,dict(service='ras_keenon_chat',protocol_version=1))
                else:self.respond(404,{'error':'not found'})
            except KeyError:self.respond(404,{'error':'对话不存在。'})
            except Exception as exc:self.respond(500,{'error':str(exc)})
        def do_POST(self):
            try:
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers.get('Host',''):
                    self.respond(403,{'error':'cross-origin request'});return
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=65536:raise ValueError('请求体大小无效。')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('请求必须是对象。')
                if self.path.startswith('/api/navigation/'):
                    status,body,kind=navigation(urlparse(self.path).path,data);self.respond(status,body,kind)
                elif self.path=='/api/config':self.respond(200,engine.configure(data))
                elif self.path=='/api/sessions':self.respond(200,engine.store.create_session())
                elif self.path=='/api/chat':self.respond(200,engine.send(data['session_id'],data['message']))
                elif self.path=='/api/cancel':self.respond(200,engine.cancel(data['turn_id']))
                elif self.path=='/api/stop':
                    result=engine.stop()
                    if data.get('session_id'):
                        engine.store.session(data['session_id'])
                        engine.store.receipt(data['session_id'],None,'control','failed' if result['errors'] else 'succeeded',
                                             '停止指令回执',result)
                    self.respond(200,result)
                else:self.respond(404,{'error':'not found'})
            except KeyError as exc:self.respond(400,{'error':'缺少字段或对话不存在：'+str(exc)})
            except (ValueError,TypeError) as exc:self.respond(400,{'error':str(exc)})
            except RuntimeError as exc:self.respond(409,{'error':str(exc)})
            except Exception as exc:self.respond(500,{'error':str(exc)})
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    return server


def main(args=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=8770)
    parser.add_argument('--data-dir',default=str(Path.home()/'ras_keenon_ws/run/chat'))
    parser.add_argument('--action-url',default=None)
    parser.add_argument('--controller-url',default='http://127.0.0.1:8765')
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--navigation-url',default=None)
    parser.add_argument('--navigation-only',action='store_true')
    cfg=parser.parse_args(args)
    path=Path(cfg.data_dir).expanduser();path.mkdir(parents=True,exist_ok=True)
    store=Store(path/'chat.sqlite3')
    overrides={'action_url':cfg.action_url}
    if cfg.demo:overrides['mode']='demo'
    engine=ChatEngine(store,overrides,navigation_url=cfg.navigation_url,navigation_only=cfg.navigation_only)
    done=threading.Event();server=None
    def signal_stop(signum,frame):done.set()
    signal.signal(signal.SIGINT,signal_stop);signal.signal(signal.SIGTERM,signal_stop)
    try:
        server=create_server(engine,cfg.host,cfg.port,controller_url=cfg.controller_url)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        print(f'Keenon chat: http://{cfg.host}:{server.server_port} · mode={engine.public_config()["mode"]}',flush=True)
        done.wait()
    finally:
        if server:server.shutdown();server.server_close()
        engine.close();store.close()


if __name__=='__main__':main()
