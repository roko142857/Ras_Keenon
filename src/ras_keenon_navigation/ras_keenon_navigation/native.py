"""Factory protocol adapter. Transport MUST be injected; this module opens no sockets.

Real enum/operation values must come from a verified site profile. The local launcher
only instantiates Simulator. Fake transports exercise this adapter without robot access.
"""
import copy
import math
import threading
import time
from .grid import Grid, finite


class NativeAdapter:
    kind='native'
    def __init__(self, transport, profile, clock=time.monotonic):
        self.transport=transport;self.profile=copy.deepcopy(profile);self.clock=clock;self.lock=threading.RLock()
        self.grid=None;self.pose=None;self.map_received=self.pose_received=None;self.sequence=0
        self.goal=None;self.results={};self.pending={};self.replies={};self.stamp=None
        self.mapping=False;self.session='native-unbound';self.closed=False
        self.mapping_confirmed=False;self.cancel_pending=None;self.mapping_reply=None

    def subscribe(self):
        for topic,kind in [('/move_base/goal','keenon_move_base_msgs/MoveBaseActionGoal'),('/move_base/cancel','actionlib_msgs/GoalID')]:
            self.transport.send(dict(op='advertise',topic=topic,type=kind,queue_size=1,latch=False))
        for topic in ['/map','/localization/robot_pose','/move_base/status','/move_base/feedback','/move_base/result']:
            self.transport.send(dict(op='subscribe',topic=topic,id='ras-navigation'+topic,queue_length=1,throttle_rate=200))

    def ingest(self, message):
        with self.lock:
            if message.get('op')=='service_response':
                identity=message.get('id');self.replies[identity]=message
                if identity in self.pending:
                    kind=self.pending.pop(identity)
                    if kind=='start':self.mapping_reply=message
                return
            if message.get('op')!='publish':return
            topic=message.get('topic');msg=message.get('msg',{})
            if topic=='/map':
                self.grid=Grid.from_ros(msg,self.session);self.map_received=self.clock();self.sequence+=1
            elif topic=='/localization/robot_pose':
                if msg['header'].get('frame_id')!='map':
                    self.pose=None;self.pose_received=None;raise ValueError('定位非 map 坐标系')
                p=msg['pose']['pose'];q=p['orientation'];norm=sum(q[k]**2 for k in ('x','y','z','w'))
                if not math.isfinite(norm) or abs(norm-1)>.001:raise ValueError('定位姿态无效')
                self.pose=[finite(p['position']['x'],'x'),finite(p['position']['y'],'y'),math.atan2(2*(q['w']*q['z']+q['x']*q['y']),1-2*(q['y']**2+q['z']**2))]
                self.pose_received=self.clock();self.stamp=copy.deepcopy(msg['header']['stamp'])
            elif topic=='/move_base/result':
                identity=msg.get('status',{}).get('goal_id',{}).get('id')
                if identity!=self.goal:return  # Late results can never finish the new goal.
                status=msg.get('status',{}).get('status');code=msg.get('result',{}).get('ret_status')
                if status==3 and type(code) is int and code in self.profile.get('success_result_codes',[]):result='succeeded'
                elif status in (2,8):result='cancelled'
                else:result='failed'
                self.results[identity]=result;self.goal=None
            elif topic=='/move_base/status' and self.cancel_pending:
                for status in msg.get('status_list',[]):
                    if status.get('goal_id',{}).get('id')==self.cancel_pending and status.get('status') in (2,8):
                        self.results[self.cancel_pending]='cancelled';self.goal=None;self.cancel_pending=None

    def snapshot(self):
        with self.lock:
            if self.grid is None or self.pose is None:raise RuntimeError('尚无真实地图和定位')
            if not self.profile.get('localization_semantics_verified'):raise RuntimeError('定位质量字段语义尚未确认')
            age=max(self.clock()-(self.map_received or 0),self.clock()-(self.pose_received or 0))
            return dict(grid=self.grid,pose=list(self.pose),sequence=self.sequence,age_s=age,
                        localization='profile-verified',mapping=self.mapping and self.mapping_confirmed,
                        backend=self.kind,native_resume_available=False)

    def _operation(self, operation, identity):
        spec=self.profile.get('operations',{}).get(operation)
        if not isinstance(spec,dict) or not spec.get('service') or not spec.get('type') or not isinstance(spec.get('args'),dict):
            raise RuntimeError('厂商 '+operation+' 协议未配置/未确认；未发送请求')
        if not self.profile.get('protocol_verified'):raise RuntimeError('原厂协议未验证；未发送请求')
        self.transport.send(dict(op='call_service',id=identity,service=spec['service'],type=spec['type'],args=copy.deepcopy(spec['args'])))
        self.pending[identity]=operation
        return dict(accepted=True,pending=True,request_id=identity,physical_completion_verified=False)

    def start_mapping(self, session):
        with self.lock:
            if not self.profile.get('mapping_navigation_concurrent_verified'):raise RuntimeError('建图/导航并发尚未验证')
            result=self._operation('start',session+'-mapping-start');self.session=session
            self.mapping=True;self.mapping_confirmed=False;self.grid=None;self.map_received=None
            return result

    def confirm_mapping_session(self, session):
        """Called only by a verified site handshake, never from an arbitrary websocket status."""
        with self.lock:
            if session!=self.session or self.grid is None or self.mapping_reply is None or not self.mapping_reply.get('result'):
                raise RuntimeError('原厂建图回执/新地图尚未关联')
            spec=self.profile.get('operations',{}).get('start',{})
            codes=spec.get('success_codes');field=spec.get('response_code_field')
            if not field or not codes or self.mapping_reply.get('values',{}).get(field) not in codes:raise RuntimeError('建图返回码未验证')
            self.mapping_confirmed=True

    def send_goal(self, identity, pose, path=None):
        with self.lock:
            if self.goal:raise RuntimeError('已有原厂目标')
            fields=self.profile.get('goal_fields')
            expected={'goal_status','ugly_goal_status','disable_orientation','updateVirtualWall'}
            if not self.profile.get('protocol_verified') or not isinstance(fields,dict) or set(fields)!=expected or any(v is None for v in fields.values()) or not self.profile.get('success_result_codes'):
                raise RuntimeError('原厂目标枚举/结果枚举未确认；未发送目标')
            if any(type(fields[k]) is not int or not 0<=fields[k]<=255 for k in ('goal_status','ugly_goal_status')) or any(type(fields[k]) is not bool for k in ('disable_orientation','updateVirtualWall')):raise ValueError('原厂目标字段类型无效')
            if not self.mapping_confirmed:raise RuntimeError('建图会话尚未确认')
            if self.pose_received is None or self.clock()-self.pose_received>3:raise RuntimeError('定位已过期')
            x,y,yaw=[finite(v,'goal') for v in pose]
            stamp=copy.deepcopy(self.stamp)  # The source clock, not a fabricated host stamp.
            goal=copy.deepcopy(fields);goal['target_pose']=dict(header=dict(frame_id='map',stamp=stamp),pose=dict(position=dict(x=x,y=y,z=0.),orientation=dict(x=0.,y=0.,z=math.sin(yaw/2),w=math.cos(yaw/2))))
            self.transport.send(dict(op='publish',topic='/move_base/goal',msg=dict(header=dict(stamp=stamp,frame_id='map'),goal_id=dict(stamp=stamp,id=identity),goal=goal)))
            self.goal=identity;self.results[identity]='running'

    def cancel(self):
        with self.lock:
            if self.goal:
                self.transport.send(dict(op='publish',topic='/move_base/cancel',msg=dict(stamp=dict(secs=0,nsecs=0),id=self.goal)))
                self.cancel_pending=self.goal
            return dict(cancel_requested=bool(self.goal),stopped=False,verified=False)

    def result(self, identity):return self.results.get(identity,'unknown')
    def finish_mapping(self):return self._operation('save',self.session+'-mapping-save')
    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True;self.cancel()
            for topic in ['/map','/localization/robot_pose','/move_base/status','/move_base/feedback','/move_base/result']:
                self.transport.send(dict(op='unsubscribe',topic=topic,id='ras-navigation'+topic))
            for topic in ['/move_base/goal','/move_base/cancel']:self.transport.send(dict(op='unadvertise',topic=topic))
