"""Serial SDK process transport; startup is acknowledged without a motion state gate."""
import json
import os
import select
import subprocess
import time


class DirectChannel:
    def __init__(self, executable, lock_fds=()):
        self.proc = subprocess.Popen([executable, '--hardware'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, start_new_session=True,
                                     pass_fds=tuple(lock_fds))
        self.pending, self.sequence = b'', 0
        try: self.receive(0, 30)
        except Exception:
            self.proc.stdin.close(); self.proc.terminate()
            raise

    def receive(self, identity, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            while b'\n' in self.pending:
                line, self.pending = self.pending.split(b'\n', 1)
                if line.startswith(b'@RAS '):
                    result = json.loads(line[5:])
                    if result.get('id') == identity:
                        if not result.get('ok'): raise RuntimeError(result.get('error', 'SDK request failed'))
                        return result
            if self.proc.poll() is not None: raise RuntimeError('SDK process exited')
            if select.select([self.proc.stdout], [], [], .05)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 8192)
                if not chunk: raise RuntimeError('SDK pipe closed')
                self.pending += chunk
                if len(self.pending) > 65536: raise RuntimeError('SDK reply too large')
        raise TimeoutError('SDK result unknown; no automatic retry')

    def request(self, op, values=()):
        self.sequence += 1
        line = ' '.join([str(self.sequence), op, *map(str, values)])+'\n'
        self.proc.stdin.write(line.encode()); self.proc.stdin.flush()
        # Arm trajectories are blocking and can legitimately take longer than 30 s.
        return self.receive(self.sequence, 180 if op == 'ARM' else 30)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b'999999 QUIT\n'); self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()
                # The SDK process keeps its inherited device lock until it exits.
                self.proc.stdin.close(); self.proc.stdout.close()
                return False
        self.proc.stdin.close(); self.proc.stdout.close()
        return True
