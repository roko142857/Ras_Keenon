"""Exercise held-input/release semantics and direct native calls without a robot."""
import math
from pathlib import Path
import time
import unittest
from ras_keenon_commands.direct_io import KeyLatch, DirectBase, DirectBody, Ticket
from ras_keenon_commands.instructions import parse
import test_native_bridge as native


def until(condition, seconds=2):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        if condition(): return True
        time.sleep(.005)
    return False


class KeyTests(unittest.TestCase):
    def test_hold_release_and_old_packet(self):
        latch = KeyLatch()
        self.assertEqual(latch.update(['w'], 'a', 1, 0), (True, True))
        self.assertEqual(latch.read(.1), {'w'})
        latch.update([], 'a', 3, .15)
        self.assertEqual(latch.update(['w'], 'a', 2, .16), (False, False))
        self.assertFalse(latch.read(.2))

    def test_heartbeat_preserves_hold_then_loss_clears(self):
        latch = KeyLatch()
        for n in range(100): latch.update(['w','a'], 'a', n, n*.1)
        self.assertEqual(latch.read(10.), {'w', 'a'})
        self.assertFalse(latch.read(10.3))

    def test_invalid_input(self):
        latch = KeyLatch()
        for keys in (['z'], 'w', [None], [['w']]):
            with self.assertRaises((ValueError, TypeError)):
                latch.update(keys, 'a', 1, 0)

    def test_configured_heartbeat_timeout(self):
        latch = KeyLatch(timeout=.15)
        latch.update(['w'], 'a', 1, 0)
        self.assertEqual(latch.read(.14), {'w'})
        self.assertFalse(latch.read(.16))


class DirectTests(unittest.TestCase):
    def test_base_continuous_beyond_two_seconds_and_release(self):
        base = DirectBase(False, .01)
        try:
            end = time.monotonic()+2.1
            while time.monotonic() < end:
                base.set(.02, -.02); time.sleep(.01)
            self.assertEqual(base.sent, (.02, -.02))
            base.set(force=True)
            self.assertTrue(until(lambda: base.sent == (0., 0.)))
        finally: base.close()

    def test_base_caller_loss_zeroes(self):
        base = DirectBase(False, .005)
        try:
            base.set(.02)
            self.assertTrue(until(lambda: base.sent[0] > 0))
            self.assertTrue(until(lambda: base.sent == (0., 0.), 1))
        finally: base.close()

    def test_configured_base_timeout(self):
        base = DirectBase(False, .005, timeout=.6)
        try:
            base.set(.015, -.01)
            self.assertTrue(until(lambda: base.sent == (.015, -.01)))
            time.sleep(.3)  # Beyond the historical hard-coded .25 s timeout.
            self.assertEqual(base.sent, (.015, -.01))
            self.assertTrue(until(lambda: base.sent == (0., 0.), 1))
        finally: base.close()

    def test_head_press_moves_release_holds(self):
        latch = KeyLatch()
        body = DirectBody(False, '', lambda: latch.read(time.monotonic()), period=.005)
        try:
            original = body.data['yaw_deg']
            latch.update(['h'], 'a', 1, time.monotonic())
            self.assertTrue(until(lambda: body.data['yaw_deg'] > original+.05))
            latch.update([], 'a', 2, time.monotonic()); body.interrupt()
            time.sleep(.03); stopped = body.data['yaw_deg'];time.sleep(.05)
            self.assertEqual(body.data['yaw_deg'], stopped)
        finally: body.close()

    def test_direct_large_head_target_without_enable(self):
        body = DirectBody(False, '', lambda: frozenset(), period=.005)
        try:
            ticket = Ticket(parse('head -19 12 0'))
            body.submit(ticket)
            self.assertTrue(ticket.done.wait(1));self.assertTrue(ticket.success)
            self.assertEqual(body.data['yaw_deg'], -19)
        finally: body.close()

    def test_replacement_and_stop_cancel_wait(self):
        body = DirectBody(False, '', lambda: frozenset(), period=.005)
        try:
            old = Ticket(parse('h -12 5'));body.submit(old)
            self.assertTrue(until(lambda: body.data['yaw_deg'] == -12))
            new = Ticket(parse('v 5 5'));body.submit(new)
            self.assertTrue(old.done.wait(1));self.assertFalse(old.success)
            self.assertTrue(until(lambda: body.data['pitch_deg'] == 5))
            body.interrupt()
            self.assertTrue(new.done.wait(1));self.assertFalse(new.success)
        finally: body.close()

    def test_document_commands_units_direction(self):
        self.assertEqual(parse('b').values, (.2, 1))
        self.assertEqual(parse('sd -').values, (-1,))
        self.assertEqual(parse('stand').values, (0., None))
        self.assertEqual(parse('base .02 -.02 10').values, (.02, -.02, 10.))
        self.assertEqual(parse('arm left 0 0 0 0 0 0 0 .5').values[-1], .5)
        with self.assertRaises(ValueError): parse('h nan')

    def test_base_speed_above_previous_cap_and_nonfinite_rejected(self):
        self.assertEqual(parse('base .2 -.5 1').values,(.2,-.5,1.))
        self.assertEqual(parse('back .3 1').values,(-.3,0.,1.))
        self.assertEqual(parse('left .6 1').values,(0.,.6,1.))
        for command in ('base inf 0 1','base 0 nan 1','forward -1 1','right nan 1'):
            with self.assertRaises(ValueError):parse(command)


class DirectNativeTests(native.NativeTests):
    SOURCE = Path(__file__).resolve().parents[1]/'src/ras_keenon_command_native/src/sdk_bridge.cpp'

    def test_refused_step_does_not_terminate_owner(self):
        self.start()
        self.assertTrue(self.request('HEAD 18 -12')['ok'])
        self.assertEqual(self.request('READ')['yaw_deg'], 18)
        self.assertTrue(self.request('HOLD')['ok'])
        self.assertFalse(self.request('HEAD 21 0')['ok'])
        self.assertTrue(self.request('READ')['ok'])

    def test_stale_callback_refuses_head_motion(self):
        # Direct control reports stale feedback, but does not gate motion on it.
        self.start(RAS_FAKE_QUIET_AFTER_S='.55');time.sleep(.4)
        self.assertFalse(self.request('READ')['head_feedback_fresh'])
        self.assertTrue(self.request('HEAD -4 3')['ok'])

    def test_direct_channel_handshake_and_requests(self):
        from ras_keenon_commands.channel import DirectChannel
        channel = DirectChannel(str(self.binary))
        try:
            self.assertTrue(channel.request('HEAD', [15, -5])['ok'])
            self.assertEqual(channel.request('READ')['yaw_deg'], 15)
            with self.assertRaises(RuntimeError): channel.request('HEAD', [21, 0])
            self.assertTrue(channel.request('HEAD', [-15, 0])['ok'])
        finally: self.assertTrue(channel.close())

    def test_positive_waist_and_full_arm_target(self):
        self.start()
        self.assertTrue(self.request('WAIST 0.20 1')['ok'])
        q = self.request('READ')['waist']
        self.assertAlmostEqual(q[1], math.acos(1-.2/.766))
        self.assertAlmostEqual(q[2], 2*q[1])
        self.assertTrue(self.request('ARM left 0 0 0 .5 0 0 0 .5')['ok'])
        self.assertEqual(self.request('READ')['left'][3], .5)

    def test_arm_error_code_and_feedback_metadata(self):
        self.start(RAS_FAKE_SDK_FAIL='1')
        result=self.request('ARM right 0 0 0 0 0 0 0 .1')
        self.assertFalse(result['ok']);self.assertEqual(result['sdk_return'],0)
        self.assertIn('right arm SDK returned code 0',result['error'])
        self.assertEqual(self.request('READ')['arm_status']['right']['joint_ids'],list(range(16,23)))
        status=self.request('READ')['arm_status']['left']
        self.assertTrue(status['feedback_fresh'])
        self.assertTrue(all(n>0 for n in status['feedback_sequence']))

    def test_arm_cache_zero_does_not_imply_online(self):
        self.start(RAS_FAKE_NO_ARMS='1')
        state=self.request('READ')
        self.assertEqual(state['right'],[0.]*7)
        self.assertFalse(state['arm_status']['right']['feedback_fresh'])
        self.assertEqual(state['arm_status']['right']['feedback_age_s'],[None]*7)

    def test_arm_targets_stay_radians_with_degree_vendor_getters(self):
        self.start(RAS_FAKE_REALISTIC_POSE='1')
        state=self.request('READ')
        self.assertAlmostEqual(state['right_deg'][2],176.)
        q=list(state['right']);q[3]-=math.radians(15)
        result=self.request('ARM right '+' '.join(map(str,q))+' .1')
        self.assertTrue(result['ok'])
        after=self.request('READ')
        self.assertAlmostEqual(after['right'][3],math.radians(-16))
        self.assertAlmostEqual(after['right_deg'][3],-16)
        self.assertAlmostEqual(after['right_deg'][2],176)
