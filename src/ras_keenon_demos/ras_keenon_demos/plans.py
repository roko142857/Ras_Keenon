"""Build an entire finite sequence before sending any movement command."""
from dataclasses import asdict, dataclass
import math
from .catalog import normalize
from .gestures import large_wave_keyframes


@dataclass(frozen=True)
class Step:
    command: str
    pause_s: float = 0.


@dataclass
class Plan:
    action: str
    parameters: dict
    steps: list
    start: dict

    def to_dict(self): return asdict(self)


def fmt(value): return format(float(value), '.12g')


def finite_values(value, n, label, maximum):
    if not isinstance(value, (list, tuple)) or len(value) != n:
        raise ValueError(label+' requires '+str(n)+' current joint readings')
    if any(type(x) not in (int, float) or not math.isfinite(x) or abs(x)>maximum for x in value):
        raise ValueError(label+' contains missing/nonfinite/out-of-range joint readings')
    return list(value)


def arm_segment(side, current, target, speed_factor, pause=0.):
    target = finite_values(target, 7, 'arm target', 3.2)
    count = max(1, math.ceil(max(abs(b-a) for a,b in zip(current,target))/math.radians(15.)))
    result = []
    for index in range(1,count+1):
        q = [a+(b-a)*index/count for a,b in zip(current,target)] if index<count else target
        result.append(Step('arm '+side+' '+' '.join(map(fmt,q))+' '+fmt(speed_factor),pause if index==count else 0.))
    return result


def build_plan(action, parameters=None, readings=None):
    p = normalize(action, parameters)
    readings = readings or {}
    body, steps, start = readings.get('body') or {}, [], {}
    if action in ('shake_hand', 'raise_arm'):
        side = p['side']
        initial = finite_values(body.get(side), 7, side, 3.2)
        start[side] = initial
        targets = []
        last_lift = 0.
        def target(lift, pause=0.):
            nonlocal last_lift
            while True:
                delta=max(-15.,min(15.,lift-last_lift))
                last_lift+=delta
                q=list(initial);q[3]-=math.radians(last_lift)
                finite_values(q,7,'arm target',3.2)
                final=abs(last_lift-lift)<1e-9
                targets.append(Step('arm '+side+' '+' '.join(map(fmt,q))+' '+fmt(p['speed_factor']),pause if final else 0.))
                if final:break
        # Same q[3] sign and <=15 degree increments as the recorded native_roundtrip.
        lift, current = p['lift_deg'], 0.
        while current < lift:
            current = min(lift, current+15.);target(current)
        if action == 'shake_hand':
            for _ in range(p['cycles']):
                target(max(0., lift-p['amplitude_deg']),p['pause_s'])
                target(lift+p['amplitude_deg'],p['pause_s'])
            target(lift)
        if p['return_to_start']:
            while current > 0:
                current = max(0., current-15.);target(current)
        steps = targets
    elif action == 'wave_large':
        side = p['side']
        current = finite_values(body.get(side),7,side,3.2)
        start[side] = list(current)
        for _, target, pause in large_wave_keyframes(p):
            steps.extend(arm_segment(side,current,target,p['speed_factor'],pause))
            current = target
        if p['return_to_start']:
            steps.extend(arm_segment(side,current,start[side],p['speed_factor']))
    elif action in ('turn_head','nod','shake_head'):
        initial = finite_values([body.get('yaw_deg'),body.get('pitch_deg')],2,'head',20.)
        start['head_deg'] = initial
        def head(yaw, pitch):
            values = finite_values([yaw,pitch],2,'head target',20.)
            steps.append(Step('head '+' '.join(map(fmt,values))+' '+fmt(p['settle_s'])))
        if action == 'turn_head':
            head(p['yaw_deg'], initial[1] if p['pitch_deg'] is None else p['pitch_deg'])
            if p['return_to_start']:head(*initial)
        else:
            axis = 1 if action == 'nod' else 0
            for _ in range(p['cycles']):
                for sign in (1,-1):
                    q = list(initial);q[axis] = max(-20.,min(20.,q[axis]+sign*p['amplitude_deg']))
                    head(*q)
            head(*initial)
    elif action == 'reset':
        # Validate both arms before dispatching any part of the reset.
        for side in ('right', 'left'):
            start[side] = finite_values(body.get(side), 7, side, 3.2)
        steps = [Step('stop'), Step('head 0 0 '+fmt(p['settle_s']))]
        for side in ('right', 'left'):
            initial = start[side]
            count = max(1, math.ceil(max(map(abs, initial))/math.radians(15.)))
            for index in range(1, count+1):
                q = [v*(1-index/count) for v in initial] if index < count else [0.]*7
                steps.append(Step('arm '+side+' '+' '.join(map(fmt,q))+' '+fmt(p['speed_factor'])))
        steps.append(Step('s '+fmt(p['height_m'])+' '+p['direction']))
    elif action == 'squat':steps=[Step('s '+fmt(p['height_m'])+' '+p['direction'])]
    elif action == 'stand':steps=[Step('stand')]
    elif action == 'move_forward':steps=[Step('base '+fmt(p['speed_mps'])+' 0 '+fmt(p['duration_s']))]
    elif action == 'rotate':
        sign = 1 if p['direction']=='left' else -1
        steps=[Step('base 0 '+fmt(sign*p['speed_radps'])+' '+fmt(p['duration_s']))]
    elif action == 'stop':steps=[Step('stop')]
    return Plan(action,p,steps,start)
