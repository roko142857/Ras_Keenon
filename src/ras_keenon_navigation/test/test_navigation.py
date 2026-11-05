import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from ras_keenon_navigation.grid import Grid, Search, polygon, region_mask
from ras_keenon_navigation.manager import Manager
from ras_keenon_navigation.native import NativeAdapter
from ras_keenon_navigation.simulator import Simulator


class Clock:
    def __init__(self):self.t=100.
    def __call__(self):return self.t
    def advance(self,n=.2):self.t+=n


class GridTests(unittest.TestCase):
    def test_rotated_world_and_cell_round_trip(self):
        g=Grid(np.zeros((8,10)),.2,(1.,-2.,math.pi/2))
        for c,r in [(0,0),(4,6),(9,7)]:self.assertEqual(g.cell(*g.world(c,r)),(c,r))
        self.assertAlmostEqual(g.world(0,0)[0],.9)
    def test_invalid_maps_and_quaternion_are_not_admitted(self):
        for a in [np.array([[float('nan')]]),np.array([[101]]),np.array([[.5]]),np.zeros((0,2))]:
            with self.subTest(a=str(a)),self.assertRaises(ValueError):Grid(a,.1)
        msg=dict(info=dict(width=1,height=1,resolution=.1,origin=dict(position=dict(x=0,y=0),orientation=dict(x=0,y=0,z=0,w=0))),data=[0])
        with self.assertRaises(ValueError):Grid.from_ros(msg,'m')
    def test_polygon_rejects_self_intersection_and_empty_area(self):
        for p in [[[0,0],[2,2],[0,2],[2,0]],[[0,0],[1,1],[2,2]]]:
            with self.assertRaises(ValueError):polygon(p)
    def test_region_rotation_and_exclusion_mask(self):
        g=Grid(np.zeros((20,20)),.1)
        mask=region_mask(g,[[.2,.2],[1.8,.2],[1.8,1.8],[.2,1.8]],[[[.8,.8],[1.2,.8],[1.2,1.2],[.8,1.2]]])
        self.assertTrue(mask[5,5]);self.assertFalse(mask[10,10]);self.assertFalse(mask[0,0])
    def test_frontier_stations_and_paths_remain_known_and_reachable(self):
        s=Simulator();v=s.snapshot();search=Search(v['grid'],v['pose'],.26);goals=search.frontiers()
        self.assertGreater(len(goals),0)
        for goal in goals:
            for x,y in search.path(goal['x'],goal['y']):
                c,r=v['grid'].cell(x,y);self.assertTrue(search.safe[r,c]);self.assertEqual(v['grid'].data[r,c],0)
    def test_footprint_cannot_cross_narrow_corridor(self):
        data=np.full((30,30),100);data[2:28,2:12]=0;data[2:28,18:28]=0;data[14:16,11:19]=0
        search=Search(Grid(data,.1),(0.65,.65,0),.22)
        with self.assertRaises(ValueError):search.path(2.25,1.55)
    def test_unknown_target_is_not_replaced_with_free(self):
        v=Simulator().snapshot();g=v['grid'];search=Search(g,v['pose'],.26)
        with self.assertRaises(ValueError):search.path(7.,5.)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.clock=Clock();self.mock=Simulator(speed=8,clock=self.clock)
        self.manager=Manager(self.temp.name,self.mock,autostep=False)
        self.patcher=patch('ras_keenon_navigation.manager.time.monotonic',self.clock);self.patcher.start()
    def tearDown(self):self.manager.close();self.patcher.stop();self.temp.cleanup()
    def step(self,n=1):
        for _ in range(n):self.clock.advance();self.manager.step()
    def start(self,**kwargs):return self.manager.start(dict(request_id='test',mode='explore',**kwargs))
    def test_pause_releases_goal_and_pose_stays_still_until_resume(self):
        self.start();self.step(4);self.manager.control('pause');pose=list(self.mock.pose);self.step(10)
        self.assertEqual(self.mock.pose,pose);self.assertIsNone(self.mock.goal)
        self.manager.control('resume');self.step(6);self.assertNotEqual(self.mock.pose,pose)
    def test_same_request_is_idempotent_and_conflicting_payload_rejected(self):
        a=self.start();b=self.start();self.assertEqual(a['task']['id'],b['task']['id']);self.assertTrue(b['deduplicated'])
        with self.assertRaises(ValueError):self.start(duration_s=20)
    def test_restart_interrupts_without_mapping_or_replay_and_keeps_request_id(self):
        a=self.start();self.step();self.manager.close()
        backend=Simulator(clock=self.clock);other=Manager(self.temp.name,backend,autostep=False)
        try:
            self.assertEqual(other.task['state'],'interrupted');self.assertFalse(backend.mapping);self.assertIsNone(backend.goal)
            b=other.start(dict(request_id='test',mode='explore'));self.assertTrue(b['deduplicated']);self.assertEqual(b['task']['id'],a['task']['id']);self.assertFalse(backend.mapping)
        finally:other.close()
    def test_exploration_grows_map_and_ends_with_explicit_scope_and_export(self):
        before=self.manager.snapshot()['map']['known_cells'];self.start()
        for _ in range(2000):
            self.step()
            if self.manager.task['state'] not in ('exploring','navigating'):break
        task=self.manager.task
        self.assertEqual(task['state'],'completed',task);self.assertGreater(task['targets_completed'],2)
        self.assertGreater(self.manager.snapshot()['map']['known_cells'],before+1500)
        self.assertIn('不代表房间全覆盖',task['reason']);self.assertTrue(task['saved_map'])
    def test_export_reload_hash_and_no_native_slam_claim(self):
        self.start();self.step(8);self.manager.control('pause');self.manager.control('finish')
        identity=self.manager.task['saved_map'];directory=Path(self.temp.name)/'maps'/identity
        saved=json.loads((directory/'map.json').read_text());report=json.loads((directory/'exploration_report.json').read_text())
        self.assertFalse(report['native_slam_state_saved']);self.assertFalse(report['native_resume_available'])
        self.assertTrue((directory/'map.pgm').read_bytes().startswith(b'P5\n80 60\n255\n'))
        self.assertTrue(self.manager.load_map(identity)['loaded']);self.assertEqual(self.manager.snapshot()['map']['version'],saved['version'])
        (directory/'map.json').write_text('{}')
        with self.assertRaises(ValueError):self.manager.load_map(identity)
    def test_task_duration_is_partial_not_completed(self):
        self.start(duration_s=1);self.step(10);self.assertEqual(self.manager.task['state'],'partial');self.assertIsNone(self.mock.goal)
    def test_saved_waypoint_route_dwell_and_stale_map_binding(self):
        w=self.manager.waypoint(dict(name='桌边',x=1.25,y=1.75,yaw=0))
        self.manager.save_route('短路线',[dict(waypoint_id=w['id'],dwell_s=2)])
        self.manager.start(dict(mode='route',request_id='route',steps=self.manager.routes['短路线']))
        self.step(5);self.assertEqual(self.manager.task['targets_completed'],1)
        self.assertNotEqual(self.manager.task['state'],'completed');self.step(15);self.assertEqual(self.manager.task['state'],'completed')
        self.manager.start(dict(mode='explore',request_id='new-map'));self.manager.control('stop')
        with self.assertRaises(ValueError):self.manager.validate_steps([dict(waypoint_id=w['id'])])
    def test_region_cannot_change_during_task_and_exclude_current_body(self):
        with self.assertRaises(ValueError):self.manager.configure_region([[5,4],[7,4],[7,5],[5,5]])
        self.start()
        with self.assertRaises(RuntimeError):self.manager.configure_region(None,[])
    def test_duplicate_manager_cannot_touch_an_active_session(self):
        self.start()
        with self.assertRaises(RuntimeError):Manager(self.temp.name,autostep=False)
        self.assertEqual(self.manager.task['state'],'exploring')

    def test_native_backend_cannot_be_activated_in_local_manager(self):
        class Backend:kind='native'
        with self.assertRaises(ValueError):Manager(self.temp.name,Backend(),autostep=False)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.sent=[]
        class Transport:
            def send(_,v):self.sent.append(copy.deepcopy(v))
        self.clock=Clock();self.adapter=NativeAdapter(Transport(),{},self.clock)
    def frame(self):
        g=Simulator().snapshot()['grid'];msg=dict(info=dict(width=g.width,height=g.height,resolution=g.resolution,origin=dict(position=dict(x=0,y=0,z=0),orientation=dict(x=0,y=0,z=0,w=1))),data=g.data.reshape(-1).tolist())
        self.adapter.ingest(dict(op='publish',topic='/map',msg=msg))
        pose=dict(header=dict(frame_id='map',stamp=dict(secs=1513431190,nsecs=123)),pose=dict(pose=dict(position=dict(x=1.25,y=1.25),orientation=dict(x=0,y=0,z=0,w=1))))
        self.adapter.ingest(dict(op='publish',topic='/localization/robot_pose',msg=pose));return pose
    def verified_fake_profile(self):
        # Fixture values are arbitrary mock values, NOT factory enum recommendations.
        self.adapter.profile=dict(protocol_verified=True,localization_semantics_verified=True,mapping_navigation_concurrent_verified=True,
            goal_fields=dict(goal_status=7,ugly_goal_status=8,disable_orientation=False,updateVirtualWall=False),success_result_codes=[19],
            operations=dict(start=dict(service='/mock/start',type='mock/Start',args={'option':'mock-start'},response_code_field='code',success_codes=[23])))
    def ready(self):
        self.verified_fake_profile();self.adapter.start_mapping('session');self.frame()
        self.adapter.ingest(dict(op='service_response',id='session-mapping-start',result=True,values=dict(code=23)))
        self.adapter.confirm_mapping_session('session')
    def test_unverified_contract_never_sends_motion_or_mapping(self):
        self.frame()
        with self.assertRaises(RuntimeError):self.adapter.start_mapping('session')
        with self.assertRaises(RuntimeError):self.adapter.send_goal('g',[1,1,0])
        self.assertEqual(self.sent,[])
    def test_mapping_transport_success_is_not_physical_session_confirmation(self):
        self.verified_fake_profile();ack=self.adapter.start_mapping('session');self.assertFalse(ack['physical_completion_verified'])
        self.frame();self.adapter.ingest(dict(op='service_response',id='session-mapping-start',result=True,values=dict(code=999)))
        with self.assertRaises(RuntimeError):self.adapter.confirm_mapping_session('session')
        self.assertFalse(self.adapter.mapping_confirmed)
    def test_source_stamp_goal_id_and_custom_fields_preserved(self):
        self.ready();self.adapter.send_goal('goal-1',[1.5,1.5,.3]);v=self.sent[-1]
        self.assertEqual(v['topic'],'/move_base/goal');self.assertEqual(v['msg']['goal']['goal_status'],7)
        self.assertEqual(v['msg']['goal_id']['stamp']['secs'],1513431190)
        self.assertEqual(v['msg']['goal_id']['id'],'goal-1')
    def test_late_result_cannot_finish_new_goal_and_cancel_is_unverified(self):
        self.ready();self.adapter.send_goal('new',[1.5,1.5,0])
        self.adapter.ingest(dict(op='publish',topic='/move_base/result',msg=dict(status=dict(goal_id=dict(id='old'),status=3),result=dict(ret_status=19))))
        self.assertEqual(self.adapter.result('new'),'running');ack=self.adapter.cancel();self.assertFalse(ack['verified'])
        self.assertEqual(self.sent[-1]['msg']['id'],'new')
    def test_matching_result_requires_verified_vendor_success_code(self):
        self.ready();self.adapter.send_goal('a',[1,1,0])
        self.adapter.ingest(dict(op='publish',topic='/move_base/result',msg=dict(status=dict(goal_id=dict(id='a'),status=3),result=dict(ret_status=99))))
        self.assertEqual(self.adapter.result('a'),'failed')
    def test_stale_pose_and_wrong_frame_do_not_become_valid(self):
        self.ready();self.clock.advance(4)
        with self.assertRaises(RuntimeError):self.adapter.send_goal('g',[1,1,0])
        pose=self.frame();pose['header']['frame_id']='odom'
        with self.assertRaises(ValueError):self.adapter.ingest(dict(op='publish',topic='/localization/robot_pose',msg=pose))
        with self.assertRaises(RuntimeError):self.adapter.snapshot()


if __name__=='__main__':unittest.main()
