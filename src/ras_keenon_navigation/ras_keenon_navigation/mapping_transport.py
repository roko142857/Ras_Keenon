"""Factory mapping transport. No velocity, goal, SDK or database delete operations."""
import base64
import copy
import hashlib
import json
import queue
import struct
import threading
import time
import uuid
import zlib

MAX_BYTES = 32_000_000
IMAGE_TOPICS = ('/map/to_web/compressed', '/gmapping_map/to_web/compressed')
TOPICS = (*IMAGE_TOPICS, '/map', '/robot_state')
TOPIC_TYPES = {**{name:'sensor_msgs/CompressedImage' for name in IMAGE_TOPICS},
               '/map':'nav_msgs/OccupancyGrid', '/robot_state':'web_backend/robot_state'}


def image_bytes(message):
    data = message.get('data', '')
    raw = base64.b64decode(data, validate=True) if isinstance(data, str) else bytes(data)
    if not 24 <= len(raw) <= MAX_BYTES or raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('原厂图像不是有效 PNG')
    width, height = struct.unpack('!II', raw[16:24])
    if not 0 < width <= 16384 or not 0 < height <= 16384 or width*height > 32_000_000:
        raise ValueError('原厂图像尺寸无效')
    pos = 8
    while pos+12 <= len(raw):
        length = struct.unpack('!I', raw[pos:pos+4])[0]
        end = pos+12+length
        if end > len(raw): raise ValueError('PNG 数据不完整')
        chunk = raw[pos+4:pos+8+length]
        if zlib.crc32(chunk)&0xffffffff != struct.unpack('!I', raw[end-4:end])[0]:
            raise ValueError('PNG 校验失败')
        pos = end
        if chunk[:4] == b'IEND':
            if pos != len(raw): raise ValueError('PNG 尾部数据无效')
            return raw, width, height
    raise ValueError('PNG 缺少结束块')


def map_template(floor=0):
    return dict(name='', floor=floor, floorInfo='', buildingInfo='', type='', md5='',
                image=dict(header=dict(seq=0, frame_id='map', stamp=dict(secs=0, nsecs=0)), format='png', data=''))


def allowed(service, args):
    if service == '/web_command':
        return set(args) == {'mainCmd', 'subCmd'} and type(args['mainCmd']) is int and args['mainCmd'] in (0, 1) and args['subCmd'] == 0
    if service == '/web_get_param': return args == dict(option='current_floor')
    if service == '/switch_dest_floor_map':
        return set(args) == {'floor'} and type(args['floor']) is int and -32768 <= args['floor'] <= 32767 and args['floor'] != 0
    if service == '/database/config_map':
        return set(args) == {'option', 'map'} and args['option'] in ('select_list', 'select', 'insert', 'update') and isinstance(args['map'], list) and len(args['map']) == 1
    return False


class RpcUncertain(RuntimeError):
    """A request may have reached the factory. Never automatically replay it."""


def websocket(host, port):
    from ras_keenon_commands import rosbridge
    rosbridge.MAX_FRAME = MAX_BYTES  # This independent process only.

    class LargeRosbridge(rosbridge.Rosbridge):
        def send_frame(self, data, opcode=1):
            import os
            if len(data) > MAX_BYTES: raise ValueError('原厂请求超过接收上限')
            mask = os.urandom(4)
            n = len(data)
            prefix = bytes([128 | opcode, 128 | n]) if n < 126 else bytes([128 | opcode, 254])+struct.pack('!H', n) if n <= 65535 else bytes([128 | opcode, 255])+struct.pack('!Q', n)
            self.sock.sendall(prefix+mask+bytes(v ^ mask[i % 4] for i, v in enumerate(data)))
    return LargeRosbridge(host, port)


class Factory:
    kind = 'hardware'

    def __init__(self, host='192.168.64.20', port=9090, transport=websocket):
        self.host, self.port, self.transport = host, port, transport
        self.lock = threading.RLock(); self.done = threading.Event()
        self.connected = False; self.error = ''; self.mode = self.mode_at = None
        self.images = {}; self.pending = {}; self.outgoing = queue.Queue()
        self.thread = threading.Thread(target=self.run, daemon=True, name='factory-mapping-transport')
        self.thread.start()

    def ingest(self, value):
        if value.get('op') == 'service_response':
            with self.lock:
                item = self.pending.get(value.get('id'))
                if item: item['response'] = value; item['event'].set()
            return
        if value.get('op') != 'publish': return
        topic, message = value.get('topic'), value.get('msg', {})
        now = time.monotonic()
        if topic == '/robot_state':
            code = message.get('mode')
            if type(code) is not int or not 0 <= code <= 255: return
            with self.lock: self.mode = code; self.mode_at = now
        elif topic in IMAGE_TOPICS:
            raw, width, height = image_bytes(message)
            with self.lock:
                self.images[topic] = dict(raw=raw, message=copy.deepcopy(message), width=width, height=height,
                                          version=hashlib.md5(raw).hexdigest(), at=now, wall=time.time(), topic=topic, native_png=True)
        elif topic == '/map':
            # Idle preview fallback only; never use /map to save a new SLAM session.
            from .grid import Grid
            from .manager import png
            grid = Grid.from_ros(message, 'factory-idle')
            raw = png(grid)
            with self.lock:
                self.images[topic] = dict(raw=raw, message=None, width=grid.width, height=grid.height,
                                          version=grid.version, at=now, wall=time.time(), topic=topic, native_png=False)

    def run(self):
        delay = .5
        while not self.done.is_set():
            ws = None
            try:
                ws = self.transport(self.host, self.port)
                if hasattr(ws, 'sock'): ws.sock.settimeout(.05)
                for topic in TOPICS:
                    ws.send(dict(op='subscribe', id='ras-mapping'+topic, topic=topic, type=TOPIC_TYPES[topic], queue_length=1, throttle_rate=500))
                with self.lock: self.connected = True; self.error = ''; self.mode = self.mode_at = None
                delay = .5
                while not self.done.is_set():
                    try: identity = self.outgoing.get_nowait()
                    except queue.Empty: identity = None
                    if identity:
                        with self.lock:
                            item = self.pending.get(identity)
                            if item:
                                # Mark before send: a partial send cannot be ruled out on failure.
                                item['sent'] = True
                                ws.send(dict(op='call_service', id=identity, service=item['service'], args=item['args']))
                    value = ws.receive()
                    if value:
                        try: self.ingest(value)
                        except (ValueError, KeyError, TypeError) as exc:
                            with self.lock: self.error = '未采纳图像：'+str(exc)
            except Exception as exc:
                with self.lock: self.error = str(exc)
            finally:
                with self.lock:
                    self.connected = False
                    for item in self.pending.values():
                        if not item['event'].is_set(): item['error'] = self.error or '原厂连接中断'; item['event'].set()
                if ws:
                    for topic in TOPICS:
                        try: ws.send(dict(op='unsubscribe', id='ras-mapping'+topic, topic=topic))
                        except Exception: pass
                    try: ws.close()
                    except Exception: pass
            if self.done.wait(delay): break
            delay = min(5., delay*2)

    def rpc(self, service, args, timeout=15.):
        if not allowed(service, args): raise ValueError('建图服务拒绝未知操作')
        identity = 'mapping-'+uuid.uuid4().hex
        item = dict(service=service, args=copy.deepcopy(args), event=threading.Event(), sent=False)
        with self.lock:
            if not self.connected: raise RuntimeError('尚未连接原厂建图服务')
            self.pending[identity] = item
        self.outgoing.put(identity)
        try:
            if not item['event'].wait(timeout):
                if item['sent']: raise RpcUncertain('原厂回执超时，结果待确认；未自动重发')
                raise RuntimeError('原厂请求排队超时，未发送')
            if item.get('error'):
                if item['sent']: raise RpcUncertain(item['error']+'；请求结果待确认')
                raise RuntimeError(item['error'])
            response = item['response']
            if response.get('result') is not True: raise RuntimeError('原厂服务拒绝：'+str(response.get('values', response)))
            return response.get('values', {})
        finally:
            with self.lock: self.pending.pop(identity, None)

    def status(self):
        with self.lock:
            return dict(connected=self.connected, error=self.error, mode=self.mode,
                        mode_age_s=None if self.mode_at is None else round(time.monotonic()-self.mode_at, 2))

    def image(self, mapping=False):
        with self.lock:
            return copy.deepcopy(self.images.get(IMAGE_TOPICS[1]) if mapping else self.images.get(IMAGE_TOPICS[0]) or self.images.get('/map'))

    def close(self):
        self.done.set(); self.thread.join(timeout=3)


class MockFactory:
    """Local factory simulator; no sockets to a robot are opened."""
    kind = 'mock'

    def __init__(self, root):
        from pathlib import Path
        self.root = Path(root); self.path = self.root/'mock_database.json'
        self.maps = json.loads(self.path.read_text()) if self.path.exists() else []
        self.mode = 0; self.floor = 1; self.started = None; self.done = False

    def status(self): return dict(connected=True, error='', mode=self.mode, mode_age_s=0.)

    def rpc(self, service, args, timeout=15.):
        if not allowed(service, args): raise ValueError('建图服务拒绝未知操作')
        if service == '/web_command':
            self.mode = 2 if args['mainCmd'] == 1 else 0  # Mode != mainCmd, deliberately.
            if self.mode: self.started = time.monotonic()
            return dict(result=True, code=self.mode)
        if service == '/web_get_param': return dict(code=0, result=str(self.floor))
        if service == '/switch_dest_floor_map': self.floor = args['floor']; return dict(result=True, msg='mock')
        option = args['option']; floor = args['map'][0]['floor']
        if option == 'update':
            record = copy.deepcopy(args['map'][0])
            found = next((i for i,x in enumerate(self.maps) if x['floor']==floor and x['type']==record['type']), None)
            if found is None: return dict(result=1, msg='map missing', map=[])
            self.maps[found] = record
            from .manager import atomic_json
            atomic_json(self.path, self.maps)
        if option == 'insert':
            if any(x['floor'] == floor for x in self.maps): return dict(result=1, msg='floor exists', map=[])
            self.maps.append(copy.deepcopy(args['map'][0]))
            from .manager import atomic_json
            atomic_json(self.path, self.maps)
        values = self.maps if option == 'select_list' else [x for x in self.maps if x['floor'] == floor]
        return dict(result=0, msg='mock', map=copy.deepcopy(values))

    def image(self, mapping=False):
        if mapping and not self.mode: return None
        import numpy as np
        from .grid import Grid
        from .manager import png
        data = np.full((130, 180), -1, dtype=np.int16)
        extent = 80 if not mapping else min(165, 55+int((time.monotonic()-self.started)*4))
        data[20:110, 20:extent] = 0; data[20, 20:extent] = 100; data[109, 20:extent] = 100; data[20:110, 20] = 100
        data[48:60, 50:65] = 100; data[80:92, 95:110] = 100
        raw = png(Grid(data, .05, (0, 0, 0), 'mock-mapping'))
        return dict(raw=raw, message=dict(header=dict(seq=0, frame_id='map', stamp=dict(secs=0, nsecs=0)), format='png', data=base64.b64encode(raw).decode()),
                    width=180, height=130, version=hashlib.md5(raw).hexdigest(), at=time.monotonic(), wall=time.time(),
                    topic=IMAGE_TOPICS[1] if mapping else IMAGE_TOPICS[0], native_png=True)

    def close(self): self.done = True
