import math
import threading
import time
import unittest
from unittest.mock import patch

from ras_keenon_control.core import (Limits, Session, Refused, BaseLease, base_target,
                                    head_target, waist_target, arm_target, twist, numbers,
                                    SentCommands, startup_head_window)
from ras_keenon_control.backends import BaseLink, BodyWorker


class PolicyTests(unittest.TestCase):
    def test_demo_continuous_still_stops_when_input_is_lost(self):
        lease = BaseLease(Limits())
        for i in range(51):
            lease.set((.02, 0.), i*.1, continuous=True)
            self.assertEqual(lease.output(i*.1), (.02, 0.))
        self.assertEqual(lease.output(5.26), (0., 0.))

    def test_head_window_tracks_measured_pose_without_recentering(self):
        window = startup_head_window(-12., 3.)
        lim = Limits(**window)
        self.assertEqual(head_target([.5, 0.], {'yaw_deg': -12., 'pitch_deg': 3.}, True, lim), (-11.5, 3.))
        with self.assertRaises(Refused): startup_head_window(40., 0.)
        with self.assertRaises(Refused): startup_head_window(0., 0., 5.)

    def test_recent_echo_is_ambiguous_only_in_fixed_write_window(self):
        history = SentCommands()
        history.record((.02, 0.), 10.)
        self.assertEqual(history.classify([.02,0,0,0,0,0], (.02,0.), 10.1), 'current')
        self.assertEqual(history.classify([.02,0,0,0,0,0], (0.,0.), 10.1), 'drain')
        self.assertEqual(history.classify([.02,0,0,0,0,0], (0.,0.), 10.21), 'foreign')
        self.assertEqual(history.classify([-.02,0,0,0,0,0], (0.,0.), 10.1), 'foreign')
        self.assertEqual(history.classify([0,0,0,0,0,0], (0.,0.), 10.1), 'zero')

    def test_ros_fixed_arrays_and_boolean_rejection(self):
        import numpy as np
        q = numbers(np.array([0., .02], dtype=np.float64), 2)
        self.assertEqual(q, [0., .02])
        self.assertTrue(all(type(v) is float for v in q))
        with self.assertRaises(Refused): numbers([True], 1)

    def test_source_and_epoch_invalidate_old_commands(self):
        s = Session()
        s.arm()
        old = s.epoch
        s.admit('manual', old, 10., 10.1)
        s.select('vision')
        self.assertFalse(s.armed)
        s.arm()
        for source, epoch in [('manual', old), ('vision', old), ('manual', s.epoch)]:
            with self.assertRaises(Refused):
                s.admit(source, epoch, 10., 10.1)
        s.admit('vision', s.epoch, 10., 10.1)

    def test_future_expired_and_nonfinite_stamps(self):
        s = Session(); s.arm()
        for stamp, now in [(9., 10.), (11., 10.), (math.nan, 10.), (10., math.inf)]:
            with self.assertRaises(Refused):
                s.admit('manual', s.epoch, stamp, now)

    def test_base_axis_units_and_opt_in(self):
        lim = Limits()
        self.assertEqual(base_target([.02, 0, 0, 0, 0, 0], lim), (.02, 0.))
        for v in [[-.02, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, .02],
                  [.02, 0, 0, 0, 0, .02], [0, .01, 0, 0, 0, 0],
                  [math.nan, 0, 0, 0, 0, 0], [.1, 0, 0, 0, 0, 0]]:
            with self.assertRaises(Refused): base_target(v, lim)
        self.assertEqual(base_target([-.02, 0, 0, 0, 0, 0], lim, True), (-.02, 0.))
        self.assertEqual(base_target([0, 0, 0, 0, 0, -.02], lim, False, True), (0., -.02))

    def test_deadline_cannot_be_extended_by_key_repeat(self):
        lease = BaseLease(Limits())
        lease.set((.02, 0.), 0.)
        for i in range(1, 20): lease.set((.02, 0.), i*.1)
        self.assertEqual(lease.output(2.01), (0., 0.))
        with self.assertRaises(Refused): lease.set((.02, 0.), 2.02)
        lease.set((0., 0.), 2.1)
        lease.set((.02, 0.), 2.2)
        self.assertEqual(lease.output(2.3), (.02, 0.))

    def test_keyboard_loss_stops(self):
        lease = BaseLease(Limits())
        lease.set((.02, 0.), 1.)
        self.assertEqual(lease.output(1.26), (0., 0.))

    def test_head_window_step_and_unknown_feedback(self):
        state = {'yaw_deg': -4., 'pitch_deg': -.13}
        self.assertEqual(head_target([.5, 0.], state, True, Limits()), (-3.5, -.13))
        for values in [[1., 0.], [-10., 0.], [0., .2], [math.inf, 0.]]:
            with self.assertRaises(Refused): head_target(values, state, True, Limits())
        with self.assertRaises(Refused):
            head_target([0., 0.], {'yaw_deg': None, 'pitch_deg': None}, True, Limits())

    def test_waist_no_nan_or_large_squat(self):
        self.assertEqual(waist_target(.03, Limits()), .03)
        for height in [-.01, .3, math.nan]:
            with self.assertRaises(Refused): waist_target(height, Limits())

    def test_arm_step_radians_and_speed(self):
        q = [0.]*7
        state = {'left': q}
        self.assertEqual(arm_target('left', [.02]*7, state, True, .1, Limits()), [.02]*7)
        for side, target, speed in [('right', q, .1), ('left', [.2]*7, .1),
                                    ('left', q, 1.), ('left', [math.nan]*7, .1)]:
            with self.assertRaises((Refused, KeyError)):
                arm_target(side, target, state, True, speed, Limits())

    def test_configuration_cannot_expand_hard_caps(self):
        self.assertEqual(Limits(base_speed=.3, yaw_speed=.4).base_speed, .3)
        for changes in [dict(base_speed=float('inf')), dict(burst_s=5.), dict(yaw_min=-90.), dict(head_step=5.)]:
            with self.assertRaises(Refused): Limits(**changes)


class FakeSocket:
    instances = []
    def __init__(self, *args):
        self.sent = []
        self.index = 0
        self.motor = True
        self.competing = False
        self.disconnect = False
        self.closed = False
        self.instances.append(self)

    def send(self, msg): self.sent.append(msg)
    def close(self): self.closed = True
    def receive(self):
        time.sleep(.005)
        if self.disconnect: raise OSError('fake disconnect')
        topics = [('/motor_speed', twist()), ('/move_base/status', {'status_list': []}),
                  ('/urgency_button_status', {'data': False}), ('/motor_lock_status', {'status': 1})]
        t, m = topics[self.index % 4]; self.index += 1
        if self.competing:
            return {'topic': '/cmd_vel', 'msg': twist(.03)}
        if t == '/motor_speed' and not self.motor: return None
        return {'topic': t, 'msg': m}


class BaseTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch('ras_keenon_control.rosbridge.Rosbridge', FakeSocket)
        self.patch.start()
        self.link = BaseLink(True, Limits(), 'fake', 9091)
        self.socket = FakeSocket.instances[-1]
        until = time.monotonic()+2
        while not self.link.ready() and time.monotonic() < until: time.sleep(.01)
        self.assertTrue(self.link.ready())

    def tearDown(self): self.link.close(); self.patch.stop()

    def test_zero_only_startup_does_not_publish(self):
        self.assertFalse([m for m in self.socket.sent if m['op'] == 'publish'])

    def test_nonzero_then_lease_loss_causes_sustained_zero(self):
        self.link.command((.02, 0.)); time.sleep(.12)
        self.assertTrue(any(m.get('msg', {}).get('linear', {}).get('x') == .02 for m in self.socket.sent))
        time.sleep(.3)
        self.assertEqual(self.link.output, (0., 0.))
        zeros = [m for m in self.socket.sent if m['op'] == 'publish' and m['msg'] == twist()]
        self.assertGreater(len(zeros), 1)

    def test_feedback_loss_latches_fault_and_stops(self):
        self.link.command((.02, 0.)); time.sleep(.08); self.socket.motor = False
        for _ in range(8):
            try: self.link.command((.02, 0.))
            except Refused: pass
            time.sleep(.05)
        self.assertTrue(self.link.fault)
        self.assertEqual(self.link.output, (0., 0.))

    def test_competing_nonzero_latches_fault(self):
        self.link.command((.02, 0.)); time.sleep(.07); self.socket.competing = True; time.sleep(.08)
        self.assertIn('competing', self.link.fault)
        self.assertEqual(self.link.output, (0., 0.))

    def test_disconnect_never_reconnects(self):
        count = len(FakeSocket.instances)
        self.socket.disconnect = True; time.sleep(.1)
        self.assertEqual(len(FakeSocket.instances), count)
        self.assertIn('connection failure', self.link.fault)

    def test_delayed_matching_echo_stops_and_requires_release(self):
        now = time.monotonic()
        self.link.history.record((.02, 0.), now)
        self.link.output = (0., 0.)
        with self.link.lock:
            self.link.observe({'topic': '/cmd_vel', 'msg': twist(.02)}, now+.01)
            self.assertEqual(self.link.fault, '')
            self.assertTrue(self.link.lease.needs_release)
            self.assertFalse(self.link.stop_feedback_verified)
            self.assertFalse(self.link.ready())
            with self.assertRaises(Refused): self.link.command((.02, 0.))
        time.sleep(2.15)
        self.assertTrue(self.link.ready())
        with self.assertRaises(Refused): self.link.command((.02, 0.))
        self.link.command((0., 0.))
        self.link.command((.02, 0.))

    def test_matching_foreign_command_cannot_extend_drain_window(self):
        now = time.monotonic()
        self.link.history.record((.02, 0.), now)
        with self.link.lock:
            self.link.observe({'topic': '/cmd_vel', 'msg': twist(.02)}, now+.1)
            with self.assertRaises(Refused):
                self.link.observe({'topic': '/cmd_vel', 'msg': twist(.02)}, now+.21)


class BodyTests(unittest.TestCase):
    def test_native_rejection_does_not_kill_sdk_owner(self):
        class Channel:
            def __init__(self, executable): pass
            def request(self, op, values=()):
                if op == 'HEAD': raise Refused('stale feedback')
                return dict(yaw_deg=-12., pitch_deg=3., left=[0.]*7, right=[0.]*7)
            def close(self): return True
        with patch('ras_keenon_control.backends.NativeChannel', Channel):
            body = BodyWorker(True, 'fake', lambda ep: True)
            try:
                until = time.monotonic()+1
                while not body.state.get('ready') and time.monotonic() < until: time.sleep(.01)
                body.submit(1, 'HEAD', [-11.5, 3.])
                time.sleep(.2)
                self.assertEqual(body.fault, '')
                self.assertEqual(body.last_rejection, 'stale feedback')
                self.assertTrue(body.state['ready'])
            finally: body.close()


if __name__ == '__main__': unittest.main()
