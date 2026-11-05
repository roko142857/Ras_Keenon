#!/usr/bin/env python3
"""Launch the new demo stack with mock; exercise every standalone script and tool API."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from ament_index_python.packages import get_package_share_directory
from ras_keenon_demos import ActionClient
from ras_keenon_demos.transport import request_json

root=Path(os.environ.get('KEENON_TEST_WORKSPACE',str(Path.home()/'ras_keenon_ws')))
output=root/'verification';output.mkdir(exist_ok=True)
port=int(os.environ.get('DEMO_CONTROLLER_TEST_PORT','18775'))
apiport=int(os.environ.get('DEMO_API_TEST_PORT','18776'))
url='http://127.0.0.1:'+str(apiport)
controller='http://127.0.0.1:'+str(port)
client=ActionClient(url);checks=[]
share=Path(get_package_share_directory('ras_keenon_demos'))


def wait(check,timeout=8):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        try:
            value=check()
            if value:return value
        except (OSError,RuntimeError,ValueError):pass
        time.sleep(.02)
    raise AssertionError('condition timed out')


def check(name,condition):
    assert condition,name
    checks.append(name);print('PASS',name,flush=True)


def script(name,*args):
    completed=subprocess.run([sys.executable,str(share/'scripts'/(name+'.py')),
        '--api-url',url,*args],capture_output=True,text=True,timeout=30)
    (output/('demo_script_'+name+'.log')).write_text(completed.stdout+completed.stderr)
    check('standalone script '+name,completed.returncode==0)
    return json.loads(completed.stdout)


logpath=output/'demos_mock_launch.log'
log=logpath.open('w')
launch=subprocess.Popen(['ros2','launch','ras_keenon_demos','demos.launch.py','backend:=mock',
    'bind:=127.0.0.1','controller_port:='+str(port),'action_port:='+str(apiport),
    'namespace:=demo_integration'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
try:
    wait(lambda:request_json(url+'/health'),15)
    readings=wait(lambda:request_json(controller+'/readings'))
    check('mock backend only',readings['backend']=='mock')
    check('tool definitions',len(client.tools())==12)
    check('wave rename and new tool names',{t['name'] for t in client.tools()}.issuperset({'robot_shake_hand','robot_wave_large'}) and
          'robot_wave' not in {t['name'] for t in client.tools()})
    plan=script('shake_hand','--dry-run','--cycles','1')
    check('dry run reads but does not move',plan['action']=='shake_hand' and
          request_json(controller+'/readings')['body']==readings['body'])
    for name,args in [
        ('raise_arm',['--lift-deg','15']),
        ('shake_hand',['--lift-deg','15','--amplitude-deg','2','--cycles','1','--pause-s','0']),
        ('wave_large',['--cycles','1','--pause-s','0']),
        ('turn_head',['--yaw-deg','10','--settle-s','0','--return-to-start']),
        ('nod',['--cycles','1','--settle-s','0.02']),
        ('shake_head',['--cycles','1','--settle-s','0.02']),
        ('squat',['--height-m','0.03']),('stand',[]),
        ('move_forward',['--speed-mps','0.2','--duration-s','0.1']),
        ('rotate',['--direction','right','--speed-radps','0.3','--duration-s','0.1']),
        ('reset',['--settle-s','0']),
    ]:
        before=request_json(controller+'/readings')
        result=script(name,*args)
        check('job succeeded '+name,result['state']=='succeeded' and not result['result']['arrival_verified'])
        if name in ('shake_hand','wave_large'):
            check(name+' restores its per-run starting pose',
                request_json(controller+'/readings')['body']['right']==before['body']['right'])
        if name=='turn_head':
            check('turn_head optional return',
                request_json(controller+'/readings')['body']['yaw_deg']==before['body']['yaw_deg'])
        if name=='reset':
            body=wait(lambda:request_json(controller+'/readings')['body'])
            check('reset zero arms head with crouched waist',body['right']==body['left']==[0.]*7 and
                  body['yaw_deg']==body['pitch_deg']==0 and body['waist_height_m']==.03 and body['waist_direction']==-1)
    movement=client.submit('move_forward',{'speed_mps':.2,'duration_s':.3})
    check('speed above old cap reaches base output',wait(lambda:request_json(controller+'/readings')['base_output']==[.2,0.]))
    check('higher speed action succeeds',client.wait(movement['id'])['state']=='succeeded')
    reset=client.call_tool('robot_reset',{'settle_s':0},'llm-reset-001')
    check('reset LLM tool call completes',client.wait(reset['id'])['state']=='succeeded')
    check('base ends at zero',wait(lambda:request_json(controller+'/readings')['base_output']==[0.,0.]))
    call=client.call_tool('robot_turn_head','{"yaw_deg":-10,"settle_s":0}','llm-demo-001')
    check('LLM tool call completes',client.wait(call['id'])['state']=='succeeded')
    start=client.status(call['id'])['started_at']
    repeated=client.call_tool('robot_turn_head',{'yaw_deg':-10,'settle_s':0},'llm-demo-001')
    check('same LLM call_id not replayed',repeated['id']==call['id'] and repeated['started_at']==start)
    large=client.call_tool('robot_wave_large',{'side':'left','cycles':1,'pause_s':0},'llm-large-1')
    check('large wave left LLM tool succeeds',client.wait(large['id'])['state']=='succeeded')
    for action in ('wave_large','shake_hand'):
        run=subprocess.run(['ros2','run','ras_keenon_demos',action,'--api-url',url,'--dry-run'],
                           capture_output=True,text=True,timeout=15)
        check('ros2 run '+action+' entry point',run.returncode==0 and json.loads(run.stdout)['action']==action)
    try:client.call_tool('robot_shell',{'command':'echo should-not-run'})
    except RuntimeError:check('unknown tool rejected',True)
    else:raise AssertionError('unknown tool accepted')
    old=client.submit('shake_hand',{'lift_deg':0,'amplitude_deg':1,'pause_s':1,'cycles':5})
    wait(lambda:client.status(old['id'])['steps_completed']>=1)
    script('stop')
    cancelled=client.wait(old['id'])
    completed=cancelled['steps_completed'];time.sleep(.15)
    check('stop cancels entire sequence without remaining steps',cancelled['state']=='cancelled' and
          client.status(old['id'])['steps_completed']==completed<cancelled['steps_total'])
    old=client.submit('nod',{'cycles':10,'settle_s':.2})
    wait(lambda:client.status(old['id'])['state']=='running')
    new=client.submit('turn_head',{'yaw_deg':0,'settle_s':0})
    check('new action replaces old sequence',client.wait(old['id'])['state']=='cancelled' and
          client.wait(new['id'])['state']=='succeeded')
    console=subprocess.run(['ros2','run','ras_keenon_demos','reset','--api-url',url,'--settle-s','0'],
                           capture_output=True,text=True,timeout=15)
    (output/'demo_console_entry.log').write_text(console.stdout+console.stderr)
    check('ros2 run reset entry point',console.returncode==0)
    report=dict(checks=checks,hardware_motion_tested=False,backend='mock',
                actions=list(client.catalog()),controller=request_json(controller+'/readings'))
finally:
    if launch.poll() is None:os.killpg(launch.pid,signal.SIGINT)
    try:launch.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(launch.pid,signal.SIGTERM);launch.wait(timeout=8)
    log.close()
assert launch.returncode==0,'launch did not exit cleanly'
assert 'Traceback' not in logpath.read_text(),'shutdown traceback'
report['clean_shutdown']=True
(output/'demos_mock_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
