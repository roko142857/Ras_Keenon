"""Persistent, manually driven Gmapping sessions. Startup and shutdown never switch modes."""
import base64
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import queue
import re
import threading
import time
import uuid
from .map_management import MapManagement, target_identity
from .manager import atomic_json
from .mapping_transport import Factory, MockFactory, RpcUncertain, image_bytes, map_template

TERMINAL = {'saved', 'cancelled'}


def metadata(value):
    if set(value)-{'name', 'floor', 'floorInfo', 'buildingInfo', 'request_id', 'session_id', 'replace_target'}:
        raise ValueError('未知建图字段')
    floor = value.get('floor')
    if type(floor) is not int or not -9 <= floor <= 32767 or floor == 0 or floor % 200 == 0:
        raise ValueError('楼层需为 -9..32767 的非零整数，且不能为 200 的倍数')
    result = dict(floor=floor)
    for key in ('name', 'buildingInfo', 'floorInfo'):
        text = value.get(key, str(floor) if key == 'floorInfo' else '')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 80 or any(ord(c) < 32 for c in text):
            raise ValueError(key+' 需为 1–80 个可打印字符')
        result[key] = text.strip()
    if not re.fullmatch(r'[\w,\u4e00-\u9fff]+', result['buildingInfo']):
        raise ValueError('楼栋仅使用文字、数字、下划线或逗号')
    return result


class Mapping(MapManagement):
    def __init__(self, root, backend='mock', host='192.168.64.20', port=9090, factory=None):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.lease = (self.root/'mapping.lock').open('a')
        try: fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: self.lease.close(); raise RuntimeError('已有建图服务使用此数据目录')
        self.lock = threading.RLock(); self.done = threading.Event(); self.jobs = queue.Queue()
        path = self.root/'state.json'
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
            if backend not in ('mock', 'hardware'): raise ValueError('未知建图后端')
            if previous and previous.get('backend') != backend: raise ValueError('数据目录属于另一种后端')
        except Exception:
            self.lease.close(); raise
        self.backend = backend; self.factory = factory or (Factory(host, port) if backend == 'hardware' else MockFactory(self.root))
        if self.factory.kind != backend: self.factory.close(); self.lease.close(); raise ValueError('建图后端配置不一致')
        self.session = previous.get('session'); self.operations = previous.get('operations', [])[-80:]
        if self.session and self.session.get('phase') == 'saved':
            self.session['note'] = ('地图覆盖记录已保存并备份；原导航点/标签及地图质量需复核。' if self.session.get('replace_target') else '原厂地图记录已保存；仅确认数据回读，地图质量未经验证。')
        self.events = previous.get('events', [])[-60:]; self.busy = None
        self.current_floor_cached = None
        self.maps = []; self.maps_at = None; self.maps_error = ''; self.frozen = None
        if self.session and self.session.get('phase') not in TERMINAL:
            self.session['phase'] = 'recovery_required'
            self.session['note'] = '服务重新启动，未重发任何控制请求；可查询、重试保存回读或取消本会话。'
        for op in self.operations:
            if op['state'] in ('queued', 'running'): op.update(state='uncertain', note='服务中断，未自动重发')
        self.persist()
        self.worker = threading.Thread(target=self.work, daemon=True, name='mapping-operations'); self.worker.start()
        self.monitor = threading.Thread(target=self.watch, daemon=True, name='mapping-observation'); self.monitor.start()

    def persist(self):
        atomic_json(self.root/'state.json', dict(backend=self.backend, session=self.session, operations=self.operations[-80:], events=self.events[-60:]))
        if self.session:
            directory = self.root/'sessions'/self.session['id']; directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory/'session.json', self.session)

    def event(self, text, **values):
        with self.lock:
            self.events.append(dict(time=time.time(), text=text, **values)); self.events = self.events[-60:]; self.persist()

    def phase(self, phase, note):
        with self.lock: self.session.update(phase=phase, note=note); self.persist()

    def call(self, service, args):
        values = self.factory.rpc(service, args)
        # Receipts never include base64 image payloads.
        summary = {k:copy.deepcopy(v) for k,v in values.items() if k != 'map'}
        if 'map' in values: summary['map_count'] = len(values['map'])
        self.event('原厂服务回执', service=service, option=args.get('option'), values=summary)
        return values

    def query_maps(self, floor=0, full=False):
        value = map_template(floor)
        result = self.factory.rpc('/database/config_map', dict(option='select' if full else 'select_list', map=[value]))
        if type(result.get('result')) is not int or result['result'] != 0: raise RuntimeError('原厂地图查询失败：'+str(result.get('msg', result)))
        records = result.get('map')
        if not isinstance(records, list): raise RuntimeError('原厂地图列表无效')
        if not full:
            with self.lock:
                self.maps = [{k:v for k,v in record.items() if k != 'image'} for record in records if record.get('type') in ('map', 'carto_map')]
                self.maps_at = time.time(); self.maps_error = ''
        return records

    def current_floor(self, allow_zero=False):
        values = self.factory.rpc('/web_get_param', dict(option='current_floor'))
        if values.get('code') != 0: raise RuntimeError('无法读取原厂当前楼层')
        raw = json.loads(values['result']) if isinstance(values.get('result'), str) else values.get('result')
        if type(raw) is not int or (raw == 0 and not allow_zero): raise RuntimeError('原厂当前楼层无效')
        with self.lock: self.current_floor_cached = raw
        return raw

    def watch(self):
        refresh = 0.
        while not self.done.wait(.3):
            try:
                status = self.factory.status()
                with self.lock:
                    session = copy.deepcopy(self.session); busy = self.busy
                if session and session['phase'] in ('waiting_image', 'mapping'):
                    image = self.factory.image(True)
                    observed = bool(status['connected'] and status['mode'] == session.get('accepted_mode') and image and image['wall'] >= session['cutoff_wall'])
                    if observed and session['phase'] != 'mapping':
                        with self.lock:
                            if self.session['id'] == session['id'] and self.session['phase'] == 'waiting_image':
                                self.phase('mapping', '本次原厂 SLAM 图像接收中；请手动驾驶探索。')
                if status['connected'] and not busy and time.monotonic() >= refresh:
                    try: self.query_maps(); self.current_floor(allow_zero=True)
                    except Exception as exc:
                        with self.lock: self.maps_error = str(exc)
                    refresh = time.monotonic()+20.
            except Exception as exc:
                with self.lock: self.maps_error = str(exc)

    def submit(self, action, value):
        if action not in ('start', 'finish', 'cancel', 'export', 'refresh', *self.management_actions): raise ValueError('未知建图操作')
        identity = value.get('request_id')
        if not isinstance(identity, str) or not 1 <= len(identity) <= 120: raise ValueError('需要有效 request_id')
        form = metadata(value) if action == 'start' else None
        if action in self.management_actions: self.validate_management(action, value)
        if action == 'start' and value.get('replace_target') is not None:
            target_identity(value['replace_target'])
            if value['replace_target']['floor'] != form['floor'] or value['replace_target']['type'] != 'map':
                raise ValueError('覆盖重建仅支持目标楼层一致的 Gmapping map，carto_map请另建新图')
        fields = {'session_id', 'request_id'} | ({'allow_stale'} if action == 'finish' else set())
        if action != 'start' and action not in self.management_actions and set(value)-fields: raise ValueError('未知操作字段')
        if action == 'finish' and type(value.get('allow_stale', False)) is not bool: raise ValueError('allow_stale 必须为布尔值')
        signature = json.dumps([action, value], sort_keys=True, ensure_ascii=False)
        with self.lock:
            for op in self.operations:
                if op['id'] == identity:
                    if op['signature'] != signature: raise ValueError('request_id 已用于其他请求')
                    return copy.deepcopy(op)
            if self.busy: raise RuntimeError('已有建图操作正在处理中；手动遥控仍可使用')
            if action in ('map_switch', 'map_update') and self.session and self.session['phase'] not in TERMINAL:
                raise RuntimeError('请先结束或取消建图，再切换或修改已有地图')
            if action == 'start':
                if self.session and self.session['phase'] not in TERMINAL: raise RuntimeError('本次建图尚未结束，请先保存或取消')
                self.session = dict(id=uuid.uuid4().hex, form=form, phase='queued', note='启动请求已受理', created_at=time.time(), cutoff_wall=time.time(), insert_status='not_sent', replace_target=copy.deepcopy(value.get('replace_target')))
            elif action in ('finish', 'cancel', 'export'):
                if not self.session or value.get('session_id') != self.session['id']: raise ValueError('建图会话不匹配')
                if action in ('finish', 'cancel') and self.session['phase'] in TERMINAL and not (action == 'cancel' and self.session['phase'] == 'cancelled' and self.session.get('original_floor')):
                    raise RuntimeError('本次建图已结束')
            op = dict(id=identity, action=action, signature=signature, state='queued', time=time.time(), note='请求已受理', session_id=self.session['id'] if self.session else None)
            if action in self.management_actions: op['payload'] = copy.deepcopy(value)
            if action == 'finish': self.session['allow_stale_save'] = value.get('allow_stale', False)
            self.operations.append(op); self.operations = self.operations[-80:]; self.busy = identity; self.persist(); self.jobs.put(op)
            return copy.deepcopy(op)

    def work(self):
        while not self.done.is_set():
            try: op = self.jobs.get(timeout=.2)
            except queue.Empty: continue
            with self.lock: op.update(state='running', note='处理中'); self.persist()
            try:
                handler = getattr(self, 'do_'+op['action'])
                result = handler(op['payload']) if op['action'] in self.management_actions else handler()
                with self.lock: op.update(state='succeeded', note=result.get('note', '操作完成'), result=result)
            except Exception as exc:
                with self.lock:
                    uncertain = isinstance(exc, RpcUncertain)
                    op.update(state='uncertain' if uncertain else 'failed', note=str(exc))
                    if self.session and op['action'] in ('start', 'finish', 'cancel'):
                        self.session.update(phase='result_unknown' if uncertain else 'error', note=str(exc))
            finally:
                with self.lock: self.busy = None; op['finished_at'] = time.time(); self.persist()

    def do_refresh(self):
        self.query_maps(); return dict(note='原厂地图列表已更新')

    def do_start(self):
        status = self.factory.status()
        if not status['connected']: raise RuntimeError('原厂尚未连接')
        if status['mode'] != 0: raise RuntimeError('原厂当前不是已确认的导航模式，未发送启动请求')
        records = self.query_maps()
        if self.session.get('replace_target'):
            target = self.selected_map(self.session['replace_target'])
            backup = self.backup_map(target, 'before_replace')
            self.session.update(replace_before={k:v for k,v in target.items() if k != 'image'}, replace_backup=backup)
            self.persist()
        elif any(x.get('floor') == self.session['form']['floor'] for x in records):
            raise RuntimeError('此楼层已有记录，请使用未占用楼层，或在已有地图管理中选择覆盖重建')
        floor = self.current_floor()
        original = self.query_maps(floor, full=True)
        directory = self.root/'sessions'/self.session['id']
        atomic_json(directory/'before_maps.json', dict(floor=floor, map=original, includes_native_slam_state=False))
        with self.lock:
            self.session.update(original_floor=floor, original_maps=self.maps, cutoff_wall=time.time(), phase='starting', note='等待原厂启动回执'); self.persist()
        values = self.call('/web_command', dict(mainCmd=1, subCmd=0))
        if values.get('result') is True and values.get('code') == 0:
            raise RuntimeError('原厂返回导航模式0，未确认建图；可能忽略了重复启动。请取消本会话以明确返回导航后再启动。')
        if values.get('result') is not True or type(values.get('code')) is not int or values['code'] == 0:
            raise RuntimeError('原厂没有确认切换到建图模式：'+str(values))
        with self.lock: self.session['accepted_mode'] = values['code']; self.persist()
        self.phase('waiting_image', '原厂接受启动请求，等待本次 SLAM 图像。')
        return dict(note='启动请求已接受；地图和模式反馈另行确认', accepted_mode=values['code'], physically_verified=False)

    def session_image(self):
        with self.lock: session = copy.deepcopy(self.session)
        if not session: raise RuntimeError('尚无建图会话')
        image = self.factory.image(True)
        status = self.factory.status()
        if not status['connected'] or not image or image['wall'] < session['cutoff_wall']:
            raise RuntimeError('尚无本会话的新鲜原厂建图图像，未使用旧 /map')
        if status['mode'] == 0 or (session.get('accepted_mode') is not None and status['mode'] != session['accepted_mode']):
            raise RuntimeError('原厂模式与本会话不匹配')
        return image

    def prepare_save(self):
        with self.lock: prepared = self.session.get('prepared')
        directory = self.root/'sessions'/self.session['id']; path = directory/'frozen.png'
        if prepared:
            raw = path.read_bytes()
            if hashlib.md5(raw).hexdigest() != prepared['md5']: raise RuntimeError('待保存图像摘要不匹配')
            record = copy.deepcopy(prepared)
        else:
            image = self.session_image(); raw = image['raw']; image_bytes(image['message'])
            age = max(0., time.time()-image['wall'])
            if age > 30 and not self.session.get('allow_stale_save'):
                with self.lock: self.session['stale_save_confirmation_required'] = True; self.persist()
                raise RuntimeError(f'建图图像已 {age:.0f} 秒未更新；未写入数据库。请检查图源，或再次点击明确保存停更快照。')
            record = dict(**self.session['form'], type='map', md5=hashlib.md5(raw).hexdigest(), image=copy.deepcopy(image['message']))
            record['image'].pop('data', None); path.write_bytes(raw)
            with self.lock:
                self.session['prepared'] = copy.deepcopy(record); self.session['frozen_source'] = dict(topic=image['topic'], received_at=image['wall'], version=image['version'], age_at_save_s=age, stale_confirmed=age>30); self.persist()
        record['image']['data'] = base64.b64encode(raw).decode()
        return record

    @staticmethod
    def same_record(found, expected, include_image=False):
        if any(found.get(k) != expected.get(k) for k in ('floor', 'name', 'type', 'md5', 'floorInfo', 'buildingInfo')): return False
        if include_image:
            try: raw, _, _ = image_bytes(found['image'])
            except (ValueError, KeyError, TypeError): return False
            return hashlib.md5(raw).hexdigest() == expected['md5']
        return True

    def do_finish(self):
        record = self.prepare_save(); self.phase('saving', '保存本次冻结地图，等待原厂数据库回执。')
        records = self.query_maps()
        occupied = [r for r in records if r.get('floor') == record['floor']]
        matching = any(self.same_record(r, record) for r in occupied)
        replace = self.session.get('replace_target')
        if replace:
            if not matching:
                prior = self.session.get('insert_status', 'not_sent')
                if prior in ('pending', 'accepted', 'confirmed'):
                    raise RpcUncertain('曾提交覆盖但回读未确认；不会自动重复写入，请核对原厂地图和备份')
                old = self.selected_map(replace)
                if not self.same_record(old, self.session['replace_before']):
                    raise RuntimeError('目标地图信息已变化，未覆盖；旧图备份已保留')
                # A second read and backup catches changes made during the mapping session.
                self.session['replace_final_backup'] = self.backup_map(old, 'before_replace_write')
                self.session['insert_status'] = 'pending'; self.persist()
                values = self.call('/database/config_map', dict(option='update', map=[record]))
                if type(values.get('result')) is not int or values['result'] != 0:
                    self.session['insert_status'] = 'rejected'; self.persist()
                    raise RuntimeError('原厂覆盖拒绝：'+str(values.get('msg', values)))
                self.session['insert_status'] = 'accepted'; self.persist()
        else:
            if occupied and not matching: raise RuntimeError('目标楼层已有其他地图，未覆盖')
            if not occupied:
                with self.lock:
                    prior = self.session.get('insert_status', 'not_sent')
                    if prior in ('pending', 'accepted', 'confirmed'):
                        raise RpcUncertain('曾提交保存但尚未查到记录；仅查询，不重复插入')
                    self.session['insert_status'] = 'pending'; self.persist()
                values = self.call('/database/config_map', dict(option='insert', map=[record]))
                if type(values.get('result')) is not int or values['result'] != 0:
                    with self.lock: self.session['insert_status'] = 'rejected'; self.persist()
                    raise RuntimeError('原厂保存拒绝：'+str(values.get('msg', values)))
                with self.lock: self.session['insert_status'] = 'accepted'; self.persist()
        found = self.query_maps(record['floor'], full=True)
        if not any(self.same_record(r, record, True) for r in found):
            raise RpcUncertain('原厂保存尚未通过图像与元数据回读；可再次点击结束保存进行查询')
        with self.lock:
            self.session['insert_status'] = 'confirmed'; self.session['saved_map'] = {k:v for k,v in record.items() if k != 'image'}; self.persist()
        self.phase('returning', '原厂地图已回读确认，切换新楼层并返回导航模式。')
        self.restore(record['floor'])
        self.query_maps(); self.phase('saved', '原厂地图记录已保存并回读确认，已返回导航模式；地图质量未经验证。')
        if replace:
            self.phase('saved', '地图已覆盖并回读确认；旧图备份已保留，原导航点/标签与地图质量需重新复核。')
        return dict(note=self.session['note'], map=self.session['saved_map'], native_database_saved=True, native_slam_resume_available=False, backup=self.session.get('replace_backup'))

    def restore(self, floor, force_navigation=False):
        if self.current_floor(allow_zero=True) != floor:
            values = self.call('/switch_dest_floor_map', dict(floor=floor))
            if values.get('result') is not True: raise RuntimeError('原厂切换楼层失败：'+str(values))
        if force_navigation or self.factory.status()['mode'] != 0:
            values = self.call('/web_command', dict(mainCmd=0, subCmd=0))
            if values.get('result') is not True or values.get('code') != 0: raise RpcUncertain('返回导航模式的结果待确认')
        end = time.monotonic()+8.
        while not self.done.is_set() and time.monotonic() < end:
            if self.factory.status()['connected'] and self.factory.status()['mode'] == 0 and self.current_floor(allow_zero=True) == floor: return
            self.done.wait(.3)
        raise RpcUncertain('地图处理已提交，原厂模式/楼层反馈仍待确认')

    def do_cancel(self):
        floor = self.session.get('original_floor')
        if floor is not None:
            self.phase('cancelling', '退出本次建图，恢复进入前楼层。'); self.restore(floor, force_navigation=True)
        elif self.factory.status()['mode'] != 0:
            raise RuntimeError('没有原始楼层记录，未擅自退出外部建图')
        self.phase('cancelled', '本次建图已取消；原有地图记录未删除。')
        return dict(note=self.session['note'], saved_map=self.session.get('saved_map'))

    def do_export(self):
        if self.session.get('prepared'):
            raw = (self.root/'sessions'/self.session['id']/'frozen.png').read_bytes()
            _, width, height = image_bytes(dict(data=base64.b64encode(raw).decode()))
            source = self.session['frozen_source']
            image = dict(raw=raw, width=width, height=height, topic=source['topic'], wall=source['received_at'])
        else: image = self.session_image()
        identity = uuid.uuid4().hex; directory = self.root/'exports'/identity; directory.mkdir(parents=True)
        (directory/'map.png').write_bytes(image['raw'])
        report = dict(id=identity, backend=self.backend, simulated=self.backend=='mock', session_id=self.session['id'], form=self.session['form'],
                      source_topic=image['topic'], received_at=image['wall'], width=image['width'], height=image['height'],
                      export_type='factory_png_and_metadata', native_database_saved=False, native_slam_resume_available=False,
                      note='保留原厂 PNG；未从像素猜测米制坐标或生成 PGM/YAML。')
        atomic_json(directory/'report.json', report)
        atomic_json(directory/'sha256.json', {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})
        return dict(note='本次原厂图像与元数据快照已导出', export_id=identity, files=['map.png', 'report.json', 'sha256.json'])

    def snapshot(self):
        status = self.factory.status()
        with self.lock:
            session = copy.deepcopy(self.session)
            active = bool(session and session['phase'] not in TERMINAL)
            image = self.display_image(active)
            if active and image and image['wall'] < session['cutoff_wall']: image = None
            return dict(backend=self.backend, simulated=self.backend=='mock', factory=status, session=session,
                        image=None if not image else dict(**{k:image[k] for k in ('width', 'height', 'version', 'wall', 'topic', 'native_png')}, age_s=max(0.,time.time()-image['wall'])),
                        busy=self.busy, operations=copy.deepcopy(self.operations[-30:]), events=copy.deepcopy(self.events[-30:]),
                        current_floor=self.current_floor_cached, maps=copy.deepcopy(self.maps), maps_updated_at=self.maps_at, maps_error=self.maps_error,
                        autonomous_exploration=False)

    def preview(self):
        with self.lock:
            active = bool(self.session and self.session['phase'] not in TERMINAL)
            image = self.display_image(active)
            if not image or active and image['wall'] < self.session['cutoff_wall']: raise RuntimeError('等待本次地图画面')
            return image['raw'], image['version']

    def display_image(self, active):
        if self.session and self.session.get('prepared') and self.session['phase'] != 'cancelled':
            key = self.session['id']
            if self.frozen is None or self.frozen[0] != key:
                raw = (self.root/'sessions'/key/'frozen.png').read_bytes()
                _, width, height = image_bytes(dict(data=base64.b64encode(raw).decode()))
                prepared = self.session['prepared']; source = self.session['frozen_source']
                if hashlib.md5(raw).hexdigest() != prepared['md5']: raise RuntimeError('冻结地图摘要无效')
                self.frozen = (key, dict(raw=raw, width=width, height=height, version=prepared['md5'], at=0,
                                         wall=source['received_at'], topic=source['topic']+'（冻结快照）', native_png=True))
            return self.frozen[1]
        return self.factory.image(active)

    def close(self):
        self.done.set(); self.factory.close(); self.worker.join(timeout=3); self.monitor.join(timeout=3); self.lease.close()
