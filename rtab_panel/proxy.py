"""Loopback RTAB API proxy; no motion messages or control state."""
import json
import urllib.error
import urllib.request
from pathlib import Path


class RtabProxy:
    def __init__(self, backend):
        settings = json.loads(Path(__file__).with_name('rtab_settings.json').read_text())
        self.url = 'http://127.0.0.1:'+str(settings['port'])
        self.backend = backend
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, data=None):
        if path not in ('/rtab/state', '/rtab/cloud', '/rtab/start', '/rtab/stop'):
            raise ValueError('未知 RTAB 接口')
        if data is not None and self.backend != 'hardware':
            raise ValueError('当前控制器不是硬件模式，未启动真实相机')
        req = urllib.request.Request(self.url+path,
                                     None if data is None else json.dumps(data).encode(),
                                     {'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=3) as response:
                return response.status, response.read(8_000_000), 'application/json'
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(8192), 'application/json'
