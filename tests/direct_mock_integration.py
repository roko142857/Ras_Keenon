#!/usr/bin/env python3
"""Robot-side ROS + HTTP integration. All launches explicitly use mock, no SDK/CAN."""
import json
import fcntl
import pty
import select
import termios
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request
import uuid

import rclpy
from ras_keenon_command_interfaces.srv import RunCommand

root = Path.home()/'ras_keenon_ws'
logs = root/'verification';logs.mkdir(exist_ok=True)
port = int(os.environ.get('KEENON_TEST_PORT', '18765'))
url = 'http://127.0.0.1:'+str(port)
identity = str(uuid.uuid4())
sequence = 0
checks = []

def get(path):
    with urllib.request.urlopen(url+path, timeout=2) as r:return json.load(r)

def post(path, data):
    req=urllib.request.Request(url+path,json.dumps(data).encode(),{'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=2) as r:return json.load(r)

def keys(value):
    global sequence
    sequence+=1
    return post('/keys', dict(keys=list(value), client=identity, seq=sequence))

def wait(check, timeout=3):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            value=check()
            if value:return value
        except (OSError, ValueError):pass
        time.sleep(.01)
    raise AssertionError('condition timed out')

def check(name, condition):
    assert condition,name
    checks.append(name);print('PASS',name,flush=True)


def command(text):
    reply=post('/command',dict(instruction=text))
    result=wait(lambda: (r if r['done'] else None) if (r:=get('/result/'+reply['id'])) else None,5)
    check('command '+text, result['success'])
    return result

log=(logs/'direct_mock_launch.log').open('w')
proc=subprocess.Popen(['ros2','launch','ras_keenon_control','manual.launch.py',
    'backend:=mock','speed:=0.02','turn_speed:=0.02','port:='+str(port),'bind:=127.0.0.1'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
node=None
try:
    wait(lambda:get('/readings'),15)
    check('mock backend',get('/readings')['backend']=='mock')
    for key, expected in [('w',[.02,0]),('s',[-.02,0]),('a',[0,.02]),('d',[0,-.02])]:
        keys(key);wait(lambda:get('/readings')['base_output']==expected)
        keys('');wait(lambda:get('/readings')['base_output']==[0.,0.])
        check('press/release '+key,True)
    started=time.monotonic()
    while time.monotonic()-started<3.1:keys('wa');time.sleep(.09)
    check('held input continues beyond 2 seconds',get('/readings')['base_output']==[.02,.02])
    start=time.monotonic();keys('');wait(lambda:get('/readings')['base_output']==[0.,0.])
    release_ms=(time.monotonic()-start)*1000
    check('base release observed within 150 ms',release_ms<150)
    sequence+=1
    post('/keys',dict(keys=['w'],client=identity,seq=sequence-2))
    time.sleep(.05);check('old keydown after release ignored',get('/readings')['base_output']==[0.,0.])
    keys('w');wait(lambda:get('/readings')['base_output']==[.02,0.])
    time.sleep(.4);check('heartbeat loss stops output',get('/readings')['base_output']==[0.,0.])
    for key, direction in [('h',1),('l',-1),('i',1),('k',-1)]:
        field='yaw_deg' if key in 'hl' else 'pitch_deg'
        initial=get('/readings')['body'][field]
        for _ in range(3):keys(key);time.sleep(.08)
        keys('');time.sleep(.1);held=get('/readings')['body'][field];time.sleep(.1)
        check('head press/release '+key,(held-initial)*direction>0 and held==get('/readings')['body'][field])
    command('h -10 0');command('v -3 0');command('s 0.03 +');command('sd +');command('stand')
    command('arm left 0 0 0 .1 0 0 0 .1')
    command('base .02 -.02 .15')
    check('duration ends with zero',wait(lambda:get('/readings')['base_output']==[0.,0.]))
    old=post('/command',dict(instruction='h -8 5'))
    time.sleep(.1);command('v 2 0')
    check('new instruction cancels prior wait',not get('/result/'+old['id'])['success'])
    command('stop')
    rclpy.init();node=rclpy.create_node('direct_mock_client')
    client=node.create_client(RunCommand,'/ras_keenon_commands/execute')
    check('ROS execute endpoint',client.wait_for_service(timeout_sec=5))
    request=RunCommand.Request();request.request_id=str(uuid.uuid4());request.instruction='head -2 1 0'
    future=client.call_async(request);rclpy.spin_until_future_complete(node,future,timeout_sec=5)
    check('ROS command has no enable/epoch/header requirement',future.done() and future.result().success)
    readings=get('/readings');check('no robot readiness/enable state in response',not any(k in readings for k in ('enabled','base_ready','epoch','source')))
    one=subprocess.run(['ros2','launch','ras_keenon_control','command.launch.py',
        'backend:=mock','namespace:=oneshot_mock_test','command:=h -10 0'],capture_output=True,text=True,timeout=20)
    (logs/'command_oneshot_mock.log').write_text(one.stdout+one.stderr)
    check('parameterized launch one-shot',one.returncode==0 and 'OK h -10 0' in one.stdout)
    master, slave = pty.openpty()
    initial_terminal = termios.tcgetattr(slave)
    def terminal_session():
        os.setsid();fcntl.ioctl(slave,termios.TIOCSCTTY,0)
    terminal=subprocess.Popen(['ros2','launch','ras_keenon_control','command.launch.py',
        'start_controller:=false'],stdin=slave,stdout=slave,stderr=slave,preexec_fn=terminal_session)
    os.close(slave);output=b''
    def see(text):
        global output
        if select.select([master],[],[],.02)[0]:
            try:output+=os.read(master,65536)
            except OSError:pass
        return text.encode() in output
    try:
        wait(lambda:see('ras>'),10)
        os.write(master,b'h -6 0\n');wait(lambda:see('OK h -6 0'),5)
        os.write(master,b'base .02 0 5\n');wait(lambda:get('/readings')['base_output']==[.02,0.])
        os.write(master,b'q\n');terminal.wait(timeout=8)
        check('interactive terminal q stops shared controller',terminal.returncode==0 and
              wait(lambda:get('/readings')['base_output']==[0.,0.]))
        check('interactive terminal attributes preserved',termios.tcgetattr(master)[3]==initial_terminal[3])
    finally:
        if terminal.poll() is None:
            os.killpg(terminal.pid,signal.SIGINT);terminal.wait(timeout=8)
        see('unused');os.close(master)
        (logs/'command_interactive_mock.log').write_bytes(output)
    metrics=get('/readings')['cycle']
    check('input timer runs',metrics['ticks']>1000 and metrics['mean_ms']<10)
    report=dict(checks=checks,base_release_observed_ms=release_ms,cycle=metrics,hardware_motion_tested=False)
    (logs/'direct_mock_report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
finally:
    if node:node.destroy_node();rclpy.shutdown()
    if proc.poll() is None:os.killpg(proc.pid,signal.SIGINT)
    try:proc.wait(timeout=12)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=8)
    log.close()
    assert proc.returncode==0, 'launch did not exit cleanly'
    assert 'Traceback' not in (logs/'direct_mock_launch.log').read_text(), 'shutdown traceback'
