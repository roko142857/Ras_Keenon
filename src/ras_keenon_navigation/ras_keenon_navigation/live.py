"""Factory read-only map viewer. Every outgoing rosbridge operation is allowlisted.

It never advertises, publishes, calls a service, cancels a goal, or initialises SDKs.
The motion-capable NativeAdapter is deliberately not used by this process.
"""
import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from .grid import Grid, finite
from .manager import atomic_json

TOPICS=('/map','/localization/robot_pose','/move_base/status','/mapping_status','/robot_state','/tf','/scan_orig')


def factory_transport(host, port):
    from ras_keenon_commands import rosbridge
    # Process-local cap for full factory maps; no other controller shares this process.
    rosbridge.MAX_FRAME=32_000_000
    return rosbridge.Rosbridge(host,port)


class ReadOnlyTransport:
    def __init__(self, inner):self.inner=inner
    def send(self, message):
        if message.get('op') not in ('subscribe','unsubscribe') or message.get('topic') not in TOPICS:
            raise RuntimeError('原厂只读通道拒绝写入或服务调用')
        self.inner.send(message)
    def receive(self):return self.inner.receive()
    def close(self):self.inner.close()


class LiveView:
    kind='native-readonly'
    def __init__(self, data_dir, host='192.168.64.20', port=9091, transport_factory=factory_transport, autostart=True, clock=time.monotonic):
        self.root=Path(data_dir);self.root.mkdir(parents=True,exist_ok=True)
        self._lease=(self.root/'manager.lock').open('a')
        try:fcntl.flock(self._lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:self._lease.close();raise RuntimeError('已有进程持有原厂地图数据目录')
        self.lock=threading.RLock();self.clock=clock;self.host=host;self.port=port;self.transport_factory=transport_factory
        self.done=threading.Event();self.connected=False;self.error='';self.closed=False;self.thread=None
        self.grid=None;self.raw_pose=None;self.pose_at=None;self.map_at=None;self.last_message=None
        self.sequence=0;self.tf={};self.scan=None;self.status=None;self.mapping_status=None;self.robot_state=None;self.trail=[]
        self.events=[];self.task=None;self.backend=self;self.map_cache=None;self.pose_header=None
        if autostart:
            self.thread=threading.Thread(target=self.run,daemon=True,name='factory-map-readonly');self.thread.start()

    def emit(self, title, **details):
        with self.lock:
            self.events.append(dict(time=time.time(),title=title,**details));self.events=self.events[-60:]

    def ingest(self, value):
        if value.get('op')!='publish':return
        topic=value.get('topic');msg=value.get('msg',{})
        with self.lock:
            now=self.clock()
            if topic=='/map':
                if msg.get('header',{}).get('frame_id')!='map':raise ValueError('原厂地图非 map 坐标系')
                info=msg['info'];origin=info['origin']
                binding=hashlib.sha256(json.dumps([info['width'],info['height'],info['resolution'],origin,info.get('map_load_time')],sort_keys=True).encode()).hexdigest()[:16]
                # A changed map frame/extent invalidates bindings. No synthetic origin is used.
                grid=Grid.from_ros(msg,'factory-'+binding)
                self.grid=grid;self.map_at=now;self.sequence+=1
                self.map_cache=None
            elif topic=='/localization/robot_pose':
                pose=msg['pose']['pose'];q=pose['orientation'];norm=sum(finite(q[k],'quaternion')**2 for k in ('x','y','z','w'))
                if abs(norm-1)>.001 or msg['header'].get('frame_id')!='map':
                    self.raw_pose=None;self.pose_at=None;raise ValueError('原厂位姿坐标系/四元数无效')
                self.raw_pose=[finite(pose['position']['x'],'x'),finite(pose['position']['y'],'y'),math.atan2(2*(q['w']*q['z']+q['x']*q['y']),1-2*(q['y']**2+q['z']**2))]
                self.pose_header=copy.deepcopy(msg['header']);self.pose_at=now
                self.pose_covariance=copy.deepcopy(msg['pose'].get('covariance'))
            elif topic=='/tf':
                for t in msg.get('transforms',[]):
                    self.tf[t['header']['frame_id']+' -> '+t['child_frame_id']]=dict(stamp=t['header'].get('stamp'),received_at=now)
            elif topic=='/move_base/status':self.status=dict(message=msg,received_at=now)
            elif topic=='/mapping_status':self.mapping_status=dict(message=msg,received_at=now)
            elif topic=='/robot_state':
                mode=msg.get('mode')
                if type(mode) is not int or not 0<=mode<=255:raise ValueError('原厂模式代码无效')
                self.robot_state=dict(message=copy.deepcopy(msg),received_at=now)
            elif topic=='/scan_orig':
                ranges=msg.get('ranges',[]);valid=[v for v in ranges if type(v) in (int,float) and math.isfinite(v) and msg.get('range_min',0)<=v<=msg.get('range_max',math.inf)]
                self.scan=dict(received_at=now,rays=len(ranges),valid_rays=len(valid),frame_id=msg.get('header',{}).get('frame_id'),source_stamp=msg.get('header',{}).get('stamp'))
            self.last_message=now

    def run(self):
        delay=.5
        while not self.done.is_set():
            ws=None
            try:
                inner=self.transport_factory(self.host,self.port)
                if hasattr(inner,'sock'):inner.sock.settimeout(.2)
                ws=ReadOnlyTransport(inner)
                for topic in TOPICS:ws.send(dict(op='subscribe',id='ras-live'+topic,topic=topic,queue_length=1,throttle_rate=500))
                with self.lock:self.connected=True;self.error=''
                self.emit('原厂只读订阅已连接');delay=.5
                while not self.done.is_set():
                    value=ws.receive()
                    if value:
                        if value.get('op')=='status' and value.get('level')=='error':raise RuntimeError(value.get('msg','订阅错误'))
                        try:self.ingest(value)
                        except (ValueError,KeyError,TypeError,OverflowError) as exc:
                            with self.lock:self.error='未采纳原厂消息：'+str(exc)
            except Exception as exc:
                with self.lock:self.error=str(exc)
            finally:
                with self.lock:self.connected=False
                if ws:
                    for topic in TOPICS:
                        try:ws.send(dict(op='unsubscribe',id='ras-live'+topic,topic=topic))
                        except Exception:pass
                    ws.close()
            if self.done.wait(delay):break
            delay=min(delay*2,5)

    def _age(self, value):return None if value is None else round(max(0.,self.clock()-value),3)

    def snapshot(self):
        with self.lock:
            meta=self.grid.meta() if self.grid else None
            pose_age=self._age(self.pose_at)
            mode=None
            if self.robot_state is not None:
                code=self.robot_state['message']['mode']
                mode=dict(code=code,label='导航模式' if code==0 else '非导航模式（具体语义待实测）',age_s=self._age(self.robot_state['received_at']))
            return dict(service='ras_keenon_navigation',backend=self.kind,local_only=False,simulated=False,
                read_only=True,robot_connection=self.connected,map=meta,pose=None,raw_pose=copy.deepcopy(self.raw_pose),
                pose_note='原厂上报位姿，定位质量/协方差语义未确认，不绘制为已验证位置。',
                sensor_age_s=self._age(self.last_message),pose_age_s=pose_age,map_age_s=self._age(self.map_at),
                map_stale=not self.connected,localization='unverified' if self.raw_pose else 'waiting',mapping=None,
                raw_mapping_status=copy.deepcopy(self.mapping_status),navigation_status=copy.deepcopy(self.status),factory_mode=mode,
                scan=copy.deepcopy(self.scan),tf_edges=copy.deepcopy(self.tf),error=self.error,
                task=None,task_historical=False,goal=None,candidates=[],path=[],trail=[],boundary=None,exclusions=[],waypoints=[],routes={},events=copy.deepcopy(self.events),
                footprint_radius_m=None,clearance_m=None,capabilities=dict(explore=False,routes=False,export_grid=meta is not None,native_runtime=True,native_motion=False,native_resume=False),
                blocked_reason='建图/导航执行闭环、并发模式和实测机身/定位尚未验证；本服务只订阅，不发送控制。')

    def map_grid(self):
        with self.lock:
            if self.grid is None:raise RuntimeError('尚未收到原厂地图')
            return self.grid

    def save_map(self):
        with self.lock:
            grid=self.map_grid();identity=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
            directory=self.root/'maps'/identity;directory.mkdir(parents=True)
            raw=grid.data;gray=np.full(raw.shape,205,dtype=np.uint8);known=raw>=0;gray[known]=np.rint(254*(1-raw[known]/100.)).astype(np.uint8)
            (directory/'map.pgm').write_bytes(f'P5\n{grid.width} {grid.height}\n255\n'.encode()+gray[::-1].tobytes())
            (directory/'map.yaml').write_text('image: map.pgm\nmode: trinary\nresolution: '+str(grid.resolution)+'\norigin: '+json.dumps(list(grid.origin))+'\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
            atomic_json(directory/'map.json',dict(**grid.meta(),data=raw.reshape(-1).tolist()))
            atomic_json(directory/'exploration_report.json',dict(export_id=identity,simulated=False,backend=self.kind,map=grid.meta(),pose=None,raw_pose=self.raw_pose,
                native_slam_state_saved=False,native_resume_available=False,note='原厂已发布地图的只读栅格快照；没有启动或保存原厂 SLAM 会话。'))
            atomic_json(directory/'sha256.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()})
            self.emit('原厂地图只读快照已导出',map_export=identity)
            return dict(id=identity,directory=str(directory),backend=self.kind,simulated=False,native_resume_available=False)

    def denied(self,*args,**kwargs):raise RuntimeError('原厂只读模式：未发送建图、导航或运动请求；需先完成协议/定位验证')
    start=control=preview=configure_region=waypoint=delete_waypoint=save_route=load_map=denied

    def close(self):
        if self.closed:return
        self.closed=True;self.done.set()
        if self.thread:self.thread.join(timeout=5)
        self._lease.close()
