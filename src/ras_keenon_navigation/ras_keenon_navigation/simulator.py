"""Explicitly synthetic two-room map. No sockets, hardware imports or SDKs."""
import math
import time
import numpy as np
from .grid import Grid


class Simulator:
    kind='mock'
    def __init__(self, speed=.9, clock=time.monotonic):
        self.clock=clock;self.speed=speed;self.last=clock();self.sequence=0
        self.world=np.zeros((60,80),dtype=np.int8)
        self.world[[0,-1],:]=100;self.world[:,[0,-1]]=100
        self.world[:,40]=100;self.world[24:36,40]=0
        self.world[15:23,18:25]=100;self.world[42:49,58:66]=100
        self.pose=[1.25,1.25,0.];self.mapping=False;self.goal=None;self.results={};self.session='mock-lobby'
        self.known=np.full_like(self.world,-1);self.observe()

    def observe(self):
        col=int(self.pose[0]/.1);row=int(self.pose[1]/.1)
        # Synthetic 360-degree scanner; deliberately not a claim about robot coverage.
        for angle in np.linspace(-math.pi,math.pi,240,endpoint=False):
            for distance in np.arange(0,2.7,.045):
                x=int((self.pose[0]+math.cos(angle)*distance)/.1);y=int((self.pose[1]+math.sin(angle)*distance)/.1)
                if not (0<=x<80 and 0<=y<60):break
                self.known[y,x]=self.world[y,x]
                if self.world[y,x]>=65:break
        self.known[row,col]=0;self.sequence+=1

    def snapshot(self):
        return dict(grid=Grid(self.known,.1,map_id=self.session),pose=list(self.pose),sequence=self.sequence,
                    age_s=max(0,self.clock()-self.last),localization='simulated',mapping=self.mapping,
                    native_resume_available=False,backend=self.kind)

    def start_mapping(self, session):
        self.cancel();self.session=session;self.mapping=True
        self.known=np.full_like(self.world,-1);self.observe();self.last=self.clock()
        return dict(accepted=True,backend=self.kind,session_id=session)

    def send_goal(self, identity, pose, path):
        if self.goal:raise RuntimeError('已有导航目标')
        self.goal=dict(id=identity,target=list(pose),path=[list(p) for p in path],index=0)
        self.results[identity]='running'

    def cancel(self):
        if self.goal:self.results[self.goal['id']]='cancelled'
        self.goal=None
        return dict(stopped=True,verified=True,backend=self.kind)

    def result(self, identity):return self.results.get(identity,'unknown')

    def tick(self):
        now=self.clock();budget=max(0,min(now-self.last,.5))*self.speed;self.last=now
        if self.goal:
            g=self.goal
            while budget>0 and g['index']<len(g['path']):
                target=g['path'][g['index']];dx=target[0]-self.pose[0];dy=target[1]-self.pose[1];distance=math.hypot(dx,dy)
                if distance<1e-7:g['index']+=1;continue
                step=min(budget,distance);self.pose[0]+=dx/distance*step;self.pose[1]+=dy/distance*step;self.pose[2]=math.atan2(dy,dx);budget-=step
                if step>=distance-1e-7:g['index']+=1
            if g['index']>=len(g['path']):
                self.pose[:]=g['target'];self.results[g['id']]='succeeded';self.goal=None
        self.observe()

    def finish_mapping(self):self.mapping=False
    def close(self):self.cancel()
