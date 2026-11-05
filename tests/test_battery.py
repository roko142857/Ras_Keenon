"""Battery telemetry validation and read-only reconnect behavior, without a robot."""
import time
import unittest
from ras_keenon_commands.battery import BatteryCache, BatteryMonitor, mock_battery


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now=10.
        self.cache=BatteryCache(monotonic=lambda:self.now,wall_time=lambda:1000+self.now)
        self.cache.connection(True)

    def test_unknown_is_not_zero_and_mock_has_no_real_measurement(self):
        self.assertIsNone(self.cache.snapshot()['percentage'])
        self.assertEqual(self.cache.snapshot()['status'],'waiting')
        self.assertEqual(mock_battery()['status'],'mock')
        self.assertIsNone(mock_battery()['percentage'])

    def test_normalized_percentage_and_unconfirmed_zero_are_distinct(self):
        self.cache.ingest(dict(percentage=.725,present=True))
        value=self.cache.snapshot()
        self.assertEqual((value['percentage'],value['quality'],value['status']),(72.5,'reported','live'))
        self.cache.ingest(dict(percentage=0.,present=False,voltage=0.,current=0.))
        value=self.cache.snapshot()
        self.assertEqual(value['percentage'],0.)
        self.assertEqual(value['quality'],'unverified')
        self.assertIn('未确认',value['note'])
        self.assertNotIn('charging',value)

    def test_missing_nonfinite_and_wrong_units_are_rejected(self):
        for value in (None,True,-1,1.01,100,float('nan'),float('inf'),'0.5'):
            with self.subTest(value=value):
                self.assertFalse(self.cache.ingest(dict(percentage=value)))
                self.assertIsNone(self.cache.snapshot()['percentage'])
        self.assertFalse(self.cache.ingest(None))

    def test_expiry_disconnect_and_reconnect_retain_old_value_without_claiming_live(self):
        self.cache.ingest(dict(percentage=.5,present=True))
        self.now+=31
        self.assertEqual(self.cache.snapshot()['status'],'stale')
        self.cache.connection(False,'socket closed')
        self.assertEqual(self.cache.snapshot()['percentage'],50.)
        self.cache.connection(True)
        self.assertEqual(self.cache.snapshot()['status'],'stale')
        self.cache.ingest(dict(percentage=.49,present=True))
        self.assertEqual(self.cache.snapshot()['status'],'live')

    def test_unknown_49_never_implies_direct_adapter_or_charging(self):
        self.cache.ingest(dict(percentage=0.,present=False,power_supply_status=0))
        self.assertTrue(self.cache.ingest_charge(dict(state=49)))
        power=self.cache.snapshot()['power']
        self.assertIsNone(power['adapter_connected'])
        self.assertEqual(power['charging_status'],'unknown')
        self.assertEqual(power['raw_charge_state'],49)
        self.assertTrue(power['raw_charge_fresh'])

    def test_standard_charging_status_still_does_not_identify_adapter(self):
        self.cache.ingest(dict(percentage=.8,present=True,power_supply_status=1))
        power=self.cache.snapshot()['power']
        self.assertEqual(power['charging_status'],'charging')
        self.assertIsNone(power['adapter_connected'])
        self.now+=31
        self.assertEqual(self.cache.snapshot()['power']['charging_status'],'unknown')

    def test_charge_heartbeat_does_not_refresh_old_battery_reading(self):
        self.cache.ingest(dict(percentage=.5,present=True))
        self.now+=31;self.cache.ingest_charge(dict(state=49))
        state=self.cache.snapshot()
        self.assertEqual(state['status'],'stale')
        self.assertTrue(state['power']['raw_charge_fresh'])


class MonitorTests(unittest.TestCase):
    def test_subscription_only_and_invalid_motor_command_never_sent(self):
        sent=[]
        class Socket:
            def settimeout(self,value):pass
        class Transport:
            def __init__(self,*args):self.sock=Socket();self.first=True
            def send(self,message):sent.append(message)
            def receive(self):
                if self.first:
                    self.first=False
                    return dict(op='publish',topic='/battery_state',msg=dict(percentage=.8,present=True))
                time.sleep(.005)
            def close(self):pass
        monitor=BatteryMonitor(transport=Transport)
        try:
            deadline=time.monotonic()+1
            while monitor.snapshot()['percentage'] is None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(monitor.snapshot()['percentage'],80.)
        finally:monitor.close()
        self.assertEqual([v['op'] for v in sent],['subscribe','subscribe','unsubscribe','unsubscribe'])
        self.assertEqual({v['topic'] for v in sent},{'/battery_state','/charge_state_fromSTM32'})


if __name__=='__main__':unittest.main()
