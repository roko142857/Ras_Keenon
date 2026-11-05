"""Conversation/tool loop with independently cancellable turns and visible receipts."""
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlparse

from ras_keenon_demos import ActionClient,tool_schemas
from ras_keenon_demos.catalog import decode_tool_call
from ras_keenon_demos.plans import build_plan
from . import provider
from .navigation import NavigationClient, TOOLS as NAVIGATION_TOOLS, NAMES as NAVIGATION_NAMES, PROMPT as NAVIGATION_PROMPT

SYSTEM = '''你是 Keenon 机器人的中文对话助手。通过给定工具调用预设动作，普通对话不调用动作。
用户明确要求动作时直接调用对应工具，无需先做使能或 base_ready 检查，不要求额外确认。
一次有多个动作时按用户要求的顺序执行。角度为度，下蹲高度为米。缺省参数使用工具默认值。
只根据工具结果描述执行情况；result.arrival_verified=false 表示没有独立机械到位确认，不要声称已验证到位。
若工具失败或取消，如实说明原因，不自动重复动作。用户要求停止时调用 robot_stop。
用户要求复位或关节归零时调用 robot_reset：头部和双臂归零，腰部默认下蹲 0.03 米，不是直立，也不校准编码器。
普通挥手、大幅挥手、举手过头顶挥手使用 robot_wave_large，包含肩肘腕多关节动作。
明确要求原来的挥手、单肘摆手、小幅招手或 shake_hand 时使用 robot_shake_hand；该动作不是与人握手。
旧 robot_wave 已更名，不再使用。用户指定左右臂时传 side。
演示模式中的动作不是真实机器人运动，回复中应清楚说明。工具结果 backend=mock 或 demo=true 时也只能描述为模拟。
请求超过工具参数范围时说明原因，不擅自替换成其他幅度。'''


class Cancelled(Exception):pass


class ChatEngine:
    def __init__(self,store,overrides=None,completer=None,navigation_url=None,navigation_only=False):
        self.store=store
        self.lock=threading.RLock();self.dispatch=threading.RLock()
        self.active={};self.closed=False
        self.completer=completer or provider.complete
        self.navigation=NavigationClient(navigation_url) if navigation_url else None
        self.navigation_only=navigation_only
        if navigation_only and not self.navigation:raise ValueError('本地导航会话需要导航服务地址')
        self.config=dict(mode='api',base_url='',model='',action_url='http://127.0.0.1:8766',api_key='')
        self.config.update(store.load_settings())
        self.config.update({k:v for k,v in dict(base_url=os.getenv('LLM_BASE_URL'),model=os.getenv('LLM_MODEL'),
            api_key=os.getenv('LLM_API_KEY')).items() if v})
        self.config.update({k:v for k,v in (overrides or {}).items() if v is not None})

    def public_config(self):
        with self.lock:
            value={k:v for k,v in self.config.items() if k!='api_key'}
            value['key_configured']=bool(self.config.get('api_key'))
            value['navigation_only']=self.navigation_only
            return value

    def configure(self,data):
        if not isinstance(data,dict):raise ValueError('设置必须为对象。')
        allowed={'mode','base_url','model','action_url','api_key','clear_api_key'}
        if set(data)-allowed:raise ValueError('未知设置字段。')
        with self.lock:
            cfg=dict(self.config)
            for name in ('mode','base_url','model','action_url'):
                if name in data:
                    if not isinstance(data[name],str) or len(data[name])>2048:raise ValueError('设置值必须为文本。')
                    cfg[name]=data[name].strip()
            if cfg['mode'] not in ('api','demo'):raise ValueError('mode 必须为 api 或 demo。')
            if cfg['base_url']:provider.chat_url(cfg['base_url'])
            parsed=urlparse(cfg['action_url'])
            if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('动作服务地址必须为不含凭据的 http(s) URL。')
            if 'api_key' in data:
                key=data['api_key']
                if not isinstance(key,str) or len(key)>8192:raise ValueError('API Key 格式无效。')
                if '\r' in key or '\n' in key:raise ValueError('API Key 不能包含换行，请重新复制。')
                if key.strip():cfg['api_key']=key.strip()
            if data.get('clear_api_key') is True:cfg['api_key']=''
            self.config=cfg;self.store.save_settings(cfg)
            return self.public_config()

    @staticmethod
    def clean(value,key):
        if not key:return value
        if isinstance(value,str):return value.replace(key,'[REDACTED]')
        if isinstance(value,list):return [ChatEngine.clean(v,key) for v in value]
        if isinstance(value,dict):return {k:ChatEngine.clean(v,key) for k,v in value.items()}
        return value

    def write_message(self,ctx,text):
        with self.lock:
            self.check(ctx)
            self.store.message(ctx['session_id'],ctx['id'],'assistant',self.clean(text,ctx['config'].get('api_key')))

    def check(self,ctx):
        if ctx['cancel'].is_set() or self.closed:raise Cancelled('本轮已取消。')

    def send(self,session_id,text):
        if not isinstance(text,str) or not text.strip() or len(text)>12000:raise ValueError('请输入 1–12000 个字符。')
        with self.lock:
            if self.closed:raise RuntimeError('对话服务正在关闭。')
            cfg=dict(self.config)
            identity=self.store.new_turn(session_id,self.clean(text.strip(),cfg.get('api_key')))
            ctx=dict(id=identity,session_id=session_id,cancel=threading.Event(),config=cfg,job_id=None,thread=None)
            self.active[identity]=ctx
            ctx['thread']=threading.Thread(target=self.run,args=(ctx,),daemon=True)
            ctx['thread'].start()
            return dict(turn_id=identity,state='running')

    def receipt(self,ctx,kind,state,title,data,identity=None):
        if self.closed:return identity or ''
        return self.store.receipt(ctx['session_id'],ctx['id'],kind,state,title,
                                  self.clean(data,ctx['config'].get('api_key')),identity)

    def run(self,ctx):
        sid,tid,cfg=ctx['session_id'],ctx['id'],ctx['config']
        try:
            history=self.store.session(sid)['messages']
            messages=[dict(role='system',content=SYSTEM+(NAVIGATION_PROMPT if self.navigation else ''))]+[
                dict(role=m['role'],content=m['content']) for m in history[-40:] if m['content']]
            for round_no in range(1,9):
                self.check(ctx)
                data=dict(model=cfg['model'] if cfg['mode']=='api' else 'demo-rules',
                          mode=cfg['mode'],round=round_no,message_count=len(messages),started_at=time.time())
                rid=self.receipt(ctx,'llm','running',f'模型请求 · 第 {round_no} 轮',data)
                started=time.monotonic()
                try:
                    response,metadata=self.completer(cfg,messages,([] if self.navigation_only else tool_schemas())+(self.navigation.schemas() if self.navigation else []))
                    choice=response['choices'][0];message=choice['message']
                    if not isinstance(message,dict):raise ValueError('模型 message 格式无效。')
                    calls=message.get('tool_calls') or []
                    if not isinstance(calls,list):raise ValueError('tool_calls 必须为数组。')
                    data.update(metadata,response_id=response.get('id'),model=response.get('model',data['model']),
                                usage=response.get('usage'),finish_reason=choice.get('finish_reason'),tool_count=len(calls),
                                latency_ms=round((time.monotonic()-started)*1000,1),response=response)
                    self.receipt(ctx,'llm','cancelled' if ctx['cancel'].is_set() else 'succeeded',
                                 f'模型回执 · 第 {round_no} 轮',data,rid)
                except Exception as exc:
                    data.update(error=str(exc),latency_ms=round((time.monotonic()-started)*1000,1),
                                http_status=getattr(exc,'status',None),request_id=getattr(exc,'request_id',None))
                    self.receipt(ctx,'llm','cancelled' if ctx['cancel'].is_set() else 'failed','模型请求失败',data,rid)
                    raise
                self.check(ctx)
                content=message.get('content') or ''
                if not isinstance(content,str):raise ValueError('当前接口要求模型 content 为文本。')
                if content:self.write_message(ctx,content)
                wire={k:message[k] for k in ('role','content','tool_calls','reasoning_content') if k in message}
                wire['role']='assistant';wire.setdefault('content',None)
                messages.append(wire)
                if not calls:
                    if not content:self.write_message(ctx,'模型返回了空消息，请查看回执或调整模型配置。')
                    with self.lock:
                        self.check(ctx);self.store.finish_turn(tid,'completed')
                    return
                if len(calls)>16:raise ValueError('单轮工具调用过多。')
                for call in calls:
                    self.check(ctx)
                    call_id=call.get('id')
                    if not isinstance(call_id,str) or not call_id:raise ValueError('工具调用缺少有效 ID。')
                    function=call.get('function') or {}
                    outcome=self.execute_tool(ctx,function.get('name'),function.get('arguments','{}'),call_id)
                    messages.append(dict(role='tool',tool_call_id=call_id,
                                         content=json.dumps(outcome,ensure_ascii=False,allow_nan=False)))
            raise RuntimeError('达到 8 轮工具调用上限，本轮已结束，不会自动重放动作。')
        except Cancelled:pass
        except Exception as exc:
            if not ctx['cancel'].is_set():
                message=self.clean(str(exc),cfg.get('api_key'))
                self.store.message(sid,tid,'assistant','本轮未完成：'+message)
                self.store.finish_turn(tid,'failed')
        finally:
            with self.lock:self.active.pop(tid,None)

    def execute_tool(self,ctx,name,arguments,call_id):
        data=dict(name=name,arguments=arguments,tool_call_id=call_id,mode=ctx['config']['mode'])
        rid=self.receipt(ctx,'action','running','动作调用 · '+str(name),data)
        try:
            if name in NAVIGATION_NAMES:
                if not self.navigation:raise ValueError('导航服务未配置')
                stable_id='chat-'+ctx['id'][:12]+'-'+hashlib.sha256(call_id.encode()).hexdigest()[:24]
                with self.dispatch:
                    self.check(ctx)
                    outcome=self.navigation.tool(name,arguments,stable_id)
                self.check(ctx)
                data.update(result=outcome,job_state='succeeded')
                self.receipt(ctx,'action','succeeded',('本地导航接口回执 · ' if outcome.get('backend')=='mock' else '原厂只读接口回执 · ')+str(name),data,rid)
                return outcome
            if self.navigation_only:raise ValueError('本地探索会话只允许模拟导航工具')
            action,params,_=decode_tool_call(dict(name=name,arguments=arguments,call_id=call_id))
            data['arguments']=params
            self.check(ctx)
            stable_id='chat-'+ctx['id'][:12]+'-'+hashlib.sha256(call_id.encode()).hexdigest()[:24]
            if ctx['config']['mode']=='demo':
                readings=dict(body=dict(yaw_deg=0.,pitch_deg=0.,right=[0.]*7,left=[0.]*7))
                plan=build_plan(action,params,readings)
                if ctx['cancel'].wait(.2):raise Cancelled()
                outcome=dict(id=stable_id,state='succeeded',action=action,
                             result=dict(demo=True,arrival_verified=False,steps=len(plan.steps)))
            else:
                client=ActionClient(ctx['config']['action_url'])
                with self.dispatch:
                    self.check(ctx)
                    ack=client.call_tool(name,params,stable_id)
                    ctx['job_id']=ack.get('id')
                if 'id' not in ack:
                    outcome=dict(state='succeeded' if not ack.get('stop_error') else 'failed',result=ack)
                else:
                    data.update(job_id=ack['id'],job_state=ack['state'])
                    self.receipt(ctx,'action','running','动作执行中 · '+str(name),data,rid)
                    while True:
                        self.check(ctx)
                        outcome=client.status(ack['id'])
                        if outcome['state'] in ('succeeded','failed','cancelled'):break
                        data.update(job_state=outcome['state'],steps_completed=outcome.get('steps_completed'),
                                    steps_total=outcome.get('steps_total'))
                        self.receipt(ctx,'action','running','动作执行中 · '+str(name),data,rid)
                        ctx['cancel'].wait(.1)
            self.check(ctx)
            ctx['job_id']=None
            data['result']=outcome
            data['job_state']=outcome.get('state')
            for key in ('steps_total','steps_completed'):
                if key in outcome:data[key]=outcome[key]
            self.receipt(ctx,'action',outcome.get('state','succeeded'),'动作回执 · '+str(name),data,rid)
            return outcome
        except Cancelled:
            self.receipt(ctx,'action','cancelled','动作调用已取消',data,rid);raise
        except Exception as exc:
            data['error']=str(exc)
            self.receipt(ctx,'action','failed','动作调用失败 · '+str(name),data,rid)
            return dict(state='failed',error=self.clean(str(exc),ctx['config'].get('api_key')))

    def cancel(self,identity):
        with self.lock:
            ctx=self.active.get(identity)
            if not ctx:return dict(cancelled=False,message='本轮已经结束。')
            if ctx['cancel'].is_set():return dict(cancelled=True,message='本轮正在取消。')
            ctx['cancel'].set()
        error=''
        with self.dispatch:
            if ctx['job_id'] and ctx['config']['mode']=='api':
                try:ActionClient(ctx['config']['action_url']).cancel(ctx['job_id'])
                except Exception as exc:error=str(exc)
        self.store.finish_turn(identity,'cancelled')
        self.store.message(ctx['session_id'],identity,'assistant','本轮已取消，后续动作不会继续发送。')
        self.receipt(ctx,'control','cancelled','本轮已取消',dict(error=error or None))
        return dict(cancelled=True,error=error)

    def stop(self):
        with self.lock:
            active=list(self.active.values());cfg=dict(self.config)
        for ctx in active:self.cancel(ctx['id'])
        errors=[]
        if self.navigation:
            try:self.navigation.post('/api/control',{'action':'stop'})
            except Exception as exc:errors.append(str(exc))
        if cfg['mode']=='api' and not self.navigation_only:
            try:ActionClient(cfg['action_url']).stop()
            except Exception as exc:errors.append(str(exc))
        return dict(stopped=True,errors=errors,blocking_sdk_preempted=False,demo=cfg['mode']=='demo')

    def close(self):
        with self.lock:
            if self.closed:return
            contexts=list(self.active.values())
        for ctx in contexts:self.cancel(ctx['id'])
        with self.lock:self.closed=True
        for ctx in contexts:ctx['thread'].join(timeout=.2)
