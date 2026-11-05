import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import threading
import urllib.request
import urllib.error
from ras_keenon_navigation.live import LiveView, ReadOnlyTransport
from ras_keenon_navigation.server import create_server
from ras_keenon_chat.navigation import NavigationClient
from ras_keenon_chat.provider import demo_complete


def map_message():
    return dict(op='publish',topic='/map',msg=dict(header=dict(frame_id='map'),info=dict(width=4,height=3,resolution=.1,map_load_time=dict(secs=123,nsecs=0),
        origin=dict(position=dict(x=-2,y=-1,z=0),orientation=dict(x=0,y=0,z=0,w=1))),data=[-1,0,0,100]*3))


def pose_message():
    return dict(op='publish',topic='/localization/robot_pose',msg=dict(header=dict(frame_id='map',stamp=dict(secs=1513431190,nsecs=0)),pose=dict(covariance=[0]*36,pose=dict(position=dict(x=0,y=0,z=0),orientation=dict(x=0,y=0,z=0,w=1)))))


class LiveTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.v=LiveView(self.tmp.name,autostart=False)
    def tearDown(self):self.v.close();self.tmp.cleanup()
    def test_waiting_has_no_fabricated_map_pose_or_simulation(self):
        v=self.v.snapshot();self.assertIsNone(v['map']);self.assertIsNone(v['pose']);self.assertFalse(v['simulated']);self.assertTrue(v['read_only'])
    def test_unverified_zero_pose_never_becomes_verified_position(self):
        self.v.ingest(map_message());self.v.ingest(pose_message());v=self.v.snapshot()
        self.assertEqual(v['raw_pose'],[0.,0.,0.]);self.assertIsNone(v['pose']);self.assertEqual(v['localization'],'unverified')
    def test_old_latched_map_remains_data_without_freshness_fabrication(self):
        self.v.ingest(map_message());self.v.connected=True
        self.v.map_at=self.v.clock()-100
        v=self.v.snapshot();self.assertGreaterEqual(v['map_age_s'],100);self.assertFalse(v['map_stale'])
        self.v.connected=False;self.assertTrue(self.v.snapshot()['map_stale'])
    def test_factory_mode_preserves_unknown_codes_and_receive_age(self):
        self.assertIsNone(self.v.snapshot()['factory_mode'])
        with patch.object(self.v,'clock',return_value=100):
            self.v.ingest(dict(op='publish',topic='/robot_state',msg=dict(mode=0)))
        with patch.object(self.v,'clock',return_value=125):
            mode=self.v.snapshot()['factory_mode'];self.assertEqual(mode['label'],'导航模式');self.assertEqual(mode['age_s'],25)
            self.v.ingest(dict(op='publish',topic='/robot_state',msg=dict(mode=7)))
            mode=self.v.snapshot()['factory_mode'];self.assertEqual(mode['code'],7);self.assertIn('待实测',mode['label'])
            with self.assertRaises(ValueError):self.v.ingest(dict(op='publish',topic='/robot_state',msg=dict(mode=True)))
    def test_transport_rejects_every_write_and_service(self):
        sent=[]
        class T:
            def send(self,m):sent.append(m)
        w=ReadOnlyTransport(T());w.send(dict(op='subscribe',topic='/map'))
        for op in ['advertise','publish','call_service','unadvertise']:
            with self.assertRaises(RuntimeError):w.send(dict(op=op,topic='/map'))
        self.assertEqual(len(sent),1)
    def test_export_is_real_grid_but_not_slam_session(self):
        self.v.ingest(map_message());result=self.v.save_map();report=json.loads((Path(result['directory'])/'exploration_report.json').read_text())
        self.assertFalse(report['simulated']);self.assertFalse(report['native_slam_state_saved']);self.assertIsNone(report['pose'])
    def test_http_and_llm_expose_readonly_truth_and_deny_motion(self):
        self.v.ingest(map_message());server=create_server(self.v,'127.0.0.1',0);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            client=NavigationClient('http://127.0.0.1:'+str(server.server_port));tools=client.schemas()
            self.assertEqual([t['name'] for t in tools],['navigation_get_status','navigation_save_map'])
            result=client.tool('navigation_get_status','{}','t');self.assertFalse(result['demo']);self.assertEqual(result['backend'],'native-readonly')
            with self.assertRaises(RuntimeError):client.post('/api/start',dict(mode='explore',request_id='x'))
            with self.assertRaises(RuntimeError):client.post('/api/manual-preempt',{})
            response,_=demo_complete([dict(role='user',content='开始探索')],tools)
            self.assertNotIn('tool_calls',response['choices'][0]['message'])
            self.assertIn('只读',response['choices'][0]['message']['content'])
        finally:server.shutdown();server.server_close()
    def test_monitor_reconnect_and_shutdown_send_only_subscriptions(self):
        sent=[]
        class Transport:
            def __init__(self):self.first=True
            def send(self,v):sent.append(v)
            def receive(self):
                if self.first:self.first=False;return map_message()
                time.sleep(.01)
            def close(self):pass
        other=LiveView(Path(self.tmp.name)/'monitor',transport_factory=lambda *_:Transport())
        try:
            end=time.monotonic()+1
            while other.grid is None and time.monotonic()<end:time.sleep(.01)
            self.assertIsNotNone(other.grid)
        finally:other.close()
        self.assertEqual(set(v['op'] for v in sent),{'subscribe','unsubscribe'})


if __name__=='__main__':unittest.main()
