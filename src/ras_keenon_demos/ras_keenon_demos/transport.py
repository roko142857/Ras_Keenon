"""JSON HTTP transport, with no SDK imports and no command replay on failures."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class Cancelled(Exception): pass


def request_json(url, data=None, timeout=3.):
    req=urllib.request.Request(url, None if data is None else json.dumps(data,allow_nan=False).encode(),
                               {'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        try: detail=json.loads(exc.read()).get('error',str(exc))
        except (ValueError,AttributeError): detail=str(exc)
        raise RuntimeError(detail) from exc


class ControllerClient:
    def __init__(self, url='http://127.0.0.1:8765', command_timeout=185.):
        self.url=url.rstrip('/');self.command_timeout=command_timeout

    def readings(self):return request_json(self.url+'/readings')
    def stop(self):return request_json(self.url+'/stop',{})
    def submit(self, command):return request_json(self.url+'/command',{'instruction':command})['id']

    def wait(self, identity, cancel, timeout=None):
        until=time.monotonic()+(self.command_timeout if timeout is None else timeout)
        while True:
            if cancel.is_set():raise Cancelled('action cancelled')
            result=request_json(self.url+'/result/'+urllib.parse.quote(identity,safe=''))
            if result['done']:
                if not result['success']:raise RuntimeError(result.get('message','controller command failed'))
                return result
            if time.monotonic()>=until:raise TimeoutError('controller result unknown; command was not retried')
            cancel.wait(.02)


class ActionClient:
    """Stable Python interface for scripts, dialogue agents and other applications."""
    def __init__(self, url='http://127.0.0.1:8766'):self.url=url.rstrip('/')
    def catalog(self):return request_json(self.url+'/actions')
    def tools(self):return request_json(self.url+'/tools')
    def plan(self, action, parameters=None):
        return request_json(self.url+'/plan',{'action':action,'parameters':{} if parameters is None else parameters})
    def submit(self, action, parameters=None, request_id=None):
        return request_json(self.url+'/actions',dict(action=action,parameters={} if parameters is None else parameters,request_id=request_id))
    def status(self, identity):return request_json(self.url+'/actions/'+urllib.parse.quote(identity,safe=''))
    def cancel(self, identity):return request_json(self.url+'/actions/'+urllib.parse.quote(identity,safe='')+'/cancel',{})
    def stop(self):return request_json(self.url+'/stop',{})
    def call_tool(self, name, arguments=None, call_id=None):
        return request_json(self.url+'/tool_call',dict(name=name,arguments={} if arguments is None else arguments,call_id=call_id))

    def wait(self, identity, timeout=240.):
        until=time.monotonic()+timeout
        while True:
            job=self.status(identity)
            if job['state'] in ('succeeded','failed','cancelled'):return job
            if time.monotonic()>until:
                self.cancel(identity)
                raise TimeoutError('action wait timed out; cancellation requested')
            time.sleep(.05)

    def run(self, action, parameters=None, request_id=None, timeout=240.):
        if action=='stop':return self.stop()
        job=self.submit(action,parameters,request_id)
        try:return self.wait(job['id'],timeout)
        except KeyboardInterrupt:
            self.cancel(job['id']);raise
