"""Command parser, with the units used by the original SOP."""
import math
import shlex
from dataclasses import dataclass

HELP = '''
h <degrees> [wait_s]       absolute head yaw; -20..20 deg
v <degrees> [wait_s]       absolute head pitch; -20..20 deg
head <yaw> <pitch> [wait_s]
s <height_m> [+|-]        squat 0..0.273 m; 0 = stand
sd <+|->                 set this server's default squat direction
b                        in-box preset: s 0.20 +
stand                    stand: s 0
g                        read current state, without a movement command
base <vx> <wz> <seconds>  finite signed x m/s and yaw rad/s; duration up to 3600 s
forward/back/left/right <speed> <seconds>
arm <left|right> <7 joint radians> [speed_factor=0.1]
stop                     cancel pending commands and stop the base
help / ?                 show help
q / quit                 leave terminal
Head wait_s defaults to 1.2 and is a post-command settling wait, not trajectory time.
'''


@dataclass(frozen=True)
class Instruction:
    op: str
    values: tuple = ()
    wait: float = 0.


def number(token, low, high, label):
    try: value = float(token)
    except (TypeError, ValueError): raise ValueError(f'{label} must be numeric')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} outside {low}..{high}')
    return value


def sign(token):
    if token in ('+', '+1', '1'): return 1
    if token in ('-', '-1'): return -1
    raise ValueError('direction must be + or -')


def parse(text):
    if not isinstance(text, str) or len(text) > 1024:
        raise ValueError('instruction must be text of at most 1024 characters')
    parts = shlex.split(text)
    if not parts: raise ValueError('empty instruction')
    op, args = parts[0].lower(), parts[1:]
    if op in ('help', '?', 'g', 'stop', 'q', 'quit', 'stand', 'b'):
        if args: raise ValueError(f'{op} takes no arguments')
        if op == 'stand': return Instruction('s', (0., None), 1.2)
        if op == 'b': return Instruction('s', (.2, 1), 1.2)
        return Instruction('help' if op == '?' else op)
    if op in ('h', 'v', 'head'):
        n = 2 if op == 'head' else 1
        if len(args) not in (n, n+1): raise ValueError(f'usage: {op} <angle(s)> [wait_s]')
        angles = tuple(number(v, -20, 20, 'angle degrees') for v in args[:n])
        wait = number(args[n], 0, 10, 'wait seconds') if len(args) > n else 1.2
        return Instruction(op, angles, wait)
    if op == 's':
        if len(args) not in (1, 2): raise ValueError('usage: s <height_m> [+|-]')
        return Instruction('s', (number(args[0], 0, .273, 'height metres'),
                                 sign(args[1]) if len(args) == 2 else None), 1.2)
    if op == 'sd':
        if len(args) != 1: raise ValueError('usage: sd <+|->')
        return Instruction('sd', (sign(args[0]),))
    if op in ('forward', 'back', 'left', 'right'):
        if len(args) != 2: raise ValueError(f'usage: {op} <speed> <seconds>')
        speed = number(args[0], 0, math.inf, 'speed')
        x = speed if op == 'forward' else -speed if op == 'back' else 0.
        yaw = speed if op == 'left' else -speed if op == 'right' else 0.
        return Instruction('base', (x, yaw, number(args[1], .001, 3600, 'duration seconds')))
    if op == 'base':
        if len(args) != 3: raise ValueError('usage: base <vx> <wz> <seconds>')
        x, yaw = [number(v, -math.inf, math.inf, 'base speed') for v in args[:2]]
        return Instruction('base', (x, yaw, number(args[2], .001, 3600, 'duration seconds')))
    if op == 'arm':
        if len(args) not in (8, 9) or args[0] not in ('left', 'right'):
            raise ValueError('usage: arm <left|right> <7 radians> [speed_factor]')
        q = tuple(number(v, -3.2, 3.2, 'joint radians') for v in args[1:8])
        speed = number(args[8], .1, 1., 'speed factor') if len(args) == 9 else .1
        return Instruction('arm', (args[0], *q, speed))
    raise ValueError('unknown instruction; type help')


def next_step(current, target, maximum):
    if len(current) != len(target) or maximum <= 0: raise ValueError('invalid step dimensions')
    return [a+max(-maximum, min(maximum, b-a)) for a, b in zip(current, target)]
