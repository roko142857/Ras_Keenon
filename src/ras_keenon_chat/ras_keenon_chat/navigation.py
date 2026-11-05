"""Optional local-navigation tools. No robot endpoint or SDK lives in this module."""
import json
from urllib.parse import urlparse
from ras_keenon_demos.transport import request_json


def loopback_url(value):
    p=urlparse(value)
    if p.scheme!='http' or p.hostname not in ('127.0.0.1','localhost','::1') or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('本地导航仅接受无路径/凭据的回环 HTTP 地址')
    return value.rstrip('/')


def schema(name, description, properties=None, required=None):
    return dict(name=name,description=description,parameters=dict(type='object',properties=properties or {},required=required or [],additionalProperties=False))


TOOLS=[
    schema('navigation_get_status','查询导航服务的地图与状态；按 backend/simulated 区分模拟和原厂只读数据。'),
    schema('navigation_preview','计算本地模拟地图的探索候选位置，不开始移动。'),
    schema('navigation_start_exploration','启动本地模拟区域探索。返回的是任务提交回执，必须查询状态才能说探索结束。',{'duration_s':dict(type='number',minimum=1,maximum=7200)}),
    schema('navigation_control','控制本地模拟任务：暂停、继续、停止、结束并保存。',{'action':dict(type='string',enum=['pause','resume','stop','finish'])},['action']),
    schema('navigation_run_route','按已登记命名点 ID 顺序执行本地模拟路线，不猜测地图坐标。',{'steps':dict(type='array',minItems=1,maxItems=50,items=dict(type='object',properties={'waypoint_id':dict(type='string'),'dwell_s':dict(type='number',minimum=0,maximum=300)},required=['waypoint_id'],additionalProperties=False))},['steps']),
    schema('navigation_save_map','导出当前已发布栅格地图和报告，不是原厂 SLAM 可恢复状态。')
]
NAMES={t['name'] for t in TOOLS}
PROMPT='''\n导航工具连接配置的导航服务。根据 backend、simulated 和 read_only 如实区分模拟任务或原厂只读观测。原厂只读状态不意味着定位已验证，更不意味着探索功能已开放。地图区域由网页圈定，不猜物体坐标。
用户要求探索/建图/导航路线时使用 navigation_*；多步骤路线先查询命名点，使用真实存在的 ID。
工具返回任务 state=exploring/navigating 只是已提交，不得说已经完成。只有 state=completed 才能描述模拟任务完成。
用户要求停止探索调用 navigation_control(action=stop)，不需要先使能。观测状态不等于真实机器人状态。'''


class NavigationClient:
    def __init__(self,url):self.url=loopback_url(url)
    def get(self,path):return request_json(self.url+path,timeout=4)
    def post(self,path,value):return request_json(self.url+path,value,timeout=8)
    def schemas(self):
        health=self.get('/api/health')
        return [t for t in TOOLS if t['name'] in ('navigation_get_status','navigation_save_map')] if health.get('read_only') else TOOLS
    def tool(self,name,arguments,identity):
        spec=next((s for s in TOOLS if s['name']==name),None)
        if spec is None:raise ValueError('未知导航工具')
        v=json.loads(arguments) if isinstance(arguments,str) else arguments
        if not isinstance(v,dict) or set(v)-set(spec['parameters']['properties']):raise ValueError('导航参数格式/字段无效')
        if set(spec['parameters']['required'])-set(v):raise ValueError('缺少导航参数')
        if name=='navigation_get_status':
            full=self.get('/api/state')
            result={k:full.get(k) for k in ['backend','simulated','robot_connection','map','pose','sensor_age_s','localization','mapping','factory_mode','task','boundary','exclusions','waypoints','read_only','raw_pose','pose_note','map_age_s','pose_age_s','blocked_reason','error']}
        elif name=='navigation_preview':result=self.post('/api/preview',{})
        elif name=='navigation_start_exploration':result=self.post('/api/start',dict(mode='explore',duration_s=v.get('duration_s',600),request_id=identity))
        elif name=='navigation_control':result=self.post('/api/control',v)
        elif name=='navigation_run_route':result=self.post('/api/start',dict(mode='route',steps=v['steps'],request_id=identity))
        else:result=self.post('/api/save',{})
        health=self.get('/api/health');mock=health['backend']=='mock'
        return dict(state='succeeded',demo=mock,backend=health['backend'],request_accepted=True,result=result,
                    note='本地模拟接口回执；任务是否完成以 result.task.state 为准；未连接机器人。' if mock else '原厂只读接口回执；没有执行建图或导航，位姿质量尚未确认。')
