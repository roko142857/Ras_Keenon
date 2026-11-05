import os,time,json,subprocess,tempfile,signal,urllib.request
from pathlib import Path
root=Path(os.environ.get('KEENON_TEST_WORKSPACE',str(Path.home()/'ras_keenon_ws')));out=root/'verification';checks=[];procs=[];logs=[]
control='http://127.0.0.1:18785';chat='http://127.0.0.1:18787'
def request(url,data=None):
 q=urllib.request.Request(url,None if data is None else json.dumps(data).encode(),{'Content-Type':'application/json'})
 with urllib.request.urlopen(q,timeout=4) as r:return json.load(r)
def wait(url):
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  try:return request(url)
  except OSError:time.sleep(.1)
 raise AssertionError('not ready: '+url)
def check(label,value):
 assert value,label
 checks.append(label);print('PASS',label,flush=True)
with tempfile.TemporaryDirectory(prefix='keenon-battery-ros-') as directory:
 try:
  for name,args in [('control',['ros2','launch','ras_keenon_commands','direct.launch.py','backend:=mock','port:=18785','bind:=127.0.0.1','namespace:=battery_test']),('chat',['ros2','launch','ras_keenon_chat','chat.launch.py','port:=18787','bind:=127.0.0.1','controller_url:='+control,'data_dir:='+directory])]:
   log=(out/('battery_ros_'+name+'_20261103.log')).open('w');logs.append(log);procs.append(subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
  wait(control+'/readings');wait(chat+'/api/health')
  before=request(control+'/readings');check('controller is mock',before['backend']=='mock')
  v=request(control+'/battery');check('manual mock battery is not fake zero',v['status']=='mock' and v['percentage'] is None)
  check('no fabricated adapter state in mock',v['power']['adapter_connected'] is None)
  check('readings exposes battery snapshot',before['battery']['status']=='mock')
  with urllib.request.urlopen(control) as r:check('manual battery UI delivered',b'batteryWidget' in r.read())
  with urllib.request.urlopen(control+'/battery.js') as r:check('manual battery JS delivered',r.status==200 and b'mountBatteryWidget' in r.read())
  check('chat controller URL routed correctly',request(chat+'/api/battery')['status']=='mock')
  with urllib.request.urlopen(chat) as r:check('chat battery UI delivered',b'batteryWidget' in r.read())
  with urllib.request.urlopen(chat+'/battery.js') as r:check('chat battery JS delivered',r.status==200 and b'mountBatteryWidget' in r.read())
  check('battery query needs no model key',request(chat+'/api/config')['key_configured'] is False)
  for _ in range(20):request(control+'/battery');request(chat+'/api/battery')
  after=request(control+'/readings')
  check('battery polls do not issue robot commands',after['pressed']==[] and after['base_output']==[0.,0.] and after['body_command'] is None and after['body']==before['body'])
  check('control loop continues during battery queries',after['cycle']['ticks']>before['cycle']['ticks'])
  request(chat+'/api/config',{'mode':'demo'});v=request(chat+'/api/battery');check('demo explicitly has no real battery',v['status']=='mock' and v['percentage'] is None)
 finally:
  for p in procs:
   if p.poll() is None:os.killpg(p.pid,signal.SIGINT)
  for p in procs:
   try:p.wait(timeout=15)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=5)
  for log in logs:log.close()
 check('clean ROS shutdown',all(p.returncode==0 for p in procs))
 report=dict(checks=checks,distribution=os.environ.get('ROS_DISTRO','unknown'),backend='mock',hardware_motion_sent=False)
 (out/'battery_ros_report_20261103.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
