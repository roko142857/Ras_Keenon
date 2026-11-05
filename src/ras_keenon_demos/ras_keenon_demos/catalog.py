"""Action parameters shared by scripts, HTTP and LLM tool definitions."""
import math


def field(kind, default=None, **kw):
    return dict(type=kind, default=default, **kw)


def num(default, low, high, description):
    spec = field('number', default, minimum=low, description=description)
    if high is not None: spec['maximum'] = high
    return spec


SIDE = field('string', 'right', enum=['right', 'left'], description='机械臂侧别')
SPEED = num(.1, .1, 1., 'SDK 速度系数；不是 rad/s')
RETURN = field('boolean', True, description='完成后返回本次动作开始时的姿态；取消时不自动返回')
CYCLES = field('integer', 2, minimum=1, maximum=10, description='往复次数')
SETTLE = num(.3, 0., 3., '每个头部目标提交后的等待秒数')
SPECS = {
    'shake_hand': dict(description='旧版单肘摆手：仅改变肘轴 q[3]，相对当前姿态往复，默认返回起点；不表示与人握手。', parameters={
        'side': SIDE, 'lift_deg': num(30., 0., 60., '肘轴 q[3] 相对抬起角度，度'),
        'amplitude_deg': num(5., 0., 15., '招手时肘轴往复幅度，度'),
        'cycles': CYCLES, 'speed_factor': SPEED, 'pause_s': num(.2, 0., 3., '每个臂目标后的停留秒数'),
        'return_to_start': RETURN}),
    'wave_large': dict(description='多关节大幅挥手：向外展开手臂，肩肘配合举手过头顶，肩部、肘部和腕部往复摆动，默认收回本次起点。', parameters={
        'side': SIDE, 'shoulder_lift_deg': num(75., 60., 90., '肩部抬举目标角幅值，度；使用左右镜像的绝对姿态'),
        'elbow_bend_deg': num(25., 15., 40., '举高手臂时肘部弯曲角幅值，度'),
        'amplitude_deg': num(10., 0., 20., '肩部往复角幅值，肘部联动幅值为其一半，度'),
        'wrist_amplitude_deg': num(20., 0., 35., '腕部往复角幅值，度'),
        'cycles': CYCLES, 'speed_factor': SPEED, 'pause_s': num(.2, 0., 3., '举高和每个摆动端点停留秒数'),
        'return_to_start': RETURN}),
    'raise_arm': dict(description='相对本轮起点抬起一只机械臂的肘关节。', parameters={
        'side': SIDE, 'lift_deg': num(30., 0., 60., '肘轴相对抬起角度，度'),
        'speed_factor': SPEED, 'return_to_start': field('boolean', False, description='是否返回起点')}),
    'turn_head': dict(description='头部转到指定绝对角度；未指定 pitch 时保留当前俯仰。', parameters={
        'yaw_deg': num(15., -20., 20., '绝对 yaw，度'),
        'pitch_deg': field(['number', 'null'], None, minimum=-20., maximum=20., description='绝对 pitch，度；null 保留当前值'),
        'settle_s': SETTLE, 'return_to_start': field('boolean', False, description='是否返回动作前头部姿态')}),
    'nod': dict(description='点头，围绕当前 pitch 往复并返回。', parameters={
        'amplitude_deg': num(5., .1, 15., '俯仰幅度，度'), 'cycles': CYCLES, 'settle_s': SETTLE}),
    'shake_head': dict(description='摇头，围绕当前 yaw 往复并返回。', parameters={
        'amplitude_deg': num(8., .1, 15., '左右幅度，度'), 'cycles': CYCLES, 'settle_s': SETTLE}),
    'squat': dict(description='下蹲到指定高度；结束后保持下蹲目标。', parameters={
        'height_m': num(.03, 0., .273, '下蹲高度，米；不是绝对身高'),
        'direction': field('string', '-', enum=['-', '+'], description='腰部弯曲方向')}),
    'stand': dict(description='直立，发送 s 0。', parameters={}),
    'reset': dict(description='复位：停止底盘，头部和双臂各关节归零；腰部默认下蹲 0.03 m，结束后保持该姿态。不是编码器校准。', parameters={
        'height_m': num(.03, 0., .273, '复位后的下蹲高度，米；0 为直立'),
        'direction': field('string', '-', enum=['-', '+'], description='腰部弯曲方向'),
        'speed_factor': SPEED, 'settle_s': SETTLE}),
    'move_forward': dict(description='底盘前进指定时长，到时发送零速度。', parameters={
        'speed_mps': num(.02, 0., None, '前进速度，m/s；有限非负数，无应用层上限'), 'duration_s': num(1., .001, 3600., '持续秒数')}),
    'rotate': dict(description='底盘原地左右旋转指定时长。', parameters={
        'direction': field('string', 'left', enum=['left', 'right'], description='旋转方向'),
        'speed_radps': num(.02, 0., None, '角速度，rad/s；有限非负数，无应用层上限'), 'duration_s': num(1., .001, 3600., '持续秒数')}),
    'stop': dict(description='取消整个演示序列，并向现有控制器发送停止。阻塞 SDK 轨迹无法保证即时打断。', parameters={}),
}


def normalize(action, parameters=None):
    if action not in SPECS: raise ValueError('unknown action: '+str(action))
    if parameters is None: parameters = {}
    if not isinstance(parameters, dict): raise ValueError('parameters must be an object')
    fields = SPECS[action]['parameters']
    extra = set(parameters)-set(fields)
    if extra: raise ValueError('unknown parameter(s): '+', '.join(sorted(extra)))
    result = {}
    for name, spec in fields.items():
        value = parameters.get(name, spec['default'])
        kind = spec['type']
        if isinstance(kind, list) and value is None:
            result[name] = None; continue
        if kind in ('number', 'integer') or isinstance(kind, list):
            if type(value) not in (int, float) or (kind == 'integer' and type(value) is not int):
                raise ValueError(name+' must be '+str(kind))
            if not math.isfinite(value) or value < spec.get('minimum', -math.inf) or value > spec.get('maximum', math.inf):
                raise ValueError(name+' out of range')
        elif kind == 'boolean':
            if type(value) is not bool: raise ValueError(name+' must be boolean')
        elif not isinstance(value, str): raise ValueError(name+' must be text')
        if 'enum' in spec and value not in spec['enum']: raise ValueError(name+' invalid choice')
        result[name] = value
    if action == 'shake_hand' and result['lift_deg']+result['amplitude_deg'] > 60:
        raise ValueError('lift_deg + amplitude_deg must be <= 60 degrees')
    return result


def tool_schemas():
    # Provider-neutral function definitions; adapters can add the provider's wrapper.
    return [dict(name='robot_'+name, description=spec['description'], parameters=dict(
        type='object', properties=spec['parameters'], additionalProperties=False))
        for name, spec in SPECS.items()]


def decode_tool_call(call):
    import json
    if not isinstance(call, dict): raise ValueError('tool call must be an object')
    name = call.get('name')
    if not isinstance(name, str) or not name.startswith('robot_'):
        raise ValueError('unknown robot tool')
    arguments = call.get('arguments', {})
    if isinstance(arguments, str): arguments = json.loads(arguments)
    action = name[6:]
    return action, normalize(action, arguments), call.get('call_id')
