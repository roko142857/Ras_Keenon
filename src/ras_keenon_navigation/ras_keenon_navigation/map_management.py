"""Existing factory PNG records: preview, backup, metadata and explicit floor switch."""
import copy
import hashlib
import re
import time
import uuid
from .manager import atomic_json
from .mapping_transport import image_bytes, RpcUncertain


def target_identity(value):
    if not isinstance(value, dict) or set(value) != {'floor', 'type', 'md5'}:
        raise ValueError('需要目标地图 floor/type/md5')
    if type(value['floor']) is not int or not -32768 <= value['floor'] <= 32767 or value['floor'] == 0:
        raise ValueError('目标地图楼层无效')
    if value['type'] not in ('map', 'carto_map') or not isinstance(value['md5'], str) or not re.fullmatch('[0-9a-f]{32}', value['md5']):
        raise ValueError('目标地图类型或摘要无效')
    return copy.deepcopy(value)


class MapManagement:
    management_actions = ('map_preview', 'map_export', 'map_switch', 'map_update')

    def validate_management(self, action, value):
        fields = {'request_id', 'session_id', 'target'}
        if action == 'map_update': fields.add('metadata')
        if set(value)-fields: raise ValueError('未知地图管理字段')
        target_identity(value.get('target'))
        if action == 'map_update':
            from .mapping import metadata
            form = value.get('metadata')
            if not isinstance(form, dict) or set(form) != {'name', 'buildingInfo', 'floorInfo'}:
                raise ValueError('可编辑字段仅为名称、楼栋与楼层说明')
            metadata(dict(form, floor=value['target']['floor']))

    def selected_map(self, target):
        target_identity(target)
        records = self.query_maps(target['floor'], full=True)
        records = [r for r in records if r.get('floor') == target['floor'] and r.get('type') == target['type']]
        if len(records) != 1 or records[0].get('md5') != target['md5']:
            raise RuntimeError('目标地图已变化或不存在，请刷新列表重新选择')
        raw, _, _ = image_bytes(records[0]['image'])
        if hashlib.md5(raw).hexdigest() != target['md5']:
            raise RuntimeError('原厂目标地图图像与摘要不一致，未修改')
        return copy.deepcopy(records[0])

    def backup_map(self, record, purpose):
        raw, width, height = image_bytes(record['image'])
        identity = uuid.uuid4().hex
        directory = self.root/'exports'/identity; directory.mkdir(parents=True)
        (directory/'map.png').write_bytes(raw)
        atomic_json(directory/'record.json', record)
        atomic_json(directory/'report.json', dict(purpose=purpose, created_at=time.time(), backend=self.backend,
                    map={k:v for k,v in record.items() if k != 'image'}, width=width, height=height,
                    export_type='factory_map_record_backup', includes_native_slam_state=False,
                    includes_navigation_targets=False, note='地图PNG与记录备份；不含Cartographer状态、导航点或标签。'))
        atomic_json(directory/'sha256.json', {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})
        return dict(export_id=identity, files=['map.png', 'record.json', 'report.json', 'sha256.json'])

    def do_map_preview(self, value):
        record = self.selected_map(value['target'])
        return dict(note='已读取所选原厂地图；预览不会切换当前楼层。', **self.backup_map(record, 'preview'))

    def do_map_export(self, value):
        record = self.selected_map(value['target'])
        return dict(note='地图PNG与原始记录备份已导出。', **self.backup_map(record, 'export'))

    def do_map_update(self, value):
        from .mapping import metadata
        record = self.selected_map(value['target'])
        form = metadata(dict(value['metadata'], floor=record['floor']))
        backup = self.backup_map(record, 'before_metadata_update')
        record.update(form)
        values = self.call('/database/config_map', dict(option='update', map=[record]))
        if type(values.get('result')) is not int or values['result'] != 0:
            raise RuntimeError('原厂更新信息失败；旧记录备份 '+backup['export_id'])
        found = self.query_maps(record['floor'], full=True)
        if not any(self.same_record(r, record, True) for r in found):
            raise RpcUncertain('地图信息更新已提交但回读不一致，请刷新核对；备份 '+backup['export_id'])
        self.query_maps()
        return dict(note='地图信息已更新并回读；图像、类型与楼层编号保持原值。', **backup)

    def do_map_switch(self, value):
        record = self.selected_map(value['target'])
        status = self.factory.status()
        if not status['connected'] or status['mode'] != 0:
            raise RuntimeError('请先退出建图再切换当前地图')
        previous = self.current_floor(allow_zero=True)
        values = self.call('/switch_dest_floor_map', dict(floor=record['floor']))
        if values.get('result') is not True: raise RuntimeError('原厂切换地图失败：'+str(values))
        end = time.monotonic()+8
        while not self.done.is_set() and time.monotonic()<end:
            if self.current_floor(allow_zero=True) == record['floor']:
                return dict(note='当前楼层已切换并回读；没有发送导航目标或驾驶指令。', previous_floor=previous, current_floor=record['floor'])
            self.done.wait(.2)
        raise RpcUncertain('切图已提交但当前楼层回读未确认，请刷新核对')
