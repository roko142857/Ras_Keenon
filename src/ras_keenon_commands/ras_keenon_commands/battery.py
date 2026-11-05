"""Read-only chassis battery subscription, independent of motion and SDK threads."""
import math
import threading
import time

from .rosbridge import HOST, PORT, Rosbridge

TOPIC = '/battery_state'
CHARGE_TOPIC = '/charge_state_fromSTM32'
SUPPLY_STATES = {0:'unknown',1:'charging',2:'discharging',3:'not_charging',4:'full'}


def mock_battery():
    return dict(status='mock', percentage=None, updated_at=None, age_s=None,
                stale_after_s=30., quality='mock', source_topic=TOPIC,
                error='', note='模拟控制器未连接真实电池。',
                power=dict(adapter_connected=None,charging_status='unknown',raw_charge_state=None))


class BatteryCache:
    def __init__(self, stale_after=30., monotonic=time.monotonic, wall_time=time.time):
        self.stale_after, self.monotonic, self.wall_time = stale_after, monotonic, wall_time
        self.lock = threading.Lock()
        self.percentage = self.received = self.updated_at = None
        self.connected = self.current_sample = False
        self.error, self.quality = '', 'unknown'
        self.supply_status = self.charge_state = None
        self.charge_received = self.charge_updated_at = None
        self.current_charge_sample = False

    def connection(self, connected, error=''):
        with self.lock:
            self.connected, self.current_sample, self.error = connected, False, error
            self.current_charge_sample = False

    def ingest(self, message):
        value = message.get('percentage') if isinstance(message, dict) else None
        with self.lock:
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                self.error, self.current_sample = '底盘电量字段无效（应为 0–1 的有限数）。', False
                return False
            self.percentage = round(value*100., 2)
            self.received, self.updated_at = self.monotonic(), self.wall_time()
            self.current_sample, self.error = True, ''
            # This chassis often leaves present/voltage/current unfilled. Do not infer charging.
            self.quality = 'reported' if message.get('present') is True else 'unverified'
            supply = message.get('power_supply_status')
            self.supply_status = supply if type(supply) is int and supply in SUPPLY_STATES else None
            return True

    def ingest_charge(self, message):
        state = message.get('state') if isinstance(message, dict) else None
        if type(state) is not int or not -(2**31) <= state < 2**31: return False
        with self.lock:
            self.charge_state = state
            self.charge_received, self.charge_updated_at = self.monotonic(), self.wall_time()
            self.current_charge_sample = True
        return True

    def snapshot(self):
        with self.lock:
            age = None if self.received is None else max(0., self.monotonic()-self.received)
            live = self.connected and self.current_sample and age is not None and age <= self.stale_after
            status = 'live' if live else 'stale' if self.percentage is not None else 'unavailable' if self.error else 'waiting'
            charge_age = None if self.charge_received is None else max(0.,self.monotonic()-self.charge_received)
            charge_fresh = bool(self.connected and self.current_charge_sample and charge_age is not None and charge_age<=self.stale_after)
            # Charging/docking is not evidence of a directly connected AC adapter.
            power = dict(adapter_connected=None,adapter_status='unconfirmed',
                         charging_status=SUPPLY_STATES.get(self.supply_status,'unknown') if live and self.quality=='reported' else 'unknown',
                         battery_supply_status=self.supply_status,
                         raw_charge_state=self.charge_state,raw_charge_source_topic=CHARGE_TOPIC,
                         raw_charge_fresh=charge_fresh,raw_charge_age_s=charge_age,
                         raw_charge_updated_at=self.charge_updated_at,raw_charge_mapping='unverified',
                         note='尚未取得已验证的适配器直连信号；充电状态不等同于适配器连接状态。')
            return dict(status=status, percentage=self.percentage, updated_at=self.updated_at,
                        age_s=age, stale_after_s=self.stale_after, quality=self.quality,
                        source_topic=TOPIC, error=self.error,
                        note='底盘上报值；电池存在状态未确认。' if self.quality=='unverified' else '底盘上报值。',power=power)


class BatteryMonitor:
    def __init__(self, host=HOST, port=PORT, stale_after=30., transport=Rosbridge):
        self.host, self.port, self.transport = host, port, transport
        self.cache = BatteryCache(stale_after)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='battery-telemetry')
        self.thread.start()

    def run(self):
        delay = .5
        while not self.closed.is_set():
            ws = None
            try:
                ws = self.transport(self.host, self.port)
                ws.sock.settimeout(.2)
                ws.send(dict(op='subscribe', id='ras-battery', topic=TOPIC,
                             type='sensor_msgs/BatteryState', queue_length=1, throttle_rate=1000))
                ws.send(dict(op='subscribe', id='ras-power', topic=CHARGE_TOPIC,
                             type='keenon_charge_msgs/ChargeFB', queue_length=1, throttle_rate=1000))
                self.cache.connection(True)
                delay = .5
                while not self.closed.is_set():
                    message = ws.receive()
                    if not message: continue
                    if message.get('op') == 'publish' and message.get('topic') == TOPIC:
                        self.cache.ingest(message.get('msg'))
                    elif message.get('op')=='publish' and message.get('topic')==CHARGE_TOPIC:
                        self.cache.ingest_charge(message.get('msg'))
                    elif message.get('op')=='status' and message.get('level')=='error':
                        raise RuntimeError(message.get('msg','电量订阅失败'))
            except Exception as exc:
                self.cache.connection(False, str(exc))
            finally:
                if ws:
                    try: ws.send(dict(op='unsubscribe', id='ras-battery', topic=TOPIC))
                    except Exception: pass
                    try: ws.send(dict(op='unsubscribe', id='ras-power', topic=CHARGE_TOPIC))
                    except Exception: pass
                    try: ws.close()
                    except Exception: pass
            if self.closed.wait(delay): break
            delay = min(5., delay*2.)

    def snapshot(self): return self.cache.snapshot()

    def close(self):
        self.closed.set()
        self.thread.join(timeout=3.)
        self.cache.connection(False, '电量订阅已关闭。')
