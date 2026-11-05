import base64
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from ras_keenon_navigation.mapping import Mapping, metadata
from ras_keenon_navigation.mapping_transport import MockFactory, RpcUncertain, allowed, image_bytes
from ras_keenon_navigation.mapping_server import create_server
from ras_keenon_commands.mapping_proxy import MappingProxy


def wait(check, timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if check():return
        time.sleep(.015)
    raise AssertionError('timeout')


class RecordingFactory(MockFactory):
    def __init__(self, root):super().__init__(root);self.calls=[];self.fault=None;self.after_insert=False;self.old_image=False;self.fail_return=False
    def rpc(self, service, args, timeout=15):
        self.calls.append((service,copy.deepcopy(args)))
        if self.fault=='insert' and args.get('option')=='insert':
            self.fault=None
            if self.after_insert:super().rpc(service,args)
            raise RpcUncertain('lost reply')
        if self.fail_return and service=='/web_command' and args['mainCmd']==0:raise RpcUncertain('lost mode reply')
        return super().rpc(service,args,timeout)
    def image(self, mapping=False):
        value=super().image(mapping)
        if value and self.old_image and mapping:value['wall']=0
        return value


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.f=RecordingFactory(self.root);self.m=Mapping(self.root,factory=self.f);self.counter=0
    def tearDown(self):self.m.close();self.tmp.cleanup()
    def submit(self, action, **value):
        self.counter+=1;value.setdefault('request_id','r'+str(self.counter))
        if action=='start':value.update(name='实验室',floor=2,buildingInfo='LAB',floorInfo='二层')
        else:value.setdefault('session_id',self.m.session['id'])
        op=self.m.submit(action,value);wait(lambda:self.m.busy is None)
        return next(x for x in self.m.snapshot()['operations'] if x['id']==op['id'])
    def start(self):
        op=self.submit('start');self.assertEqual(op['state'],'succeeded');wait(lambda:self.m.snapshot()['session']['phase']=='mapping')
    def test_startup_shutdown_are_readonly_and_no_sdk_or_motion(self):
        time.sleep(.35);self.assertFalse(any(s in ('/web_command','/switch_dest_floor_map') or a.get('option')=='insert' for s,a in self.f.calls));self.assertIsNone(self.m.snapshot()['session'])
    def test_save_roundtrip_preserves_factory_png_and_metadata(self):
        self.start();self.assertEqual(self.m.session['accepted_mode'],2)
        op=self.submit('finish');self.assertEqual(op['state'],'succeeded',op)
        record=self.f.maps[0];raw=base64.b64decode(record['image']['data'])
        self.assertEqual(raw,(self.root/'sessions'/self.m.session['id']/'frozen.png').read_bytes())
        self.assertEqual(self.m.snapshot()['session']['phase'],'saved');self.assertEqual(self.f.mode,0);self.assertEqual(self.f.floor,2)
        self.assertTrue(op['result']['native_database_saved']);self.assertFalse(op['result']['native_slam_resume_available'])
        self.assertTrue((self.root/'sessions'/self.m.session['id']/'before_maps.json').exists())
    def test_lost_insert_reply_retries_readback_without_duplicate_insert(self):
        self.start();self.f.fault='insert';self.f.after_insert=True
        self.assertEqual(self.submit('finish')['state'],'uncertain');self.assertEqual(self.submit('finish')['state'],'succeeded')
        self.assertEqual(len([a for s,a in self.f.calls if a.get('option')=='insert']),1)
    def test_unknown_insert_does_not_replay_when_query_is_empty(self):
        self.start();self.f.fault='insert'
        self.assertEqual(self.submit('finish')['state'],'uncertain');self.assertEqual(self.submit('finish')['state'],'uncertain')
        self.assertEqual(len([a for s,a in self.f.calls if a.get('option')=='insert']),1)
    def test_save_readback_and_mode_return_are_separate(self):
        self.start();self.f.fail_return=True
        self.assertEqual(self.submit('finish')['state'],'uncertain');self.assertEqual(self.m.session['insert_status'],'confirmed')
        self.f.fail_return=False;self.assertEqual(self.submit('finish')['state'],'succeeded')
        self.assertEqual(len([a for s,a in self.f.calls if a.get('option')=='insert']),1)
    def test_old_map_cannot_be_saved_as_this_session(self):
        self.f.old_image=True;self.assertEqual(self.submit('start')['state'],'succeeded')
        self.assertEqual(self.submit('finish')['state'],'failed');self.assertFalse(self.f.maps);self.assertIsNone(self.m.snapshot()['image'])
    def test_cancel_restores_floor_without_insert_or_delete(self):
        self.start();self.assertEqual(self.submit('cancel')['state'],'succeeded');self.assertEqual(self.f.floor,1);self.assertEqual(self.f.mode,0)
        self.assertFalse(any(a.get('option') in ('insert','delete') for s,a in self.f.calls))
    def test_mapping_floor_zero_does_not_block_return_to_saved_floor(self):
        self.start();self.f.floor=0
        self.assertEqual(self.submit('finish')['state'],'succeeded');self.assertEqual(self.f.floor,2)
    def test_cancel_sends_navigation_even_if_cached_mode_is_zero(self):
        self.start();self.f.mode=0
        before=len(self.f.calls)
        self.assertEqual(self.submit('cancel')['state'],'succeeded')
        self.assertIn(('/web_command',{'mainCmd':0,'subCmd':0}),self.f.calls[before:])
    def test_existing_floor_is_not_overwritten(self):
        self.start();self.f.maps.append(dict(floor=2,type='map',name='other',md5='other'))
        self.assertEqual(self.submit('finish')['state'],'failed');self.assertEqual(len(self.f.maps),1)
        self.assertFalse(any(a.get('option')=='insert' for s,a in self.f.calls))
    def test_restart_does_not_replay_and_retains_pending_save(self):
        self.start();self.f.fault='insert';self.f.after_insert=True;self.submit('finish');self.m.close()
        self.f=RecordingFactory(self.root);self.m=Mapping(self.root,factory=self.f);time.sleep(.35)
        self.assertEqual(self.m.session['phase'],'recovery_required')
        self.assertFalse(any(s=='/web_command' or a.get('option')=='insert' for s,a in self.f.calls))
        self.assertEqual(self.submit('finish')['state'],'succeeded');self.assertFalse(any(a.get('option')=='insert' for s,a in self.f.calls))
    def test_idempotent_request_and_reused_id_payload_rejected(self):
        value=dict(name='实验室',floor=2,buildingInfo='LAB',request_id='same')
        first=self.m.submit('start',value);again=self.m.submit('start',value);self.assertEqual(first['id'],again['id'])
        with self.assertRaises(ValueError):self.m.submit('start',dict(value,name='different'))
        wait(lambda:self.m.busy is None);self.assertEqual(len([a for s,a in self.f.calls if s=='/web_command']),1)
    def test_export_after_save_is_explicit_png_snapshot(self):
        self.start();self.submit('finish');op=self.submit('export');self.assertEqual(op['state'],'succeeded',op)
        report=json.loads((self.root/'exports'/op['result']['export_id']/'report.json').read_text())
        self.assertEqual(report['export_type'],'factory_png_and_metadata');self.assertFalse(report['native_slam_resume_available']);self.assertNotIn('resolution',report)
    def test_http_proxy_backend_check_and_cross_origin(self):
        server=create_server(self.m,port=0);threading.Thread(target=server.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(server.server_port)
        try:
            code,body,kind=MappingProxy(url,'mock').request('/mapping/state');self.assertEqual(code,200);self.assertTrue(json.loads(body)['simulated'])
            with self.assertRaises(ValueError):MappingProxy(url,'hardware').request('/mapping/start',{})
            with self.assertRaises(ValueError):MappingProxy('http://192.168.3.20:18781','mock')
            req=urllib.request.Request(url+'/api/start',b'{}',{'Content-Type':'application/json','Origin':'http://elsewhere'})
            with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code,403)
        finally:server.shutdown();server.server_close()
    def test_stalled_image_requires_explicit_confirmation_before_insert(self):
        self.start()
        original=self.f.image
        def stale(mapping=False):
            image=original(mapping)
            if mapping and image:image['wall']=time.time()-260
            return image
        self.f.image=stale
        self.m.session['cutoff_wall']=time.time()-400
        self.assertEqual(self.submit('finish')['state'],'failed')
        self.assertFalse(self.f.maps)
        self.assertTrue(self.m.session['stale_save_confirmation_required'])
        self.assertEqual(self.submit('finish',allow_stale=True)['state'],'succeeded')
        self.assertTrue(self.m.session['frozen_source']['stale_confirmed'])
    def test_mode_feedback_is_not_command_number(self):
        self.start();self.assertEqual(self.f.mode,2);self.assertEqual(self.m.session['accepted_mode'],2)
    def test_invalid_png_metadata_and_writes_are_rejected(self):
        self.start();image=self.f.image(True);self.assertTrue(image_bytes(image['message'])[0]);bad=bytearray(image['raw']);bad[-6]^=1
        with self.assertRaises(ValueError):image_bytes(dict(data=base64.b64encode(bad).decode()))
        for service,args in [('/cmd_vel',{}),('/database/config_map',dict(option='delete',map=[{}])),('/web_command',dict(mainCmd=2,subCmd=0))]:self.assertFalse(allowed(service,args))
        for floor in [0,200,True,32768,float('nan')]:
            with self.assertRaises(ValueError):metadata(dict(name='a',floor=floor,buildingInfo='LAB'))


if __name__=='__main__':unittest.main()
