"""Finite plans, cancellation and public HTTP/tool interfaces; no SDK or robot."""
import copy
import json
import math
import threading
import time
import unittest

from ras_keenon_demos.api import create_server
from ras_keenon_demos.catalog import normalize,tool_schemas,decode_tool_call
from ras_keenon_demos.plans import build_plan
from ras_keenon_demos.gestures import large_wave_keyframes
from ras_keenon_demos.runner import ActionManager
from ras_keenon_demos.transport import ActionClient,Cancelled

READINGS=dict(backend='mock',body=dict(yaw_deg=-4.,pitch_deg=2.,right=[.1,.2,.3,.4,.5,.6,.7],left=[-.1]*7))


def wait(check, timeout=3.):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=check()
        if value:return value
        time.sleep(.005)
    raise AssertionError('condition timed out')


class FakeController:
    def __init__(self, block=False, fail=False):
        self.commands=[];self.stops=0;self.timeouts=[]
        self.block,self.fail=block,fail
        self.entered=threading.Event()
    def readings(self):return copy.deepcopy(READINGS)
    def stop(self):self.stops+=1;return {'ok':True}
    def submit(self,command):self.commands.append(command);return str(len(self.commands))
    def wait(self,identity,cancel,timeout=None):
        self.timeouts.append(timeout)
        self.entered.set()
        if self.fail:raise RuntimeError('test SDK failure')
        while self.block:
            if cancel.wait(.005):raise Cancelled('cancelled')
        return dict(done=True,success=True)


class PlanTests(unittest.TestCase):
    def test_real_degree_values_fail_but_converted_pose_plans_wave_and_reset(self):
        readings=dict(backend='hardware',body=dict(yaw_deg=7.67,pitch_deg=-5.93,
            right=[1.8759,85.7208,176.8153,-.5713,89.9437,-2.8798,-.3571],
            left=[-3.0363,-83.264,-11.4931,2.8592,100.8051,2.5076,16.8599]))
        for action in ('shake_hand','wave_large','reset'):
            with self.assertRaises(ValueError):build_plan(action,{},readings)
        for side in ('right','left'):readings['body'][side]=list(map(math.radians,readings['body'][side]))
        for action in ('shake_hand','wave_large','reset'):
            plan=build_plan(action,{},readings)
            self.assertTrue(plan.steps)
            self.assertAlmostEqual(plan.start['right'][2],math.radians(176.8153))
        reset=build_plan('reset',{},readings)
        self.assertEqual(reset.steps[-1].command,'s 0.03 -')

    def test_wave_uses_current_arm_only_elbow_and_returns(self):
        p=build_plan('shake_hand',{},READINGS)
        previous=READINGS['body']['right'][3]
        for step in p.steps:
            args=step.command.split();self.assertEqual(args[:2],['arm','right'])
            q=list(map(float,args[2:9]))
            for i in (0,1,2,4,5,6):self.assertAlmostEqual(q[i],READINGS['body']['right'][i])
            self.assertLessEqual(abs(q[3]-previous),math.radians(15)+1e-9);previous=q[3]
            self.assertEqual(float(args[9]),.1)
        self.assertAlmostEqual(previous,READINGS['body']['right'][3])
        self.assertGreater(len(p.steps),5)

    def test_wave_subdivides_wide_oscillations_and_uses_left_origin(self):
        p=build_plan('shake_hand',{'side':'left','amplitude_deg':15},READINGS)
        values=[READINGS['body']['left'][3]]+[float(s.command.split()[5]) for s in p.steps]
        self.assertTrue(all(abs(a-b)<=math.radians(15)+1e-9 for a,b in zip(values,values[1:])))
        self.assertAlmostEqual(values[-1],-.1)

    def test_head_preserves_pitch_and_restores_head_gestures(self):
        p=build_plan('turn_head',{'yaw_deg':10},READINGS)
        self.assertEqual(p.steps[0].command,'head 10 2 0.3')
        for action in ('nod','shake_head'):
            p=build_plan(action,{},READINGS)
            self.assertEqual(p.steps[-1].command,'head -4 2 0.3')

    def test_head_clamps_at_documented_angle_range(self):
        r=copy.deepcopy(READINGS);r['body']['pitch_deg']=19
        p=build_plan('nod',{},r)
        self.assertTrue(all(abs(float(s.command.split()[2]))<=20 for s in p.steps))

    def test_waist_and_base_units(self):
        self.assertEqual(build_plan('squat').steps[0].command,'s 0.03 -')
        self.assertEqual(build_plan('stand').steps[0].command,'stand')
        self.assertEqual(build_plan('rotate',{'direction':'right'}).steps[0].command,'base 0 -0.02 1')
        self.assertEqual(build_plan('move_forward').steps[0].command,'base 0.02 0 1')

    def test_validate_every_waypoint_before_any_dispatch(self):
        r=copy.deepcopy(READINGS);r['body']['right'][3]=-3.15
        with self.assertRaises(ValueError):build_plan('shake_hand',{},r)
        with self.assertRaises(ValueError):build_plan('shake_hand',{},dict(body={}))

    def test_invalid_arguments_and_tools(self):
        for action,params in [('shake_hand',{'cycles':True}),('shake_hand',{'lift_deg':float('nan')}),
                              ('shake_hand',{'shell':'ls'}),('shake_hand',{'lift_deg':60,'amplitude_deg':5}),
                              ('turn_head',{'yaw_deg':'10'}),('stand',[]),('not_an_action',{})]:
            with self.assertRaises(ValueError):normalize(action,params)
        with self.assertRaises(ValueError):decode_tool_call({'name':'exec','arguments':{}})
        self.assertEqual(len(tool_schemas()),12)
        json.dumps(tool_schemas(),allow_nan=False)

    def test_reset_zeros_both_arms_head_and_defaults_to_crouch(self):
        plan = build_plan('reset', {}, READINGS)
        self.assertEqual(plan.steps[0].command, 'stop')
        self.assertEqual(plan.steps[1].command, 'head 0 0 0.3')
        self.assertEqual(plan.steps[-1].command, 's 0.03 -')
        for side in ('right','left'):
            previous = READINGS['body'][side]
            commands = [s.command.split() for s in plan.steps if s.command.startswith('arm '+side+' ')]
            for c in commands:
                q = list(map(float,c[2:9]))
                self.assertTrue(all(abs(a-b)<=math.radians(15)+1e-9 for a,b in zip(previous,q)))
                previous = q
            self.assertEqual(previous,[0.]*7)
        self.assertEqual(build_plan('reset',{'height_m':.1,'direction':'+'},READINGS).steps[-1].command,'s 0.1 +')
        bad = copy.deepcopy(READINGS);bad['body']['left']=[]
        with self.assertRaises(ValueError):build_plan('reset',{},bad)
        action,params,_=decode_tool_call({'name':'robot_reset','arguments':'{}'})
        self.assertEqual((action,params['height_m']),('reset',.03))

    def test_base_speed_has_no_old_application_maximum(self):
        self.assertEqual(build_plan('move_forward',{'speed_mps':.4}).steps[0].command,'base 0.4 0 1')
        self.assertEqual(build_plan('rotate',{'speed_radps':.6}).steps[0].command,'base 0 0.6 1')
        schema=next(t for t in tool_schemas() if t['name']=='robot_move_forward')
        self.assertNotIn('maximum',schema['parameters']['properties']['speed_mps'])
        for value in (-.1,float('inf'),float('nan')):
            with self.assertRaises(ValueError):normalize('move_forward',{'speed_mps':value})

    def test_renamed_action_and_large_wave_tools(self):
        names={t['name'] for t in tool_schemas()}
        self.assertTrue({'robot_shake_hand','robot_wave_large'}<=names)
        self.assertNotIn('robot_wave',names)
        with self.assertRaises(ValueError):normalize('wave',{})
        action,params,_=decode_tool_call({'name':'robot_wave_large','arguments':'{"side":"left"}'})
        self.assertEqual((action,params['side']),('wave_large','left'))

    def test_large_wave_moves_shoulders_elbow_wrist_and_restores_origin(self):
        p=build_plan('wave_large',{'pause_s':0},READINGS)
        previous=list(READINGS['body']['right']);moved=set()
        for s in p.steps:
            fields=s.command.split();self.assertEqual(fields[:2],['arm','right'])
            q=list(map(float,fields[2:9]))
            self.assertTrue(all(abs(a-b)<=math.radians(15)+1e-9 for a,b in zip(previous,q)))
            moved.update(i for i,(a,b) in enumerate(zip(previous,q)) if abs(a-b)>1e-6)
            previous=q
        self.assertTrue({0,1,3,5}<=moved)
        for a,b in zip(previous,READINGS['body']['right']):self.assertAlmostEqual(a,b)

    def test_large_wave_mirrors_sides_and_sweeps_above_raised_pose(self):
        right=large_wave_keyframes(normalize('wave_large'))
        left=large_wave_keyframes(normalize('wave_large',{'side':'left'}))
        for (name,r,_),(_,l,_) in zip(right,left):
            for a,b in zip(r,l):self.assertAlmostEqual(a,-b)
        raised=next(q for name,q,_ in right if name=='raise')
        self.assertAlmostEqual(raised[1],math.radians(-75))
        swings=[q for name,q,_ in right if name=='swing']
        self.assertEqual(len(swings),4)
        self.assertTrue(all(q[1]==raised[1] for q in swings))
        self.assertNotEqual(swings[0][0],swings[1][0])
        self.assertNotEqual(swings[0][3],swings[1][3])
        self.assertNotEqual(swings[0][5],swings[1][5])

    def test_large_wave_optional_hold_and_missing_pose(self):
        parameters={'side':'left','return_to_start':False,'pause_s':0}
        plan=build_plan('wave_large',parameters,READINGS)
        q=list(map(float,plan.steps[-1].command.split()[2:9]))
        expected=next(q for name,q,_ in large_wave_keyframes(normalize('wave_large',parameters)) if name=='raise')
        for a,b in zip(q,expected):self.assertAlmostEqual(a,b)
        with self.assertRaises(ValueError):build_plan('wave_large',{},dict(body={}))
        for params in ({'amplitude_deg':float('nan')},{'shoulder_lift_deg':120},{'wrist_amplitude_deg':90}):
            with self.assertRaises(ValueError):normalize('wave_large',params)


class ManagerTests(unittest.TestCase):
    def test_large_wave_cancel_discards_raise_and_return_steps(self):
        controller=FakeController(block=True);m=ActionManager(controller)
        try:
            job=m.submit('wave_large',{'pause_s':0})
            self.assertTrue(controller.entered.wait(2))
            m.cancel(job['id']);wait(lambda:m.status(job['id'])['state']=='cancelled')
            self.assertEqual(len(controller.commands),1)
            self.assertGreater(m.status(job['id'])['steps_total'],1)
        finally:m.close()
    def test_long_base_duration_gets_matching_result_deadline(self):
        controller=FakeController();m=ActionManager(controller)
        try:
            j=m.submit('move_forward',{'duration_s':100})
            wait(lambda:m.status(j['id'])['state']=='succeeded')
            self.assertGreaterEqual(controller.timeouts[0],105)
        finally:m.close()

    def test_closing_unused_service_does_not_stop_manual_controller(self):
        controller=FakeController();m=ActionManager(controller);m.close()
        self.assertEqual(controller.stops,0)

    def test_same_tool_id_does_not_replay_and_different_arguments_fail(self):
        controller=FakeController();m=ActionManager(controller)
        try:
            j=m.submit('stand',request_id='call-1')
            wait(lambda:m.status(j['id'])['state']=='succeeded')
            self.assertEqual(m.submit('stand',request_id='call-1')['id'],j['id'])
            self.assertEqual(controller.commands,['stand'])
            with self.assertRaises(ValueError):m.submit('squat',request_id='call-1')
        finally:m.close()

    def test_cancel_discards_remaining_wave_without_restore(self):
        controller=FakeController(block=True);m=ActionManager(controller)
        try:
            j=m.submit('shake_hand')
            self.assertTrue(controller.entered.wait(2))
            m.cancel(j['id']);wait(lambda:m.status(j['id'])['state']=='cancelled')
            self.assertEqual(len(controller.commands),1)
            self.assertGreater(controller.stops,0)
        finally:m.close()

    def test_latest_action_replaces_active_sequence(self):
        controller=FakeController(block=True);m=ActionManager(controller)
        try:
            old=m.submit('shake_hand');self.assertTrue(controller.entered.wait(2))
            new=m.submit('stand');controller.block=False
            wait(lambda:m.status(new['id'])['state']=='succeeded')
            self.assertEqual(m.status(old['id'])['state'],'cancelled')
            self.assertEqual(len(controller.commands),2)
            self.assertEqual(controller.commands[-1],'stand')
        finally:m.close()

    def test_failure_does_not_send_later_waypoints(self):
        controller=FakeController(fail=True);m=ActionManager(controller)
        try:
            j=m.submit('shake_hand');wait(lambda:m.status(j['id'])['state']=='failed')
            self.assertEqual(len(controller.commands),1)
            self.assertIn('SDK failure',m.status(j['id'])['error'])
            self.assertGreater(controller.stops,0)
        finally:m.close()

    def test_invalid_request_does_not_cancel_active_action(self):
        controller=FakeController(block=True);m=ActionManager(controller)
        try:
            j=m.submit('shake_hand');self.assertTrue(controller.entered.wait(2))
            with self.assertRaises(ValueError):m.submit('shake_hand',{'unknown':1})
            self.assertFalse(m.jobs[j['id']].cancel.is_set())
        finally:m.close()


class APITests(unittest.TestCase):
    def setUp(self):
        self.controller=FakeController();self.manager=ActionManager(self.controller)
        self.server=create_server(self.manager,'127.0.0.1',0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=ActionClient('http://127.0.0.1:'+str(self.server.server_port))
    def tearDown(self):
        self.manager.close();self.server.shutdown();self.server.server_close();self.thread.join(2)
    def test_tool_call_json_string_dedup_and_result(self):
        job=self.client.call_tool('robot_turn_head','{"yaw_deg":10,"settle_s":0}','tool-1')
        self.assertEqual(self.client.wait(job['id'])['state'],'succeeded')
        repeated=self.client.call_tool('robot_turn_head',{'yaw_deg':10,'settle_s':0},'tool-1')
        self.assertEqual(repeated['id'],job['id']);self.assertEqual(len(self.controller.commands),1)
    def test_large_wave_tool_dispatches_seven_axis_commands(self):
        job=self.client.call_tool('robot_wave_large',{'side':'left','cycles':1,'pause_s':0},'large-1')
        result=self.client.wait(job['id'])
        self.assertEqual(result['state'],'succeeded')
        self.assertTrue(all(c.startswith('arm left ') for c in self.controller.commands))
        self.assertGreater(len(self.controller.commands),10)
    def test_plan_never_dispatches_and_schemas_match_catalog(self):
        plan=self.client.plan('shake_hand')
        self.assertEqual(plan['action'],'shake_hand');self.assertEqual(self.controller.commands,[])
        self.assertEqual(len(self.client.tools()),len(self.client.catalog()))
    def test_tool_stop_cancels_entire_sequence(self):
        self.controller.block=True
        job=self.client.submit('shake_hand');self.assertTrue(self.controller.entered.wait(2))
        self.assertTrue(self.client.call_tool('robot_stop',{})['stopped'])
        self.assertEqual(self.client.wait(job['id'])['state'],'cancelled')
    def test_unknown_action_and_invalid_types_rejected(self):
        with self.assertRaises(RuntimeError):self.client.submit('shell',{'command':'ls'})
        with self.assertRaises(RuntimeError):self.client.call_tool('robot_shake_hand','[]')
        with self.assertRaises(RuntimeError):self.client.submit('stand',[])
        self.assertEqual(self.controller.commands,[])


if __name__=='__main__':unittest.main()
