import base64
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from ras_keenon_navigation.mapping import Mapping
from ras_keenon_navigation.mapping_transport import MockFactory, RpcUncertain
from ras_keenon_navigation.mapping_server import create_server


def wait(manager, op):
    end=time.monotonic()+5
    while manager.busy and time.monotonic()<end:time.sleep(.01)
    assert not manager.busy
    return next(o for o in manager.operations if o['id']==op['id'])


class Factory(MockFactory):
    def __init__(self,root):super().__init__(root);self.calls=[];self.lose_update=False;self.commit=True
    def rpc(self,service,args,timeout=15):
        self.calls.append((service,copy.deepcopy(args)))
        if args.get('option')=='update' and self.lose_update:
            self.lose_update=False
            if self.commit:super().rpc(service,args)
            raise RpcUncertain('update reply lost')
        return super().rpc(service,args,timeout)


class MapManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.f=Factory(self.root);img=self.f.image()
        self.record=dict(floor=2,type='map',md5=img['version'],name='旧图',buildingInfo='LAB',floorInfo='2',image=img['message'])
        self.f.maps=[copy.deepcopy(self.record)];self.m=Mapping(self.root,factory=self.f);self.i=0
    def tearDown(self):self.m.close();self.tmp.cleanup()
    def target(self):return {k:self.record[k] for k in ('floor','type','md5')}
    def submit(self,action,**v):
        self.i+=1;v['request_id']='m'+str(self.i)
        if action in ['finish','cancel']:v['session_id']=self.m.session['id']
        return wait(self.m,self.m.submit(action,v))
    def start(self,replace=True):
        return self.submit('start',floor=2,name='新图',buildingInfo='LAB',floorInfo='二层',**({'replace_target':self.target()} if replace else {}))
    def test_preview_is_readonly_and_exports_exact_factory_record(self):
        o=self.submit('map_preview',target=self.target());self.assertEqual(o['state'],'succeeded',o)
        directory=self.root/'exports'/o['result']['export_id']
        self.assertEqual((directory/'map.png').read_bytes(),base64.b64decode(self.record['image']['data']))
        self.assertEqual(json.loads((directory/'record.json').read_text()),self.record)
        self.assertFalse(any(s in ['/web_command','/switch_dest_floor_map'] or a.get('option')=='update' for s,a in self.f.calls))
    def test_metadata_update_preserves_image_and_backs_up_old_record(self):
        o=self.submit('map_update',target=self.target(),metadata=dict(name='改名',buildingInfo='A',floorInfo='二楼'))
        self.assertEqual(o['state'],'succeeded',o);self.assertEqual(self.f.maps[0]['name'],'改名');self.assertEqual(self.f.maps[0]['image'],self.record['image'])
        backup=json.loads((self.root/'exports'/o['result']['export_id']/'record.json').read_text());self.assertEqual(backup,self.record)
    def test_stale_target_and_cartographer_rebuild_rejected(self):
        target=self.target();target['md5']='0'*32
        self.assertEqual(self.submit('map_update',target=target,metadata=dict(name='a',buildingInfo='A',floorInfo='1'))['state'],'failed')
        target=self.target();target['type']='carto_map'
        with self.assertRaises(ValueError):self.submit('start',floor=2,name='a',buildingInfo='A',replace_target=target)
        self.assertFalse(any(a.get('option')=='update' for s,a in self.f.calls))
    def test_explicit_replace_backs_up_and_updates_once(self):
        self.assertEqual(self.start()['state'],'succeeded');before=self.m.session['replace_backup'];self.assertEqual(self.f.maps[0],self.record)
        o=self.submit('finish');self.assertEqual(o['state'],'succeeded',o);self.assertEqual(len(self.f.maps),1);self.assertEqual(self.f.maps[0]['name'],'新图')
        old=json.loads((self.root/'exports'/before['export_id']/'record.json').read_text());self.assertEqual(old,self.record)
        self.assertEqual(len([a for s,a in self.f.calls if a.get('option')=='update']),1)
        self.assertFalse(any(a.get('option') in ('insert','delete') for s,a in self.f.calls))
    def test_no_implicit_overwrite_and_no_switch_during_mapping(self):
        self.assertEqual(self.start(False)['state'],'failed');self.assertEqual(self.f.maps[0],self.record)
        self.submit('cancel');self.assertEqual(self.start()['state'],'succeeded')
        with self.assertRaises(RuntimeError):self.submit('map_switch',target=self.target())
    def test_replace_conflict_while_mapping_never_writes(self):
        self.start();self.f.maps[0]['name']='外部修改'
        self.assertEqual(self.submit('finish')['state'],'failed');self.assertFalse(any(a.get('option')=='update' for s,a in self.f.calls))
    def test_lost_replace_reply_checks_readback_without_replay(self):
        self.start();self.f.lose_update=True
        self.assertEqual(self.submit('finish')['state'],'uncertain');self.assertEqual(self.submit('finish')['state'],'succeeded')
        self.assertEqual(len([a for s,a in self.f.calls if a.get('option')=='update']),1)
    def test_unknown_replace_result_does_not_replay(self):
        self.start();self.f.lose_update=True;self.f.commit=False
        self.assertEqual(self.submit('finish')['state'],'uncertain');self.assertEqual(self.submit('finish')['state'],'uncertain')
        self.assertEqual(len([a for s,a in self.f.calls if a.get('option')=='update']),1)
    def test_switch_readback_without_starting_slam(self):
        o=self.submit('map_switch',target=self.target());self.assertEqual(o['state'],'succeeded',o);self.assertEqual(self.f.floor,2)
        self.assertFalse(any(s=='/web_command' for s,a in self.f.calls))
    def test_legacy_refresh_envelope_serves_management_and_backup_download(self):
        server=create_server(self.m,port=0);threading.Thread(target=server.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(server.server_port)
        try:
            body=json.dumps(dict(management_action='map_export',target=self.target(),request_id='http')).encode()
            with urllib.request.urlopen(urllib.request.Request(url+'/api/refresh',body,{'Content-Type':'application/json'})) as r:o=json.load(r)
            o=wait(self.m,o);self.assertEqual(o['state'],'succeeded',o)
            with urllib.request.urlopen(url+'/api/exports/'+o['result']['export_id']+'/record.json') as r:self.assertEqual(json.load(r),self.record)
        finally:server.shutdown();server.server_close()
