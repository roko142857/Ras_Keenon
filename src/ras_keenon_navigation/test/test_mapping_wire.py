"""Real RFC6455 frames and >64 KiB factory saves, entirely on loopback."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
import zlib
from ras_keenon_navigation.mapping import Mapping
from ras_keenon_navigation.mapping_transport import Factory


def chunk(kind, value):return struct.pack('!I',len(value))+kind+value+struct.pack('!I',zlib.crc32(kind+value)&0xffffffff)


class Fixture:
    def __init__(self):
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen();self.port=self.sock.getsockname()[1]
        self.conn=None;self.records=[];self.calls=[];self.mode=0;self.floor=1;self.max_request=0;self.delay=0
        raw=b''.join(b'\0'+os.urandom(600*3) for _ in range(500))
        self.png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',600,500,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
        self.image=dict(header=dict(seq=1,stamp=dict(secs=1513570000,nsecs=123),frame_id='map'),format='png',data=base64.b64encode(self.png).decode())
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def exact(self,n):
        data=b''
        while len(data)<n:
            v=self.conn.recv(n-len(data))
            if not v:raise EOFError
            data+=v
        return data
    def send(self,value):
        data=json.dumps(value).encode();n=len(data)
        prefix=bytes([129,n]) if n<126 else bytes([129,126])+struct.pack('!H',n) if n<=65535 else bytes([129,127])+struct.pack('!Q',n)
        self.conn.sendall(prefix+data)
    def publish(self,topic,msg):self.send(dict(op='publish',topic=topic,msg=msg))
    def run(self):
        try:
            self.conn,_=self.sock.accept();header=b''
            while b'\r\n\r\n' not in header:header+=self.conn.recv(1)
            key=next(line.split(b':',1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
            accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            self.conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
            while True:
                a,b=self.exact(2);n=b&127
                if n==126:n=struct.unpack('!H',self.exact(2))[0]
                elif n==127:n=struct.unpack('!Q',self.exact(8))[0]
                mask=self.exact(4) if b&128 else None;raw=self.exact(n)
                if a&15==8:break
                data=bytes(v^mask[i%4] for i,v in enumerate(raw)) if mask else raw
                value=json.loads(data);self.max_request=max(self.max_request,len(data));self.calls.append(value)
                if value['op']=='subscribe':
                    if value['topic']=='/robot_state':self.publish('/robot_state',dict(mode=self.mode))
                    if value['topic']=='/map/to_web/compressed':self.publish(value['topic'],self.image)
                if value['op']!='call_service':continue
                service=value['service'];args=value['args'];v={}
                if service=='/web_get_param':v=dict(code=0,result=str(self.floor))
                elif service=='/web_command':
                    if self.delay:time.sleep(self.delay)
                    self.mode=2 if args['mainCmd']==1 else 0;v=dict(result=True,code=self.mode)
                elif service=='/switch_dest_floor_map':self.floor=args['floor'];v=dict(result=True,msg='fixture')
                elif service=='/database/config_map':
                    option=args['option'];floor=args['map'][0]['floor']
                    if option=='insert':self.records.append(args['map'][0])
                    rows=self.records if option=='select_list' else [x for x in self.records if x['floor']==floor]
                    v=dict(result=0,msg='fixture',map=rows)
                self.send(dict(op='service_response',id=value['id'],service=service,result=True,values=v))
                if service=='/web_command':
                    self.publish('/robot_state',dict(mode=self.mode))
                    if self.mode:self.publish('/gmapping_map/to_web/compressed',self.image)
        except (OSError,EOFError):pass
    def close(self):
        if self.conn:
            try:self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            self.conn.close()
        self.sock.close();self.thread.join(timeout=2)


def wait(fn, timeout=10):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if fn():return
        time.sleep(.02)
    raise AssertionError('timeout')


class WireTests(unittest.TestCase):
    def test_large_masked_save_frames_and_native_readback(self):
        fixture=Fixture();tmp=tempfile.TemporaryDirectory();factory=Factory('127.0.0.1',fixture.port);m=Mapping(Path(tmp.name),backend='hardware',factory=factory)
        try:
            wait(lambda:factory.status()['mode']==0)
            m.submit('start',dict(request_id='start',name='wire',floor=2,buildingInfo='LAB'))
            wait(lambda:m.busy is None);wait(lambda:m.snapshot()['session']['phase']=='mapping')
            m.submit('finish',dict(request_id='finish',session_id=m.session['id']))
            wait(lambda:m.busy is None,20)
            self.assertEqual(m.session['phase'],'saved',m.snapshot()['operations'])
            self.assertGreater(fixture.max_request,65535);self.assertGreater(len(fixture.png),800000)
            self.assertEqual(base64.b64decode(fixture.records[0]['image']['data']),fixture.png)
            self.assertEqual(fixture.records[0]['image']['header']['stamp'],fixture.image['header']['stamp'])
            self.assertTrue(all(v['op'] in ('subscribe','unsubscribe','call_service') for v in fixture.calls))
            self.assertTrue(any(v.get('op')=='subscribe' and v.get('topic')=='/gmapping_map/to_web/compressed' and v.get('type')=='sensor_msgs/CompressedImage' for v in fixture.calls))
        finally:m.close();fixture.close();tmp.cleanup()


if __name__=='__main__':unittest.main()
