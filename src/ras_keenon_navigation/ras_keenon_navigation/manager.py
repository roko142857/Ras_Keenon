"""Single-task local exploration lifecycle; disk snapshots never resume motion."""
import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid
import zlib
import struct
import numpy as np
from .grid import Search, finite, polygon, region_mask
from .simulator import Simulator

ACTIVE={'exploring','navigating','paused'}


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False));tmp.replace(path)


def png(grid):
    gray=np.full(grid.data.shape,205,dtype=np.uint8)
    known=grid.data>=0;gray[known]=np.rint(254*(1-grid.data[known]/100.)).astype(np.uint8)
    rows=gray[::-1];raw=b''.join(b'\0'+row.tobytes() for row in rows)
    def chunk(name,data):return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',grid.width,grid.height,8,0,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


class Manager:
    def __init__(self, data_dir, backend=None, autostep=True, interval=.1):
        self.root=Path(data_dir);self.root.mkdir(parents=True,exist_ok=True)
        self.backend=backend or Simulator()
        if self.backend.kind!='mock':raise ValueError('本地部署仅支持无硬件连接的 mock 后端')
        self._lease=(self.root/'manager.lock').open('a')
        try:fcntl.flock(self._lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease.close();raise RuntimeError('已有本地导航进程使用此数据目录')
        self.closed=False
        self.lock=threading.RLock();self.done=threading.Event()
        self.task=None;self.events=[];self.waypoints={};self.routes={};self.receipts={};self.candidates=[];self.path=[];self.trail=[]
        self.boundary=None;self.exclusions=[];self.radius=.18;self.margin=.08
        self.failures=[];self.next_plan=0.;self.empty_count=0;self.last_empty_sequence=-1
        self.interval=interval;self.started_mono=0.;self.goal=None;self.goal_at=0.;self.route_queue=[];self.dwell_until=0.;self.request_ids={};self.revision=0
        settings=self.root/'settings.json'
        if settings.exists():
            value=json.loads(settings.read_text());self.waypoints=value.get('waypoints',{});self.routes=value.get('routes',{})
        state=self.root/'last_task.json'
        if state.exists():
            self.task=json.loads(state.read_text())
            if self.task.get('state') in ACTIVE:
                self.task.update(state='interrupted',reason='进程重启；旧任务没有自动恢复或重发目标')
                atomic_json(state,self.task)
        request_file=self.root/'request_ids.json'
        if request_file.exists():self.request_ids=json.loads(request_file.read_text())
        if self.task and self.task.get('request_id'):
            self.request_ids[self.task['request_id']]=dict(signature=self.task['request_signature'],task=copy.deepcopy(self.task))
        self.thread=None
        if autostep:
            self.thread=threading.Thread(target=self.run,daemon=True,name='local-exploration');self.thread.start()

    def emit(self, title, **details):
        event=dict(sequence=self.revision,time=time.time(),title=title,**details);self.revision+=1
        self.events.append(event);self.events=self.events[-200:]
        with (self.root/'events.jsonl').open('a') as f:f.write(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n')

    def persist(self):
        if self.task:atomic_json(self.root/'last_task.json',self.task)

    def snapshot(self):
        with self.lock:
            v=self.backend.snapshot();g=v['grid']
            return dict(service='ras_keenon_navigation',backend=self.backend.kind,local_only=self.backend.kind=='mock',
                        simulated=self.backend.kind=='mock',robot_connection=False,
                        map=g.meta(),pose=v['pose'],sensor_age_s=round(v['age_s'],3),localization=v['localization'],
                        mapping=v['mapping'],task=copy.deepcopy(self.task),task_historical=bool(self.task and self.task.get('map_id')!=g.map_id),goal=copy.deepcopy(self.goal),
                        candidates=copy.deepcopy(self.candidates),path=copy.deepcopy(self.path),trail=copy.deepcopy(self.trail[-1000:]),
                        boundary=copy.deepcopy(self.boundary),exclusions=copy.deepcopy(self.exclusions),
                        waypoints=[copy.deepcopy(w) for w in self.waypoints.values()],routes=copy.deepcopy(self.routes),
                        footprint_radius_m=self.radius,clearance_m=self.margin,events=copy.deepcopy(self.events[-60:]),
                        capabilities=dict(explore=True,routes=True,export_grid=True,native_runtime=False,native_resume=False),
                        synthetic_sensor='360° ray scanner; not robot coverage' if self.backend.kind=='mock' else None)

    def configure_region(self, boundary=None, exclusions=None):
        with self.lock:
            if self.task and self.task['state'] in ACTIVE:raise RuntimeError('任务活动时不能修改区域，请先结束任务')
            bound=polygon(boundary);exclusions=exclusions or []
            if not isinstance(exclusions,list) or len(exclusions)>20:raise ValueError('禁入区数量无效')
            exclusions=[polygon(p) for p in exclusions]
            if any(p is None for p in exclusions):raise ValueError('禁入区不能为空')
            snap=self.backend.snapshot();Search(snap['grid'],snap['pose'],self.radius+self.margin,bound,exclusions)
            self.boundary=bound;self.exclusions=exclusions;self.candidates=[]
            self.emit('区域已更新',boundary=bound,exclusions=exclusions)
            return self.snapshot()

    def waypoint(self, value):
        with self.lock:
            g=self.backend.snapshot()['grid'];name=value.get('name','')
            if not isinstance(name,str) or not 1<=len(name.strip())<=60:raise ValueError('请输入 1–60 字符的点名称')
            x=finite(value.get('x'),'x');y=finite(value.get('y'),'y');yaw=finite(value.get('yaw',0),'yaw',-math.pi,math.pi)
            snap=self.backend.snapshot();Search(g,snap['pose'],self.radius+self.margin,self.boundary,self.exclusions).path(x,y)
            identity=value.get('id') or uuid.uuid4().hex[:12]
            if not isinstance(identity,str) or len(identity)>80:raise ValueError('点 ID 无效')
            self.waypoints[identity]=dict(id=identity,name=name.strip(),x=x,y=y,yaw=yaw,map_id=g.map_id)
            self.save_settings();self.emit('命名点已保存',waypoint_id=identity)
            return self.waypoints[identity]

    def delete_waypoint(self, identity):
        with self.lock:
            if self.task and self.task['state'] in ACTIVE:raise RuntimeError('任务活动时不能删除点')
            self.waypoints.pop(identity);self.save_settings();return dict(deleted=True)

    def save_settings(self):atomic_json(self.root/'settings.json',dict(waypoints=self.waypoints,routes=self.routes))

    def save_route(self, name, steps):
        with self.lock:
            self.validate_steps(steps)
            if not isinstance(name,str) or not 1<=len(name.strip())<=60:raise ValueError('路线名称无效')
            self.routes[name.strip()]=copy.deepcopy(steps);self.save_settings();return dict(name=name,steps=steps)

    def validate_steps(self, steps):
        if not isinstance(steps,list) or not 1<=len(steps)<=50:raise ValueError('路线需要 1–50 个点')
        current=self.backend.snapshot()['grid'].map_id
        for step in steps:
            if not isinstance(step,dict) or set(step)-{'waypoint_id','dwell_s'}:raise ValueError('路线步骤字段无效')
            identity=step.get('waypoint_id')
            if identity not in self.waypoints:raise ValueError('命名点不存在：'+str(identity))
            if self.waypoints[identity]['map_id']!=current:raise ValueError('命名点属于旧地图，需重新选择')
            finite(step.get('dwell_s',0),'dwell_s',0,300)

    def start(self, data):
        with self.lock:
            request_id=data.get('request_id')
            if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('开始任务需要 request_id')
            signature=hashlib.sha256(json.dumps({k:v for k,v in data.items() if k!='request_id'},sort_keys=True).encode()).hexdigest()
            if request_id in self.request_ids:
                old=self.request_ids[request_id]
                if old['signature']!=signature:raise ValueError('同一 request_id 对应不同请求')
                return dict(task=copy.deepcopy(self.task if self.task and self.task['id']==old['task']['id'] else old['task']),deduplicated=True)
            if self.task and self.task['state'] in ACTIVE:raise RuntimeError('已有活动任务')
            mode=data.get('mode','explore')
            if mode not in ('explore','route'):raise ValueError('mode 需要 explore 或 route')
            snap=self.backend.snapshot()
            if snap['age_s']>3:raise RuntimeError('传感器已过期')
            Search(snap['grid'],snap['pose'],self.radius+self.margin,self.boundary,self.exclusions)
            if mode=='route':self.validate_steps(data.get('steps'))
            limit=finite(data.get('duration_s',600),'duration_s',1,7200)
            identity=uuid.uuid4().hex
            if mode=='explore':self.backend.start_mapping('mock-'+identity[:12])
            self.started_mono=time.monotonic()
            self.task=dict(id=identity,request_id=request_id,request_signature=signature,state='exploring' if mode=='explore' else 'navigating',mode=mode,started_at=time.time(),
                           elapsed_s=0.,duration_s=limit,targets_completed=0,targets_failed=0,reason='',backend=self.backend.kind,
                           map_id=self.backend.snapshot()['grid'].map_id,steps=copy.deepcopy(data.get('steps',[])),route_index=0,saved_map=None)
            self.goal=None;self.path=[];self.trail=[];self.failures=[];self.candidates=[];self.empty_count=0
            self.next_plan=0.;self.last_empty_sequence=-1;self.route_queue=copy.deepcopy(data.get('steps',[]));self.dwell_until=0.
            self.persist();self.emit('开始模拟探索' if mode=='explore' else '开始模拟路线',task_id=identity)
            result=copy.deepcopy(self.task);self.request_ids[request_id]=dict(signature=signature,task=result)
            self.request_ids=dict(list(self.request_ids.items())[-200:]);atomic_json(self.root/'request_ids.json',self.request_ids)
            return dict(task=result,deduplicated=False)

    def control(self, action):
        with self.lock:
            if not self.task:raise RuntimeError('尚无任务')
            if action not in ('pause','resume','stop','finish'):raise ValueError('未知任务操作')
            state=self.task['state']
            if action=='resume':
                if state!='paused':raise RuntimeError('任务并未暂停')
                if self.backend.snapshot()['age_s']>3:raise RuntimeError('传感器已过期')
                self.task['state']='exploring' if self.task['mode']=='explore' else 'navigating';self.next_plan=0.
            elif action=='pause':
                if state not in ('exploring','navigating'):raise RuntimeError('当前任务不能暂停')
                self.backend.cancel();self.goal=None;self.path=[];self.task['state']='paused'
            elif action in ('stop','finish'):
                if state not in ACTIVE:return dict(task=copy.deepcopy(self.task))
                self.backend.cancel();self.goal=None;self.path=[];self.backend.finish_mapping()
                self.task.update(state='cancelled' if action=='stop' else 'partial',reason='用户停止' if action=='stop' else '用户结束并保存；不代表全部探索完成')
                if action=='finish':self.save_map()
            self.persist();self.emit('任务操作：'+action,state=self.task['state'])
            return dict(task=copy.deepcopy(self.task))

    def save_map(self):
        with self.lock:
            grid=self.backend.snapshot()['grid'];identity=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
            directory=self.root/'maps'/identity;directory.mkdir(parents=True)
            raw=grid.data;gray=np.full(raw.shape,205,dtype=np.uint8);known=raw>=0;gray[known]=np.rint(254*(1-raw[known]/100.)).astype(np.uint8)
            (directory/'map.pgm').write_bytes(f'P5\n{grid.width} {grid.height}\n255\n'.encode()+gray[::-1].tobytes())
            (directory/'map.yaml').write_text('image: map.pgm\nmode: trinary\nresolution: '+str(grid.resolution)+'\norigin: '+json.dumps(list(grid.origin))+'\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
            atomic_json(directory/'map.json',dict(**grid.meta(),data=raw.reshape(-1).tolist()))
            report=dict(backend=self.backend.kind,simulated=self.backend.kind=='mock',native_resume_available=False,
                        native_slam_state_saved=False,map=grid.meta(),pose=self.backend.snapshot()['pose'],task=copy.deepcopy(self.task),boundary=self.boundary,exclusions=self.exclusions,
                        note='仅导出栅格，不是原厂数据库/可续建 SLAM 状态',created_at=time.time())
            atomic_json(directory/'exploration_report.json',report)
            manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
            atomic_json(directory/'sha256.json',manifest)
            if self.task:self.task['saved_map']=identity;self.persist()
            self.emit('模拟栅格地图已保存',map_export=identity)
            return dict(id=identity,directory=str(directory.resolve()),native_resume_available=False)

    def load_map(self, identity):
        with self.lock:
            if self.task and self.task['state'] in ACTIVE:raise RuntimeError('任务活动时不能加载地图')
            if not isinstance(identity,str) or not identity or any(c not in '0123456789abcdef_' for c in identity):raise ValueError('导出 ID 无效')
            directory=self.root/'maps'/identity
            manifest=json.loads((directory/'sha256.json').read_text())
            for name in ('map.json','exploration_report.json'):
                if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=manifest.get(name):raise ValueError('导出文件校验失败')
            value=json.loads((directory/'map.json').read_text());report=json.loads((directory/'exploration_report.json').read_text())
            if not report.get('simulated'):raise ValueError('本地只允许加载模拟地图')
            from .grid import Grid
            grid=Grid(np.asarray(value['data']).reshape(value['height'],value['width']),value['resolution'],tuple(value['origin']),value['map_id'])
            if grid.data.shape!=self.backend.world.shape or grid.resolution!=.1 or grid.origin!=(0.,0.,0.):raise ValueError('不是当前模拟世界的地图')
            known=grid.data>=0
            if not np.array_equal(grid.data[known],self.backend.world[known]):raise ValueError('地图与模拟世界不匹配')
            pose=report.get('pose')
            if not isinstance(pose,list) or len(pose)!=3:raise ValueError('导出没有模拟位姿')
            pose=[finite(p,'pose') for p in pose]
            Search(grid,pose,self.radius+self.margin)
            self.backend.cancel();self.backend.known=grid.data.copy();self.backend.pose=pose;self.backend.session=grid.map_id
            self.backend.mapping=False;self.backend.last=self.backend.clock()
            self.boundary=None;self.exclusions=[];self.path=[];self.candidates=[];self.trail=[];self.goal=None
            self.emit('已加载模拟栅格快照；不是 SLAM 续建',map_export=identity)
            return dict(loaded=True,map=grid.meta(),native_resume_available=False)

    def dispatch_goal(self, pose, search, waypoint_id=None):
        identity=uuid.uuid4().hex;path=search.path(pose[0],pose[1]);self.backend.send_goal(identity,pose,path)
        self.goal=dict(id=identity,x=pose[0],y=pose[1],yaw=pose[2],waypoint_id=waypoint_id);self.path=path;self.goal_at=time.monotonic()
        self.emit('模拟导航目标已提交',goal_id=identity,target=pose)

    def step(self):
        with self.lock:
            self.backend.tick();v=self.backend.snapshot()
            if not self.task or self.task['state'] not in ('exploring','navigating'):return
            self.task['elapsed_s']=round(time.monotonic()-self.started_mono,2)
            if v['age_s']>3:raise RuntimeError('观测超过 3 秒未更新')
            if self.task['elapsed_s']>self.task['duration_s']:
                self.backend.cancel();self.backend.finish_mapping();self.goal=None;self.path=[]
                self.task.update(state='partial',reason='达到任务时限，未宣称完整覆盖');self.save_map();self.persist();return
            if not self.trail or math.dist(self.trail[-1],v['pose'][:2])>.08:self.trail.append(v['pose'][:2])
            now=time.monotonic()
            if self.goal:
                result=self.backend.result(self.goal['id'])
                if result=='running' and now-self.goal_at<60:return
                if result=='succeeded':
                    self.task['targets_completed']+=1
                    if self.task['mode']=='route':
                        step=self.route_queue.pop(0);self.task['route_index']+=1;self.dwell_until=now+step.get('dwell_s',0)
                    self.emit('模拟导航到达',goal_id=self.goal['id'])
                else:
                    self.task['targets_failed']+=1;self.failures.append((now,self.goal['x'],self.goal['y']));self.backend.cancel()
                    self.emit('目标失败',goal_id=self.goal['id'],result=result)
                    if self.task['mode']=='route':
                        self.task.update(state='failed',reason='路线目标失败；未跳过或自动重放');self.goal=None;self.path=[];self.persist();return
                self.goal=None;self.path=[];self.next_plan=now+.2;self.persist()
            if now<max(self.next_plan,self.dwell_until):return
            self.next_plan=now+1.
            search=Search(v['grid'],v['pose'],self.radius+self.margin,self.boundary,self.exclusions)
            if self.task['mode']=='route':
                if not self.route_queue:
                    self.task.update(state='completed',reason='模拟路线全部到达');self.persist();self.emit('模拟路线完成');return
                w=self.waypoints[self.route_queue[0]['waypoint_id']]
                self.dispatch_goal([w['x'],w['y'],w['yaw']],search,w['id']);return
            self.failures=[f for f in self.failures if now-f[0]<60]
            self.candidates=search.frontiers([(f[1],f[2]) for f in self.failures])[:30]
            if not self.candidates:
                if v['sequence']!=self.last_empty_sequence:self.empty_count+=1;self.last_empty_sequence=v['sequence']
                if self.empty_count>=3:
                    self.backend.finish_mapping();self.task.update(state='partial' if self.task['targets_failed'] else 'completed',reason='连续三次新观测无可达前沿；'+('仍有失败目标，部分完成；' if self.task['targets_failed'] else '')+'限当前区域与传感器，不代表房间全覆盖')
                    self.save_map();self.persist();self.emit('模拟探索结束',reason=self.task['reason'])
                return
            self.empty_count=0;best=self.candidates[0];self.dispatch_goal([best['x'],best['y'],best['yaw']],search)

    def preview(self):
        with self.lock:
            v=self.backend.snapshot();search=Search(v['grid'],v['pose'],self.radius+self.margin,self.boundary,self.exclusions)
            self.candidates=search.frontiers()[:30];return dict(candidates=copy.deepcopy(self.candidates),map_version=v['grid'].version,simulated=True)

    def run(self):
        while not self.done.wait(self.interval):
            try:self.step()
            except Exception as exc:
                with self.lock:
                    self.backend.cancel();self.goal=None;self.path=[]
                    if self.task and self.task['state'] in ACTIVE:self.task.update(state='failed',reason=str(exc));self.persist()
                    self.emit('本地任务错误',error=str(exc))
                self.done.wait(.5)

    def close(self):
        if self.closed:return
        self.closed=True
        self.done.set()
        if self.thread:self.thread.join(timeout=5)
        with self.lock:
            self.backend.cancel()
            if self.task and self.task['state'] in ACTIVE:
                self.task.update(state='interrupted',reason='服务退出，任务未自动恢复');self.persist()
            self.backend.close()
            self._lease.close()
