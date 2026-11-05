"""HTTP action and LLM tool adapter. Never imports or initializes the hardware SDK."""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import signal
import threading
import urllib.parse

from .catalog import SPECS,tool_schemas,decode_tool_call
from .plans import build_plan
from .runner import ActionManager
from .transport import ControllerClient


def create_server(manager, host='127.0.0.1', port=8766):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,data):
            value=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(value)));self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(value)
        def do_GET(self):
            try:
                if self.path=='/health':self.respond(200,dict(service='ras_keenon_demos',protocol_version=1))
                elif self.path=='/actions':self.respond(200,SPECS)
                elif self.path=='/tools':self.respond(200,tool_schemas())
                elif self.path.startswith('/actions/'):
                    self.respond(200,manager.status(urllib.parse.unquote(self.path[9:])))
                else:self.respond(404,{'error':'not found'})
            except KeyError:self.respond(404,{'error':'unknown action id'})
        def do_POST(self):
            try:
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers.get('Host',''):
                    self.respond(403,{'error':'cross-origin request'});return
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=16384:raise ValueError('invalid body length')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('body must be an object')
                if self.path=='/actions':
                    self.respond(200,manager.submit(data['action'],data.get('parameters'),data.get('request_id')))
                elif self.path=='/plan':
                    from .catalog import normalize
                    p=normalize(data['action'],data.get('parameters'))
                    plan=build_plan(data['action'],p,manager.controller.readings())
                    self.respond(200,plan.to_dict())
                elif self.path=='/tool_call':
                    action,p,identity=decode_tool_call(data)
                    self.respond(200,manager.submit(action,p,identity))
                elif self.path=='/stop':self.respond(200,manager.stop())
                elif self.path.startswith('/actions/') and self.path.endswith('/cancel'):
                    identity=urllib.parse.unquote(self.path[9:-7])
                    try:self.respond(200,manager.cancel(identity))
                    except KeyError:self.respond(404,{'error':'unknown action id'})
                else:self.respond(404,{'error':'not found'})
            except (ValueError,TypeError,KeyError) as exc:self.respond(400,{'error':str(exc)})
            except Exception as exc:self.respond(503,{'error':str(exc)})
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    return server


def main(args=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-url',default='http://127.0.0.1:8765')
    parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=8766)
    cfg=parser.parse_args(args)
    manager=ActionManager(ControllerClient(cfg.controller_url))
    server=None;finished=threading.Event()
    def shutdown(signum,frame):finished.set()
    signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
    try:
        server=create_server(manager,cfg.host,cfg.port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        print(f'Action API http://{cfg.host}:{server.server_port}; controller={cfg.controller_url}',flush=True)
        finished.wait()
    finally:
        manager.close()
        if server:server.shutdown();server.server_close()


if __name__=='__main__':main()
