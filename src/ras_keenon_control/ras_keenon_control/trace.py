"""Bounded asynchronous diagnostics, outside the motion/stop loop."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import threading
import time


class Trace:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.queue = queue.Queue(maxsize=512)
        self.dropped = 0
        self.done = threading.Event()
        self.logger = logging.Logger('ras_trace')
        self.handler = RotatingFileHandler(path, maxBytes=2*1024*1024, backupCount=3)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(self.handler)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def record(self, kind, **fields):
        row = dict(kind=kind, monotonic_s=time.monotonic(), wall_time_s=time.time(), **fields)
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def run(self):
        while not self.done.is_set() or not self.queue.empty():
            try:
                self.logger.info(json.dumps(self.queue.get(timeout=.1), allow_nan=False, default=str))
            except queue.Empty:
                continue
            except Exception:
                self.dropped += 1

    def close(self):
        self.done.set()
        self.thread.join(timeout=2)
        if not self.thread.is_alive():
            self.handler.close()
