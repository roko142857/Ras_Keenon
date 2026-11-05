"""Mirrored seven-joint keyframes, using the robot's whole_dh arm-chain convention.

All values returned here are radians. Model geometry has been checked offline;
this profile has not been taught or accepted on the physical robot.
"""
import math


def large_wave_keyframes(parameters):
    p = parameters
    mirror = -1. if p['side'] == 'right' else 1.
    clear = [mirror*20., 0., 0., mirror*p['elbow_bend_deg'], 0., 0., 0.]
    raised = list(clear)
    raised[1] = mirror*p['shoulder_lift_deg']
    frames = [('clear', list(clear), 0.), ('raise', list(raised), p['pause_s'])]
    for _ in range(p['cycles']):
        for direction in (1., -1.):
            q = list(raised)
            q[0] += mirror*direction*p['amplitude_deg']
            q[3] += mirror*direction*p['amplitude_deg']/2.
            q[5] = mirror*direction*p['wrist_amplitude_deg']
            frames.append(('swing', q, p['pause_s']))
    frames.append(('recenter', list(raised), 0.))
    if p['return_to_start']: frames.append(('lower', list(clear), 0.))
    return [(name, list(map(math.radians, q)), pause) for name, q, pause in frames]
