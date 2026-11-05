"""ROS-independent input validation and control ownership."""
import math
from collections import deque
from dataclasses import dataclass
from numbers import Real


class Refused(ValueError):
    pass


def numbers(values, count):
    values = list(values)
    if len(values) != count or any(isinstance(v, bool) or not isinstance(v, Real)
                                   or not math.isfinite(v) for v in values):
        raise Refused('wrong dimensions or nonfinite command')
    return [float(v) for v in values]


@dataclass
class Limits:
    base_speed: float = .02
    yaw_speed: float = .02
    command_age: float = .4
    base_timeout: float = .25
    burst_s: float = 2.
    yaw_min: float = -5.099835
    yaw_max: float = -2.999835
    pitch_min: float = -.233209
    pitch_max: float = -.033209
    head_step: float = .5
    waist_max: float = .05
    arm_step: float = .04

    def __post_init__(self):
        numbers(self.__dict__.values(), len(self.__dict__))
        if not (0 < self.base_speed and 0 < self.yaw_speed
                and 0 < self.base_timeout <= .5 and 0 < self.burst_s <= 2
                and 0 < self.command_age <= 1 and 0 < self.head_step <= .5
                and 0 <= self.waist_max <= .05 and 0 < self.arm_step <= .04
                and -20 <= self.yaw_min < self.yaw_max <= 20
                and -20 <= self.pitch_min < self.pitch_max <= 20):
            raise Refused('unsafe or invalid limits')


class Session:
    def __init__(self, limits=None):
        self.limits = limits or Limits()
        self.source = 'manual'
        self.epoch = 1
        self.armed = False

    def stop(self):
        self.armed = False
        self.epoch += 1

    def select(self, source):
        if source not in ('manual', 'vision', 'idle'):
            raise Refused('source must be manual, vision or idle')
        self.stop()
        self.source = source

    def arm(self):
        if self.source == 'idle':
            raise Refused('idle source cannot arm')
        self.epoch += 1
        self.armed = True

    def admit(self, source, epoch, stamp_s, now_s):
        numbers([stamp_s, now_s], 2)
        if not self.armed or source != self.source or epoch != self.epoch:
            raise Refused('disarmed, inactive source or stale epoch')
        age = now_s-stamp_s
        if not 0 <= age <= self.limits.command_age:
            raise Refused('expired or future-dated command')


def base_target(values, limits, allow_reverse=False, allow_turn=False):
    x, y, z, ax, ay, az = numbers(values, 6)
    if any(abs(v) > 1e-9 for v in (y, z, ax, ay)):
        raise Refused('only forward/backward x and yaw z supported')
    if abs(x) > limits.base_speed or abs(az) > limits.yaw_speed:
        raise Refused('base speed limit')
    if x and az:
        raise Refused('combined translation and rotation not commissioned')
    if x < 0 and not allow_reverse:
        raise Refused('reverse not enabled for this launch')
    if az and not allow_turn:
        raise Refused('turn not enabled for this launch')
    return x, az


def head_target(values, state, relative, limits):
    y, p = numbers(values, 2)
    current_y, current_p = numbers([state['yaw_deg'], state['pitch_deg']], 2)
    if relative:
        y += current_y
        p += current_p
    if not (limits.yaw_min <= y <= limits.yaw_max and limits.pitch_min <= p <= limits.pitch_max):
        raise Refused('head outside configured commissioning window')
    if max(abs(y-current_y), abs(p-current_p)) > limits.head_step+1e-9:
        raise Refused('head step exceeds configured limit')
    return y, p


def startup_head_window(yaw, pitch, yaw_span=1., pitch_span=.1):
    yaw, pitch, yaw_span, pitch_span = numbers([yaw, pitch, yaw_span, pitch_span], 4)
    if abs(yaw) > 20 or abs(pitch) > 20 or not (0 < yaw_span <= 1. and 0 < pitch_span <= .1):
        raise Refused('invalid measured head baseline or session window')
    return dict(yaw_min=max(-20., yaw-yaw_span), yaw_max=min(20., yaw+yaw_span),
                pitch_min=max(-20., pitch-pitch_span), pitch_max=min(20., pitch+pitch_span))


def waist_target(height, limits):
    numbers([height], 1)
    if not 0 <= height <= limits.waist_max:
        raise Refused('waist height outside configured range')
    return height


def arm_target(side, values, state, relative, speed, limits):
    if side not in ('left', 'right'):
        raise Refused('arm side must be left or right')
    q = numbers(values, 7)
    current = numbers(state[side], 7)
    numbers([speed], 1)
    if abs(speed-.1) > 1e-9:
        raise Refused('only commissioned arm speed factor 0.1 supported')
    if relative:
        q = [a+b for a, b in zip(q, current)]
    if any(abs(v) > 3.2 or abs(v-c) > limits.arm_step+1e-9 for v, c in zip(q, current)):
        raise Refused('arm absolute bound or per-command step exceeded')
    return q


def twist(x=0., yaw=0.):
    return {'linear': {'x': x, 'y': 0., 'z': 0.}, 'angular': {'x': 0., 'y': 0., 'z': yaw}}


class BaseLease:
    def __init__(self, limits):
        self.limits = limits
        self.target = (0., 0.)
        self.last_input = -math.inf
        self.deadline = None
        self.needs_release = False

    def set(self, target, now, continuous=False):
        if target == (0., 0.):
            self.stop()
            self.needs_release = False
            return
        if self.needs_release:
            raise Refused('burst expired: release movement key before next motion')
        if self.deadline is None or continuous:
            self.deadline = now+self.limits.burst_s
        self.target, self.last_input = target, now

    def stop(self):
        self.target = (0., 0.)
        self.deadline = None

    def output(self, now):
        if self.deadline is not None and now >= self.deadline:
            self.stop()
            self.needs_release = True
        elif now-self.last_input > self.limits.base_timeout:
            self.stop()
        return self.target


class SentCommands:
    """A bounded write history; matching late values are ambiguous, never source proof."""
    def __init__(self, window=.2):
        if not 0 < window <= .2:
            raise Refused('echo drain window must be at most 0.2 s')
        self.window = window
        self.sent = deque(maxlen=32)

    def record(self, target, now):
        if target != (0., 0.):
            self.sent.append((now, target))

    def classify(self, values, current, now):
        v = numbers(values, 6)
        if all(abs(x) <= 1e-6 for x in v):
            return 'zero'
        matches = any(0 <= now-at <= self.window and all(abs(a-b) <= 1e-6 for a, b in
                      zip(v, [target[0], 0., 0., 0., 0., target[1]]))
                      for at, target in self.sent)
        if not matches:
            return 'foreign'
        expected = [current[0], 0., 0., 0., 0., current[1]]
        if current != (0., 0.) and all(abs(a-b) <= 1e-6 for a, b in zip(v, expected)):
            return 'current'
        return 'drain'
