"""Bounded rosbridge transport, with no reconnect or command replay."""
import base64
import hashlib
import json
import os
import socket
import struct

HOST = '192.168.64.20'
PORT = 9091
MAX_FRAME = 1_000_000


class Rosbridge:
    def __init__(self, host=HOST, port=PORT):
        self.sock = socket.create_connection((host, port), timeout=2)
        self.pending = bytearray()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (f'GET / HTTP/1.1\r\nHost: {host}:{port}\r\n'
                       'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                       f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
            self.sock.sendall(request.encode())
            header = bytearray()
            while b'\r\n\r\n' not in header:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise EOFError('closed during upgrade')
                header.extend(chunk)
                if len(header) > 16384:
                    raise ValueError('oversized upgrade')
            raw, rest = bytes(header).split(b'\r\n\r\n', 1)
            lines = raw.decode().split('\r\n')
            fields = {k.lower(): v.strip() for k, v in
                      (s.split(':', 1) for s in lines[1:])}
            accept = base64.b64encode(hashlib.sha1(
                (key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if ' 101 ' not in lines[0] or fields.get('sec-websocket-accept') != accept:
                raise ValueError('invalid WebSocket upgrade')
            self.pending.extend(rest)
            self.sock.settimeout(.02)
        except BaseException:
            self.sock.close()
            raise

    def send_frame(self, data, opcode=1):
        if len(data) > 65535:
            raise ValueError('outgoing frame too large')
        mask = os.urandom(4)
        prefix = (bytes([128 | opcode, 128 | len(data)]) if len(data) < 126 else
                  bytes([128 | opcode, 254])+struct.pack('!H', len(data)))
        self.sock.sendall(prefix+mask+bytes(v ^ mask[i % 4] for i, v in enumerate(data)))

    def send(self, message):
        self.send_frame(json.dumps(message, allow_nan=False).encode())

    def receive(self):
        while len(self.pending) >= 2:
            a, b = self.pending[:2]
            if not a & 128 or a & 112 or b & 128:
                raise ValueError('fragmented, reserved or masked server frame')
            size, pos = b & 127, 2
            if size == 126:
                if len(self.pending) < 4:
                    break
                size, pos = struct.unpack('!H', self.pending[2:4])[0], 4
            elif size == 127:
                if len(self.pending) < 10:
                    break
                size, pos = struct.unpack('!Q', self.pending[2:10])[0], 10
            if size > MAX_FRAME:
                raise ValueError('incoming frame too large')
            if len(self.pending) < pos+size:
                break
            payload = bytes(self.pending[pos:pos+size])
            del self.pending[:pos+size]
            opcode = a & 15
            if opcode == 8:
                raise EOFError('WebSocket closed')
            if opcode == 9:
                self.send_frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode != 1:
                raise ValueError('non-text rosbridge frame')
            try:
                value = json.loads(payload)
            except (RecursionError, OverflowError) as exc:
                raise ValueError('invalid nested or numeric JSON') from exc
            if not isinstance(value, dict):
                raise ValueError('non-object rosbridge message')
            return value
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return None
        if not chunk:
            raise EOFError('peer disconnected')
        self.pending.extend(chunk)
        if len(self.pending) > MAX_FRAME+14:
            raise ValueError('incoming buffer too large')
        return None

    def close(self):
        try:
            self.send_frame(b'', 8)
        except OSError:
            pass
        self.sock.close()
