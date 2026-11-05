"""Same-origin mapping proxy; no ROS dependency or motion transport."""
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse


class MappingProxy:
    def __init__(self, url, backend):
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('mapping_url 需为回环 HTTP 服务地址')
        self.url = url.rstrip('/'); self.backend = backend
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, data=None):
        parsed = urlparse(path); route = parsed.path
        routes = {'/mapping/state', '/mapping/maps', '/mapping/map.png', '/mapping/start', '/mapping/finish', '/mapping/cancel', '/mapping/export', '/mapping/refresh'}
        routes.update('/mapping/'+x for x in ('map_preview','map_export','map_switch','map_update'))
        if route not in routes and not route.startswith('/mapping/exports/'): raise ValueError('未知建图接口')
        if data is not None:
            with self.opener.open(self.url+'/api/health', timeout=2) as response: health = json.load(response)
            if health.get('backend') != self.backend: raise ValueError('建图与手动控制后端不一致，未提交请求')
        target = self.url+'/api/'+path[len('/mapping/'):]
        req = urllib.request.Request(target, None if data is None else json.dumps(data, allow_nan=False).encode(), {'Content-Type':'application/json'})
        try:
            with self.opener.open(req, timeout=3) as response:
                body = response.read(32_000_001)
                if len(body) > 32_000_000: raise ValueError('建图响应过大')
                return response.status, body, response.headers.get('Content-Type', 'application/json')
        except urllib.error.HTTPError as exc: return exc.code, exc.read(8192), 'application/json'
