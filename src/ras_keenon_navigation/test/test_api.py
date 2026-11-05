import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
from ras_keenon_navigation.manager import Manager
from ras_keenon_navigation.server import create_server
from ras_keenon_chat.engine import ChatEngine
from ras_keenon_chat.server import create_server as chat_server
from ras_keenon_chat.store import Store


def request(url,data=None,origin=None):
    headers={'Content-Type':'application/json'}
    if origin:headers['Origin']=origin
    req=urllib.request.Request(url,None if data is None else json.dumps(data).encode(),headers)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as response:
        raw=response.read()
        return raw if response.headers.get('Content-Type','').startswith('image/') else json.loads(raw)


def wait(predicate,timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.02)
    raise AssertionError('condition not met')


class API(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.manager=Manager(self.root/'nav',autostep=False);self.nav=create_server(self.manager,'127.0.0.1',0)
        self.navurl=f'http://127.0.0.1:{self.nav.server_port}'
        self.store=Store(self.root/'chat.sqlite3');self.engine=ChatEngine(self.store,dict(mode='demo'),navigation_url=self.navurl,navigation_only=True)
        self.web=chat_server(self.engine,'127.0.0.1',0);self.weburl=f'http://127.0.0.1:{self.web.server_port}'
        self.threads=[]
        for server in (self.nav,self.web):
            t=threading.Thread(target=server.serve_forever,daemon=True);t.start();self.threads.append(t)
        original=socket.create_connection;self.connections=[]
        def local_only(address,*args,**kwargs):
            self.assertIn(address[0],('127.0.0.1','localhost','::1'),'non-loopback connection attempted')
            self.connections.append(address)
            return original(address,*args,**kwargs)
        self.guard=patch('socket.create_connection',local_only);self.guard.start()
    def tearDown(self):
        self.engine.close();self.manager.close()
        for server in (self.web,self.nav):server.shutdown();server.server_close()
        for t in self.threads:t.join(2)
        self.store.close();self.guard.stop();self.tmp.cleanup()
    def status(self):return request(self.weburl+'/api/navigation/state')
    def session(self):return request(self.weburl+'/api/sessions',{})['id']
    def finish(self,sid):return wait(lambda: self.store.session(sid) if not self.engine.active else None)
    def test_map_png_proxy_tools_and_no_robot_backend(self):
        self.assertTrue(self.status()['local_only']);self.assertFalse(self.status()['robot_connection'])
        self.assertTrue(request(self.weburl+'/api/navigation/map.png').startswith(b'\x89PNG'))
        tools=request(self.weburl+'/api/tools');self.assertEqual(len(tools),6);self.assertTrue(all(t['name'].startswith('navigation_') for t in tools))
        for port in self.connections:self.assertIn(port[0],('127.0.0.1','localhost','::1'))
        with self.assertRaises(ValueError):create_server(self.manager,'0.0.0.0',0)
    def test_api_lifecycle_idempotency_and_pause(self):
        a=request(self.weburl+'/api/navigation/start',dict(request_id='one',mode='explore'))
        b=request(self.weburl+'/api/navigation/start',dict(request_id='one',mode='explore'));self.assertEqual(a['task']['id'],b['task']['id'])
        self.manager.step();request(self.weburl+'/api/navigation/manual-preempt',{})
        self.assertEqual(self.status()['task']['state'],'paused');self.assertIsNone(self.manager.backend.goal)
        request(self.weburl+'/api/navigation/control',dict(action='finish'))
        self.assertEqual(self.status()['task']['state'],'partial')
        exports=request(self.weburl+'/api/navigation/exports');self.assertEqual(len(exports),1)
        request(self.weburl+'/api/navigation/load',dict(id=exports[0]['id']))
    def test_cross_origin_invalid_region_and_proxy_path_are_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as err:request(self.weburl+'/api/navigation/start',dict(request_id='bad'),origin='http://untrusted.test')
        self.assertEqual(err.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):request(self.weburl+'/api/navigation/region',dict(boundary=[[0,0],[1,1],[2,2]]))
        with self.assertRaises(urllib.error.HTTPError):request(self.weburl+'/api/navigation/exports/../../etc/passwd')
        self.assertIsNone(self.manager.task)
    def test_demo_chat_starts_mock_task_and_does_not_claim_finished(self):
        sid=self.session();request(self.weburl+'/api/chat',dict(session_id=sid,message='开始探索当前区域'))
        data=self.finish(sid)
        self.assertEqual(self.manager.task['state'],'exploring')
        self.assertIn('本地模拟导航接口已返回',data['messages'][-1]['content'])
        receipts=data['receipts'];self.assertTrue(any('导航接口回执' in r['title'] for r in receipts))
        request(self.weburl+'/api/chat',dict(session_id=sid,message='停止探索'));self.finish(sid)
        self.assertEqual(self.manager.task['state'],'cancelled')
    def test_compatible_model_receives_navigation_tools_and_dispatches_locally(self):
        captured=[]
        def complete(cfg,messages,tools):
            captured.append(tools)
            msg=dict(role='assistant',content='模拟任务已提交。') if messages[-1]['role']=='tool' else dict(role='assistant',content='',tool_calls=[dict(id='nav-call',type='function',function=dict(name='navigation_start_exploration',arguments='{"duration_s":10}'))])
            return dict(choices=[dict(message=msg)]),dict(demo=True)
        self.engine.completer=complete;self.engine.configure(dict(mode='api',base_url='http://127.0.0.1:1/v1',model='fake-model'))
        sid=self.session();request(self.weburl+'/api/chat',dict(session_id=sid,message='探索十秒'));self.finish(sid)
        self.assertEqual(self.manager.task['duration_s'],10)
        self.assertTrue(all(len(t)==6 for t in captured));self.assertEqual(self.manager.backend.kind,'mock')
    def test_cancel_late_model_prevents_navigation_dispatch(self):
        started=threading.Event();release=threading.Event()
        def complete(*args):
            started.set();release.wait(2)
            return dict(choices=[dict(message=dict(role='assistant',content='',tool_calls=[dict(id='late',type='function',function=dict(name='navigation_start_exploration',arguments='{}'))]))]),{}
        self.engine.completer=complete;sid=self.session();ack=request(self.weburl+'/api/chat',dict(session_id=sid,message='开始探索'))
        self.assertTrue(started.wait(1));request(self.weburl+'/api/cancel',dict(turn_id=ack['turn_id']));release.set();self.finish(sid)
        self.assertIsNone(self.manager.task)
    def test_local_navigation_mode_refuses_any_preset_robot_tool(self):
        sid=self.session()
        def complete(cfg,messages,tools):
            msg=dict(role='assistant',content='调用结束') if messages[-1]['role']=='tool' else dict(role='assistant',content='',tool_calls=[dict(id='bad',type='function',function=dict(name='robot_move_forward',arguments='{}'))])
            return dict(choices=[dict(message=msg)]),{}
        self.engine.completer=complete
        with patch('ras_keenon_chat.engine.ActionClient.call_tool',side_effect=AssertionError('must not call')) as caller:
            request(self.weburl+'/api/chat',dict(session_id=sid,message='前进'));data=self.finish(sid);caller.assert_not_called()
        self.assertTrue(any(r['state']=='failed' for r in data['receipts']))
        self.assertIsNone(self.manager.task)


if __name__=='__main__':unittest.main()
