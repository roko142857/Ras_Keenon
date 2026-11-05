#!/usr/bin/env python3
"""Run a real chat-server process against local fake LLM and fake action HTTP APIs."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from test_chat import Harness,request,wait

root=Path(__file__).resolve().parents[3];out=root/'verification';out.mkdir(exist_ok=True)
h=Harness();h.behavior='multiple';checks=[]
with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
url='http://127.0.0.1:'+str(port)
env=os.environ.copy();env.update(LLM_BASE_URL=f'http://127.0.0.1:{h.llm.server_port}/v1',
    LLM_MODEL='fake-model',LLM_API_KEY='test-api-secret')
proc=None;log=None

def check(name,value):
    assert value,name
    checks.append(name);print('PASS',name,flush=True)

def launch(directory):
    global proc,log
    log=(out/'chat_web_process.log').open('a')
    proc=subprocess.Popen([sys.executable,'-m','ras_keenon_chat.server','--host','127.0.0.1',
        '--port',str(port),'--data-dir',directory,'--action-url',f'http://127.0.0.1:{h.actions.server_port}'],
        env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    def ready():
        try:return request(url+'/api/health')
        except OSError:return False
    wait(ready,15)

def stop():
    global proc,log
    if proc and proc.poll() is None:os.killpg(proc.pid,signal.SIGINT)
    if proc:proc.wait(timeout=10);assert proc.returncode==0
    if log:log.close()
    proc=None;log=None

(out/'chat_web_process.log').write_text('')
try:
    with tempfile.TemporaryDirectory(prefix='keenon-chat-web-') as directory:
        launch(directory)
        cfg=request(url+'/api/config')
        check('API model configured; key not exposed',cfg['model']=='fake-model' and cfg['key_configured'] and 'api_key' not in cfg)
        sid=request(url+'/api/sessions',{})['id']
        tid=request(url+'/api/chat',dict(session_id=sid,message='请先挥手，再转头'))['turn_id']
        wait(lambda:request(url+'/api/sessions/'+sid)['turns'][-1]['state']=='completed')
        value=request(url+'/api/sessions/'+sid)
        check('HTTP chat turn completes',value['turns'][-1]['state']=='completed')
        actions=[r for r in value['receipts'] if r['kind']=='action']
        check('two preset actions succeed sequentially',len(actions)==2 and all(r['state']=='succeeded' for r in actions))
        llms=[r for r in value['receipts'] if r['kind']=='llm']
        check('real HTTP usage and receipt ids persisted',len(llms)==2 and sum(r['data']['usage']['total_tokens'] for r in llms)==40 and llms[0]['data']['request_id']=='req-test-001')
        check('tool results returned to model',h.requests[-1]['messages'][-1]['role']=='tool')
        h.behavior='late';h.started.clear();h.late.clear()
        tid=request(url+'/api/chat',dict(session_id=sid,message='请挥手'))['turn_id'];assert h.started.wait(2)
        request(url+'/api/cancel',dict(turn_id=tid));h.late.set();time.sleep(.3)
        data=request(url+'/api/sessions/'+sid)
        check('late reply cannot execute a cancelled turn',data['turns'][-1]['state']=='cancelled' and not any(r['kind']=='action' and r['turn_id']==tid for r in data['receipts']))
        h.behavior='error';request(url+'/api/chat',dict(session_id=sid,message='测试错误'))
        wait(lambda:request(url+'/api/sessions/'+sid)['turns'][-1]['state']=='failed')
        data=request(url+'/api/sessions/'+sid)
        check('error receipt redacts key','test-api-secret' not in str(data) and '[REDACTED]' in str(data))
        count=len(data['messages']);sent=len(h.requests)
        stop();launch(directory)
        check('history and receipts survive process restart',len(request(url+'/api/sessions/'+sid)['messages'])==count)
        check('restart never replays model requests',len(h.requests)==sent)
        request(url+'/api/config',dict(mode='demo'))
        sid2=request(url+'/api/sessions',{})['id']
        request(url+'/api/chat',dict(session_id=sid2,message='请下蹲再站起'))
        wait(lambda:request(url+'/api/sessions/'+sid2)['turns'][-1]['state']=='completed')
        check('demo mode does not call model endpoint',len(h.requests)==sent)
        result=request(url+'/api/stop',dict(session_id=sid2))
        check('stop receipt recorded',request(url+'/api/sessions/'+sid2)['receipts'][-1]['kind']=='control' and result['stopped'])
        stop()
        for file in Path(directory).iterdir():
            if file.is_file():assert b'test-api-secret' not in file.read_bytes()
        check('key absent from on-disk database',True)
    check('clean process shutdown','Traceback' not in (out/'chat_web_process.log').read_text())
    report=dict(checks=checks,hardware_motion_tested=False,real_cloud_api_tested=False,
                transport='OpenAI-compatible local fake HTTP API',clean_shutdown=True)
    (out/'chat_web_report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
finally:
    h.late.set();stop();h.close()
