#!/usr/bin/env python3
"""Full ROS mock stack + local fake LLM API. No cloud API and no hardware SDK."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from test_chat import Harness,request,wait

root=Path(os.environ.get('KEENON_TEST_WORKSPACE',str(Path.home()/'ras_keenon_ws')));output=root/'verification';output.mkdir(exist_ok=True)
h=Harness();h.behavior='multiple';checks=[];process=None;log=None
port=18870;action_port=18876;controller_port=18875
url='http://127.0.0.1:'+str(port)
config_env=os.environ.copy();config_env.update(LLM_BASE_URL=f'http://127.0.0.1:{h.llm.server_port}/v1',
                                               LLM_MODEL='fake-model',LLM_API_KEY='test-api-secret')


def check(name,value):
    assert value,name
    checks.append(name);print('PASS',name,flush=True)


def launch(data_dir,*args):
    global process,log
    log=(output/'chat_ros_launch.log').open('a')
    process=subprocess.Popen(['ros2','launch','ras_keenon_chat','chat.launch.py','bind:=127.0.0.1',
        'port:='+str(port),'controller_port:='+str(controller_port),'action_port:='+str(action_port),
        'data_dir:='+str(data_dir),*args],env=config_env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    def ready():
        try:return request(url+'/api/health')
        except OSError:return False
    wait(ready,15)


def stop():
    global process,log
    if process and process.poll() is None:os.killpg(process.pid,signal.SIGINT)
    if process:
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=8)
        assert process.returncode==0,'launch exit failed'
    if log:log.close()
    process=None;log=None


(output/'chat_ros_launch.log').write_text('')
try:
    with tempfile.TemporaryDirectory(prefix='keenon-chat-') as directory:
        launch(directory,'start_actions:=true','backend:=mock')
        def controller_ready():
            try:return request(f'http://127.0.0.1:{controller_port}/readings')
            except OSError:return False
        wait(controller_ready,15)
        check('controller is mock',request(f'http://127.0.0.1:{controller_port}/readings')['backend']=='mock')
        cfg=request(url+'/api/config')
        check('compatible model configuration loaded',cfg['model']=='fake-model' and cfg['key_configured'])
        check('key absent from public configuration','api_key' not in cfg)
        sid=request(url+'/api/sessions',{})['id']
        tid=request(url+'/api/chat',dict(session_id=sid,message='请先挥手再转头'))['turn_id']
        wait(lambda:request(url+'/api/sessions/'+sid)['turns'][-1]['state']=='completed',15)
        session=request(url+'/api/sessions/'+sid)
        actions=[r for r in session['receipts'] if r['kind']=='action']
        check('two presets execute sequentially',[r['state'] for r in actions]==['succeeded','succeeded'])
        check('actions used actual ROS mock controller',all(r['data']['result']['result']['backend']=='mock' for r in actions))
        llms=[r for r in session['receipts'] if r['kind']=='llm']
        check('model response usage and ids recorded',len(llms)==2 and all(r['data']['usage']['total_tokens']==20 for r in llms))
        check('assistant reply and user text saved',len(session['messages'])==3)
        check('reply sent tool_call_id to model',h.requests[-1]['messages'][-1]['role']=='tool')
        h.behavior='late';h.started.clear();h.late.clear()
        cancelled=request(url+'/api/chat',dict(session_id=sid,message='请挥手'))['turn_id']
        assert h.started.wait(3)
        request(url+'/api/cancel',dict(turn_id=cancelled));h.late.set();time.sleep(.4)
        later=request(url+'/api/sessions/'+sid)
        check('late model reply cannot dispatch after cancellation',not any(r['kind']=='action' and r['turn_id']==cancelled for r in later['receipts']))
        check('cancelled turn visible',later['turns'][-1]['state']=='cancelled')
        h.behavior='error'
        request(url+'/api/chat',dict(session_id=sid,message='测试错误回执'))
        wait(lambda:request(url+'/api/sessions/'+sid)['turns'][-1]['state']=='failed')
        failure=request(url+'/api/sessions/'+sid)
        check('API error visible and key redacted','[REDACTED]' in str(failure) and 'test-api-secret' not in str(failure))
        expected_count=len(failure['messages']);before=len(h.requests)
        stop()
        launch(directory)
        restored=request(url+'/api/sessions/'+sid)
        check('history survives server restart',len(restored['messages'])==expected_count)
        check('restart does not replay model calls',len(h.requests)==before)
        stop()
        launch(directory,'demo:=true','start_actions:=true','backend:=mock')
        demo_sid=request(url+'/api/sessions',{})['id']
        request(url+'/api/chat',dict(session_id=demo_sid,message='挥手再下蹲'))
        wait(lambda:request(url+'/api/sessions/'+demo_sid)['turns'][-1]['state']=='completed')
        demo=request(url+'/api/sessions/'+demo_sid)
        check('demo does not invoke model endpoint',len(h.requests)==before)
        check('demo actions explicitly labelled',all(r['data']['mode']=='demo' for r in demo['receipts'] if r['kind']=='action'))
        stop()
    check('clean launch shutdown','Traceback' not in (output/'chat_ros_launch.log').read_text())
    report=dict(checks=checks,hardware_motion_tested=False,real_cloud_api_tested=False,
                model_transport='local fake OpenAI-compatible API',clean_shutdown=True)
    (output/'chat_ros_report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
finally:
    h.late.set();stop();h.close()
