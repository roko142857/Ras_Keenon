"""OpenAI-compatible Chat Completions transport and explicitly labelled offline demo."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ProviderError(Exception):
    def __init__(self,message,status=None,request_id=None):
        super().__init__(message);self.status=status;self.request_id=request_id


def chat_url(base):
    value=base.strip().rstrip('/')
    parsed=urllib.parse.urlparse(value)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('API 地址必须为不含用户名和密码的 http(s) URL。')
    if parsed.query or parsed.fragment:raise ValueError('API 地址不能包含 query 或 fragment。')
    return value if value.endswith('/chat/completions') else value+'/chat/completions'


def complete(config,messages,tools):
    if config['mode']=='demo':return demo_complete(messages,tools)
    if any(c in config.get('api_key','') for c in ('\r','\n')):
        raise ProviderError('API Key 不能包含换行，请重新复制。')
    if not config.get('base_url') or not config.get('model'):
        raise ProviderError('请先在模型设置中填写 API 地址和模型名称，或选择演示模式。')
    body=dict(model=config['model'],messages=messages,tools=[
        dict(type='function',function=dict(f,strict=False)) for f in tools],
        tool_choice='auto',parallel_tool_calls=False,stream=False)
    headers={'Content-Type':'application/json'}
    if config.get('api_key'):headers['Authorization']='Bearer '+config['api_key']
    request=urllib.request.Request(chat_url(config['base_url']),json.dumps(body,ensure_ascii=False,allow_nan=False).encode(),headers)
    start=time.monotonic()
    try:
        with urllib.request.urlopen(request,timeout=60) as response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ProviderError('模型回执过大。')
            value=json.loads(raw)
            json.dumps(value,allow_nan=False)  # Reject malformed nonfinite JSON before recording it.
            request_id=response.headers.get('x-request-id')
            status=response.status
    except urllib.error.HTTPError as exc:
        text=exc.read(16384).decode(errors='replace')
        try:
            error=json.loads(text).get('error',{})
            text=error.get('message',text) if isinstance(error,dict) else str(error)
        except ValueError:pass
        raise ProviderError('模型 API 返回错误：'+text[:2000],exc.code,exc.headers.get('x-request-id')) from exc
    except (OSError,ValueError) as exc:raise ProviderError('模型 API 请求失败：'+str(exc)) from exc
    if not isinstance(value,dict) or not value.get('choices'):
        raise ProviderError('模型回执缺少 choices，请确认使用 Chat Completions 兼容接口。',status,request_id)
    return value,dict(http_status=status,request_id=request_id,latency_ms=round((time.monotonic()-start)*1000,1))


def demo_complete(messages,tools=()):
    """Deterministic UI demonstration, never represented as a real model request."""
    time.sleep(.18)
    if messages[-1]['role']=='tool':
        outputs=[]
        for m in reversed(messages):
            if m['role']!='tool':break
            outputs.append(json.loads(m['content']))
        states=[v.get('state') for v in outputs]
        navigation=any(v.get('request_accepted') for v in outputs)
        text=('本地模拟导航接口已返回；任务进度请查看地图任务状态，未连接机器人。' if navigation else '演示动作流程已完成；实际机器人没有运动。') if all(s=='succeeded' for s in states) else '本次演示已停止或出现错误，请查看回执。'
        if any(v.get('backend')=='native-readonly' for v in outputs):text='原厂只读查询已返回；地图数据来自机器人，定位质量尚未确认，未执行建图或导航。'
        message=dict(role='assistant',content=text)
    else:
        text=messages[-1]['content'];calls=[]
        patterns=[('reset','复位|归零'),('nod','点头'),
                  ('shake_head','摇头'),('squat','下蹲|蹲下'),('stand','站起|直立'),
                  ('turn_head','转头|头向.{0,2}转'),('raise_arm','抬臂|抬手'),
                  ('move_forward','前进|往前'),('rotate','原地旋转|转身')]
        found=[]
        wave_pattern=r'wave_large|shake_hand|(?:大幅(?:度)?|小幅|屈肘|原来的)?挥(?:[一二两三四五六七八九十\d]+次)?手|(?:举手|抬臂|抬手)过头顶(?:挥手)?|屈肘摆手|小幅招手|晃手'
        waves=list(re.finditer(wave_pattern,text))
        for match in waves:
            small=bool(re.search(r'shake_hand|小幅|屈肘|原来的|晃手',match.group()))
            found.append((match.start(),'shake_hand' if small else 'wave_large'))
        for action,pattern in patterns:
            match=re.search(pattern,text)
            if match and not (action=='raise_arm' and any(w.start()<=match.start()<w.end() for w in waves)):
                found.append((match.start(),action))
        if re.search('停下|停止',text):found=[(0,'stop')]
        for _,action in sorted(found):
            p={}
            if action in ('shake_hand','wave_large'):
                p['cycles']=2 if re.search('两次|二次|2次',text) else 1
                if '左' in text:p['side']='left'
            if action=='turn_head':p['yaw_deg']=-15 if '右' in text else 15
            if action=='squat':p['height_m']=.03
            if action=='move_forward':p['duration_s']=1
            calls.append(dict(id='demo_call_'+uuid.uuid4().hex[:12],type='function',
                function=dict(name='robot_'+action,arguments=json.dumps(p))))
        message=dict(role='assistant',content='我会按顺序演示这些动作。' if calls else
                     '你好，我是机器人对话助手。你可以让我复位、挥手、转头、点头或下蹲。当前为演示模式，不调用真实模型，也不驱动机器人。')
        nav_available=any(t.get('name')=='navigation_get_status' for t in tools)
        if nav_available and (re.search('探索|建图|导航|地图|路线',text) or (all(t['name'].startswith('navigation_') for t in tools) and re.search('停止|停下|暂停|继续|恢复|保存',text))):
            nav='navigation_get_status';args={}
            if re.search('预览|候选',text):nav='navigation_preview'
            elif re.search('停止|停下|结束|暂停|继续|恢复',text):
                nav='navigation_control';args={'action':'finish' if '保存' in text else 'pause' if '暂停' in text else 'resume' if re.search('继续|恢复',text) else 'stop'}
            elif re.search('保存',text):nav='navigation_save_map'
            elif re.search('开始|启动|探索一下|探索全部',text):nav='navigation_start_exploration'
            message['content']='我会调用导航查询接口。'
            calls=[dict(id='demo_nav_'+uuid.uuid4().hex[:12],type='function',function=dict(name=nav,arguments=json.dumps(args)))]
        if nav_available and not any(t['name']=='navigation_start_exploration' for t in tools):
            message['content']='当前为原厂只读模式，尚未开放自主探索；可以查询导航状态或保存栅格快照。'
        if tools:
            allowed={t['name'] for t in tools};calls=[c for c in calls if c['function']['name'] in allowed]
        if nav_available and not calls and any(t['name']=='navigation_start_exploration' for t in tools):message['content']='这是本地导航演示。可以输入开始探索、暂停探索、继续探索、查询导航状态或保存地图；不会连接机器人。'
        if calls:message['tool_calls']=calls
    return dict(id='demo_'+uuid.uuid4().hex[:12],model='demo-rules',choices=[
        dict(index=0,message=message,finish_reason='tool_calls' if message.get('tool_calls') else 'stop')],usage=None),dict(
        http_status=None,request_id=None,latency_ms=180.,demo=True)
