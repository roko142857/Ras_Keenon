"""Real HTTP protocol tests against fake LLM and fake action controller, never hardware."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request

from ras_keenon_demos.api import create_server as actions_server
from ras_keenon_demos.runner import ActionManager
from ras_keenon_chat.engine import ChatEngine
from ras_keenon_chat.provider import chat_url
from ras_keenon_chat.server import create_server
from ras_keenon_chat.store import Store


def request(url,data=None):
    req=urllib.request.Request(url,None if data is None else json.dumps(data).encode(),{'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)


def wait(check,timeout=5):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=check()
        if value:return value
        time.sleep(.01)
    raise AssertionError('condition timed out')


class FakeController:
    def __init__(self):self.commands=[];self.stops=0;self.block=False
    def readings(self):return dict(backend='mock',body=dict(yaw_deg=0.,pitch_deg=0.,right=[0.]*7,left=[0.]*7))
    def submit(self,command):self.commands.append(command);return str(len(self.commands))
    def wait(self,identity,cancel,timeout=None):
        while self.block:
            if cancel.wait(.01):raise __import__('ras_keenon_demos.transport',fromlist=['Cancelled']).Cancelled()
        return dict(done=True,success=True)
    def stop(self):self.stops+=1;return dict(ok=True)


class Harness:
    def __init__(self):
        self.requests=[];self.behavior='normal';self.late=threading.Event();self.started=threading.Event()
        harness=self
        class LLM(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                harness.requests.append(body);harness.started.set()
                if harness.behavior=='late':harness.late.wait(4)
                if harness.behavior=='error':
                    code=401;value={'error':{'message':'bad token test-api-secret'}}
                elif harness.behavior=='malformed':code=200;value={'no_choices':True}
                else:
                    code=200
                    if body['messages'][-1]['role']=='tool':
                        message=dict(role='assistant',content='工具返回已收到。')
                    elif harness.behavior=='plain':message=dict(role='assistant',content='你好，这是普通对话。')
                    else:
                        names=['robot_shake_hand','robot_turn_head'] if harness.behavior=='multiple' else ['robot_shake_hand']
                        if harness.behavior=='reset':names=['robot_reset']
                        if harness.behavior=='large_wave':names=['robot_wave_large']
                        if harness.behavior=='unknown':names=['robot_shell']
                        args='{"cycles":1,"lift_deg":0,"amplitude_deg":1,"pause_s":0}'
                        if harness.behavior=='invalid_args':args='{"cycles":true}'
                        message=dict(role='assistant',content='开始调用动作。',tool_calls=[dict(
                            id='call-'+str(i),type='function',function=dict(name=n,arguments=(
                                args if n=='robot_shake_hand' else '{"cycles":1,"pause_s":0}' if n=='robot_wave_large' else '{"settle_s":0}' if n=='robot_reset' else '{"yaw_deg":5,"settle_s":0}')))
                            for i,n in enumerate(names)])
                    value=dict(id='response-test-001',model='fake-model',choices=[dict(message=message,
                        finish_reason='tool_calls' if message.get('tool_calls') else 'stop')],
                        usage=dict(prompt_tokens=12,completion_tokens=8,total_tokens=20))
                raw=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json')
                self.send_header('x-request-id','req-test-001');self.end_headers()
                try:self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
        self.llm=ThreadingHTTPServer(('127.0.0.1',0),LLM);self.llm.daemon_threads=True
        self.controller=FakeController();self.manager=ActionManager(self.controller)
        self.actions=actions_server(self.manager,'127.0.0.1',0)
        self.threads=[]
        for s in (self.llm,self.actions):
            t=threading.Thread(target=s.serve_forever,daemon=True);t.start();self.threads.append(t)
    def close(self):
        self.late.set();self.manager.close()
        for s in (self.llm,self.actions):s.shutdown();s.server_close()
        for t in self.threads:t.join(2)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'history.sqlite3'
        self.store=Store(self.path);self.h=Harness()
        self.engine=ChatEngine(self.store,dict(mode='api',model='fake-model',base_url=f'http://127.0.0.1:{self.h.llm.server_port}/v1',
            action_url=f'http://127.0.0.1:{self.h.actions.server_port}',api_key='test-api-secret'))
        self.sid=self.store.create_session()['id']
    def tearDown(self):
        self.h.late.set();self.engine.close();self.h.close();self.store.close();self.tmp.cleanup()
    def send(self,text='请挥手'):
        turn=self.engine.send(self.sid,text)['turn_id']
        wait(lambda:self.store.session(self.sid)['turns'][-1]['state']!='running')
        return self.store.session(self.sid)

    def test_complete_function_call_loop_and_receipt_usage(self):
        result=self.send()
        self.assertEqual(result['turns'][-1]['state'],'completed')
        self.assertGreater(len(self.h.controller.commands),0)
        self.assertEqual(len(self.h.requests),2)
        self.assertFalse(self.h.requests[0]['parallel_tool_calls'])
        self.assertFalse(self.h.requests[0]['tools'][0]['function']['strict'])
        tool=self.h.requests[1]['messages'][-1]
        self.assertEqual(tool['role'],'tool');self.assertEqual(tool['tool_call_id'],'call-0')
        self.assertEqual(json.loads(tool['content'])['state'],'succeeded')
        llm=[r for r in result['receipts'] if r['kind']=='llm'][0]
        self.assertEqual(llm['data']['usage']['total_tokens'],20)
        self.assertEqual(llm['data']['request_id'],'req-test-001')
        self.assertEqual(llm['data']['http_status'],200)

    def test_multiple_calls_are_sequential_not_replacements(self):
        self.h.behavior='multiple';result=self.send()
        actions=[r for r in result['receipts'] if r['kind']=='action']
        self.assertEqual([r['state'] for r in actions],['succeeded','succeeded'])
        self.assertTrue(self.h.controller.commands[-1].startswith('head '))

    def test_plain_chat_never_dispatches_actions(self):
        self.h.behavior='plain';result=self.send('你好')
        self.assertEqual(self.h.controller.commands,[])
        self.assertEqual(result['messages'][-1]['content'],'你好，这是普通对话。')

    def test_demo_never_calls_api_or_robot(self):
        self.engine.configure({'mode':'demo'})
        result=self.send('请先挥两次手，再转头。')
        self.assertEqual(self.h.requests,[]);self.assertEqual(self.h.controller.commands,[])
        self.assertEqual(len([r for r in result['receipts'] if r['kind']=='action']),2)
        self.assertIn('实际机器人没有运动',result['messages'][-1]['content'])

    def test_demo_reset_registered_and_does_not_touch_hardware(self):
        self.engine.configure({'mode':'demo'})
        result=self.send('请复位。')
        actions=[r for r in result['receipts'] if r['kind']=='action']
        self.assertEqual(len(actions),1)
        self.assertIn('reset',json.dumps(actions))
        self.assertEqual(actions[0]['state'],'succeeded')
        self.assertEqual(self.h.controller.commands,[])

    def test_compatible_provider_reset_reaches_action_service(self):
        self.h.behavior='reset'
        result=self.send('请复位')
        actions=[r for r in result['receipts'] if r['kind']=='action']
        self.assertEqual([r['state'] for r in actions],['succeeded'])
        self.assertEqual(self.h.controller.commands, ['stop','head 0 0 0',
            'arm right 0 0 0 0 0 0 0 0.1','arm left 0 0 0 0 0 0 0 0.1','s 0.03 -'])
        schema=next(t['function'] for t in self.h.requests[0]['tools'] if t['function']['name']=='robot_reset')
        self.assertEqual(schema['parameters']['properties']['height_m']['default'],.03)

    def test_compatible_provider_large_wave_reaches_action_service(self):
        self.h.behavior='large_wave';result=self.send('请大幅挥手')
        actions=[r for r in result['receipts'] if r['kind']=='action']
        self.assertEqual([r['state'] for r in actions],['succeeded'])
        positions=[list(map(float,c.split()[2:9])) for c in self.h.controller.commands]
        self.assertTrue(any(abs(q[1])>1. for q in positions))
        self.assertTrue(any(abs(q[5])>.2 for q in positions))
        self.assertEqual(positions[-1],[0.]*7)

    def test_demo_distinguishes_large_wave_and_old_elbow_action(self):
        self.engine.configure({'mode':'demo'})
        result=self.send('请先大幅挥手，再小幅挥手。')
        actions=[r for r in result['receipts'] if r['kind']=='action']
        self.assertEqual([r['data']['name'] for r in actions],['robot_wave_large','robot_shake_hand'])
        self.assertEqual([r['state'] for r in actions],['succeeded','succeeded'])
        self.assertEqual(self.h.controller.commands,[])

    def test_unknown_tool_rejected_before_dispatch(self):
        self.h.behavior='unknown';result=self.send()
        self.assertEqual(self.h.controller.commands,[])
        self.assertEqual([r['state'] for r in result['receipts'] if r['kind']=='action'],['failed'])
        self.assertEqual(json.loads(self.h.requests[-1]['messages'][-1]['content'])['state'],'failed')

    def test_invalid_tool_parameters_rejected(self):
        self.h.behavior='invalid_args';result=self.send()
        self.assertEqual(self.h.controller.commands,[])
        self.assertIn('cycles must be integer',str(result['receipts']))

    def test_model_error_is_visible_and_key_is_redacted(self):
        self.h.behavior='error';result=self.send()
        self.assertEqual(result['turns'][-1]['state'],'failed')
        self.assertNotIn('test-api-secret',json.dumps(result))
        self.assertIn('[REDACTED]',str(result))
        self.assertEqual(result['receipts'][0]['data']['http_status'],401)
        self.assertEqual(self.h.controller.commands,[])

    def test_malformed_response_fails_without_action(self):
        self.h.behavior='malformed';result=self.send()
        self.assertEqual(result['turns'][-1]['state'],'failed')
        self.assertEqual(self.h.controller.commands,[])

    def test_cancel_late_model_prevents_late_dispatch(self):
        self.h.behavior='late';tid=self.engine.send(self.sid,'挥手')['turn_id']
        self.assertTrue(self.h.started.wait(2));self.engine.cancel(tid);self.engine.cancel(tid)
        self.h.late.set();wait(lambda:tid not in self.engine.active)
        data=self.store.session(self.sid)
        self.assertEqual(data['turns'][-1]['state'],'cancelled')
        self.assertEqual(self.h.controller.commands,[])
        self.assertEqual(len([m for m in data['messages'] if '本轮已取消' in m['content']]),1)

    def test_stop_remains_available_while_action_blocks(self):
        self.h.controller.block=True
        tid=self.engine.send(self.sid,'挥手')['turn_id']
        wait(lambda:self.h.controller.commands)
        self.engine.stop();wait(lambda:tid not in self.engine.active)
        self.assertEqual(self.store.session(self.sid)['turns'][-1]['state'],'cancelled')
        self.assertGreater(self.h.controller.stops,0)
        self.assertEqual(len(self.h.controller.commands),1)

    def test_known_key_in_message_is_redacted_before_history(self):
        self.h.behavior='plain'
        result=self.send('不要记录 test-api-secret')
        self.assertNotIn('test-api-secret',str(result))
        self.assertIn('[REDACTED]',result['messages'][0]['content'])

    def test_newline_key_rejected_without_echoing_it(self):
        bad='bad-key\nsecond-part'
        with self.assertRaises(ValueError) as caught:self.engine.configure({'api_key':bad})
        self.assertNotIn('bad-key',str(caught.exception))
        self.assertNotIn('bad-key',json.dumps(self.store.load_settings()))

    def test_api_key_not_returned_or_persisted(self):
        self.engine.configure(dict(api_key='do-not-save-this-key',mode='api',model='another-model'))
        public=self.engine.public_config();self.assertTrue(public['key_configured'])
        self.assertNotIn('api_key',public)
        self.assertNotIn('do-not-save-this-key',json.dumps(self.store.load_settings()))
        self.assertEqual(self.store.load_settings()['model'],'another-model')

    def test_no_configuration_does_not_fall_back_to_demo(self):
        self.engine.configure(dict(mode='api',base_url='',model=''))
        result=self.send()
        self.assertEqual(result['turns'][-1]['state'],'failed')
        self.assertIn('请先',result['messages'][-1]['content'])
        self.assertEqual(self.h.controller.commands,[])

    def test_history_survives_new_store_and_omits_internal_tools_in_next_turn(self):
        self.h.behavior='plain';self.send('第一条');self.send('第二条')
        messages=self.h.requests[-1]['messages']
        self.assertTrue(any(m['content']=='第一条' for m in messages))
        self.assertFalse(any(m['role']=='tool' for m in messages))
        another=Store(self.path)
        try:self.assertEqual(len(another.session(self.sid)['messages']),4)
        finally:another.close()


class StoreTests(unittest.TestCase):
    def test_restart_marks_incomplete_turn_without_replay(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'chat.db';s=Store(p);sid=s.create_session()['id'];tid=s.new_turn(sid,'挥手');s.receipt(sid,tid,'llm','running','模型请求',{});s.close()
            s=Store(p)
            try:
                data=s.session(sid)
                self.assertEqual(data['turns'][0]['state'],'interrupted')
                self.assertEqual(data['receipts'][0]['state'],'interrupted')
                self.assertIn('不会自动重发',data['messages'][-1]['content'])
            finally:s.close()
    def test_url_normalization_and_embedded_credentials_rejected(self):
        self.assertEqual(chat_url('https://example.com/v1/'),'https://example.com/v1/chat/completions')
        self.assertEqual(chat_url('http://localhost:123/v1/chat/completions'),'http://localhost:123/v1/chat/completions')
        for url in ('ftp://example.com','https://secret@example.com/v1','https://example.com/v1?key=x'):
            with self.assertRaises(ValueError):chat_url(url)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'chat.db')
        self.engine=ChatEngine(self.store,dict(mode='demo',api_key=''))
        self.battery_calls=0
        self.battery_value=dict(status='live',percentage=75.,quality='reported',updated_at=time.time(),age_s=0.)
        def read_battery():
            self.battery_calls+=1
            if isinstance(self.battery_value,Exception):raise self.battery_value
            return self.battery_value
        self.server=create_server(self.engine,'127.0.0.1',0,battery_reader=read_battery)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.engine.close();self.store.close();self.thread.join(2);self.tmp.cleanup()
    def test_static_page_has_required_regions(self):
        for path,kind in (('/','text/html'),('/app.js','text/javascript'),('/battery.js','text/javascript'),('/style.css','text/css')):
            with urllib.request.urlopen(self.url+path) as r:
                self.assertEqual(r.status,200);self.assertIn(kind,r.headers['Content-Type'])
    def test_demo_battery_does_not_query_robot(self):
        value=request(self.url+'/api/battery')
        self.assertEqual(value['status'],'mock');self.assertIsNone(value['percentage'])
        self.assertEqual(self.battery_calls,0)
    def test_api_battery_forwards_reported_value_and_unknown_quality(self):
        self.engine.configure({'mode':'api'})
        self.assertEqual(request(self.url+'/api/battery')['percentage'],75.)
        self.battery_value.update(percentage=0.,quality='unverified')
        value=request(self.url+'/api/battery')
        self.assertEqual((value['percentage'],value['quality']),(0.,'unverified'))
    def test_battery_failure_is_not_reported_as_empty_battery(self):
        self.engine.configure({'mode':'api'})
        self.battery_value=OSError('connection refused')
        value=request(self.url+'/api/battery')
        self.assertEqual(value['status'],'unavailable');self.assertIsNone(value['percentage'])
        self.assertIn('connection refused',value['error'])
    def test_session_chat_receipt_and_settings_routes(self):
        session=request(self.url+'/api/sessions',{})
        request(self.url+'/api/chat',dict(session_id=session['id'],message='请挥手'))
        wait(lambda:request(self.url+'/api/sessions/'+session['id'])['turns'][-1]['state']=='completed')
        data=request(self.url+'/api/sessions/'+session['id'])
        self.assertGreater(len(data['receipts']),2)
        self.assertEqual(request(self.url+'/api/sessions')[0]['message_count'],3)
        cfg=request(self.url+'/api/config',dict(api_key='fake-only-key'))
        self.assertTrue(cfg['key_configured']);self.assertNotIn('fake-only-key',json.dumps(cfg))
    def test_cross_origin_post_blocked(self):
        req=urllib.request.Request(self.url+'/api/sessions',b'{}',{'Content-Type':'application/json','Origin':'http://unrelated.invalid'})
        with self.assertRaises(urllib.error.HTTPError) as c:urllib.request.urlopen(req)
        self.assertEqual(c.exception.code,403)
    def test_stop_creates_visible_receipt_without_llm(self):
        session=request(self.url+'/api/sessions',{})
        request(self.url+'/api/stop',dict(session_id=session['id']))
        data=request(self.url+'/api/sessions/'+session['id'])
        self.assertEqual(data['receipts'][0]['kind'],'control')


if __name__=='__main__':unittest.main()
