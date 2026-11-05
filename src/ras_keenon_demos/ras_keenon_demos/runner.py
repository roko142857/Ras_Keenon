"""One finite action sequence at a time; latest action replaces the pending action."""
from collections import OrderedDict
import json
import threading
import time
import uuid

from .catalog import normalize
from .plans import build_plan
from .transport import Cancelled

TERMINAL = ('succeeded','failed','cancelled')


class Job:
    def __init__(self, identity, action, parameters):
        self.cancel=threading.Event()
        self.data=dict(id=identity,action=action,parameters=parameters,state='queued',created_at=time.time(),
                       started_at=None,finished_at=None,steps_total=0,steps_completed=0,error='',
                       result={'arrival_verified':False})


class ActionManager:
    def __init__(self, controller):
        self.controller=controller
        self.lock=threading.RLock()
        self.jobs=OrderedDict()
        self.pending,self.active=None,None
        self.wake,self.closed=threading.Event(),threading.Event()
        self.worker=threading.Thread(target=self.work,daemon=True);self.worker.start()

    def snapshot(self, job):
        with self.lock:return json.loads(json.dumps(job.data,allow_nan=False))

    def finish(self, job, state, error=''):
        with self.lock:
            if job.data['state'] in TERMINAL:return
            job.data.update(state=state,error=error,finished_at=time.time())

    def interrupt(self, job):
        if job and job.data['state'] not in TERMINAL:
            job.cancel.set()
            if job is self.pending:
                self.finish(job,'cancelled','replaced or stopped before dispatch');self.pending=None
            else:job.data['state']='cancelling'

    def stop_controller(self):
        try:self.controller.stop();return ''
        except Exception as exc:return str(exc)

    def submit(self, action, parameters=None, request_id=None):
        parameters=normalize(action,parameters)
        if action=='stop':return self.stop()
        if request_id is not None and (not isinstance(request_id,str) or not 1<=len(request_id)<=128):
            raise ValueError('request_id must be a nonempty string, at most 128 characters')
        with self.lock:
            if self.closed.is_set():raise RuntimeError('action server is closing')
            identity=request_id or str(uuid.uuid4())
            if identity in self.jobs:
                job=self.jobs[identity]
                if job.data['action']!=action or job.data['parameters']!=parameters:
                    raise ValueError('request_id reused for a different action')
                return self.snapshot(job)
            while len(self.jobs)>=128:
                old=next((k for k,j in self.jobs.items() if j.data['state'] in TERMINAL),None)
                if old is None:raise RuntimeError('action history is full')
                del self.jobs[old]
            self.interrupt(self.pending)
            if self.active and self.active.data['state'] not in TERMINAL:
                self.interrupt(self.active);self.stop_controller()
            job=Job(identity,action,parameters)
            self.jobs[identity]=job;self.pending=job;self.wake.set()
            return self.snapshot(job)

    def status(self, identity):
        with self.lock:
            if identity not in self.jobs:raise KeyError(identity)
            return self.snapshot(self.jobs[identity])

    def cancel(self, identity):
        with self.lock:
            if identity not in self.jobs:raise KeyError(identity)
            job=self.jobs[identity]
            running=job is self.active and job.data['state'] not in TERMINAL
            self.interrupt(job)
            error=self.stop_controller() if running else ''
            result=self.snapshot(job)
            if error:result['stop_error']=error
            self.wake.set()
            return result

    def stop(self):
        with self.lock:
            self.interrupt(self.pending);self.interrupt(self.active)
            error=self.stop_controller();self.wake.set()
            return dict(stopped=True,stop_error=error,blocking_sdk_preempted=False)

    def work(self):
        while not self.closed.is_set():
            self.wake.wait(.1);self.wake.clear()
            with self.lock:
                job,self.pending=self.pending,None
                if not job:continue
                self.active=job
                job.data.update(state='running',started_at=time.time())
            try:
                if job.cancel.is_set():raise Cancelled('action cancelled')
                readings=self.controller.readings()
                plan=build_plan(job.data['action'],job.data['parameters'],readings)
                with self.lock:
                    job.data['steps_total']=len(plan.steps)
                    job.data['result'].update(backend=readings.get('backend','unknown'),plan=plan.to_dict())
                for step in plan.steps:
                    # Serialize dispatch with cancellation: a stop cannot be followed by an old dispatch.
                    with self.lock:
                        if job.cancel.is_set():raise Cancelled('action cancelled')
                        identity=self.controller.submit(step.command)
                    timeout=max(getattr(self.controller,'command_timeout',90.),
                                float(plan.parameters.get('duration_s',0.))+5.)
                    self.controller.wait(identity,job.cancel,timeout=timeout)
                    with self.lock:job.data['steps_completed']+=1
                    if job.cancel.wait(step.pause_s):raise Cancelled('action cancelled')
                with self.lock:
                    self.finish(job,'cancelled' if job.cancel.is_set() else 'succeeded')
            except Cancelled as exc:self.finish(job,'cancelled',str(exc))
            except Exception as exc:
                if not job.cancel.is_set():
                    with self.lock:self.stop_controller()
                self.finish(job,'cancelled' if job.cancel.is_set() else 'failed',str(exc))
            finally:
                with self.lock:
                    self.active=None
                    if self.pending:self.wake.set()

    def close(self):
        with self.lock:
            if self.closed.is_set():return
            self.closed.set()
            if self.pending or (self.active and self.active.data['state'] not in TERMINAL):self.stop()
            self.wake.set()
        self.worker.join(timeout=5.)
