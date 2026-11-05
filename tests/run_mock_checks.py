"""Current direct-control checks. Source the built ROS environment before running."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
output = root/'verification'
output.mkdir(exist_ok=True)
env = os.environ.copy()
env.update(ROS_DOMAIN_ID='167', ROS_LOCALHOST_ONLY='1')
env['PYTHONPATH'] = os.pathsep.join([str(root/'src/ras_keenon_commands'),
    str(root/'src/ras_keenon_control'), env.get('PYTHONPATH', '')])
checks = {}
for name, args in (
    ('direct_unit_tests', ['-m','unittest','discover','-s','tests','-p','test_*.py','-v']),
    ('direct_mock_integration', [str(root/'tests/direct_mock_integration.py')]),
    ('manual_config_mock_integration', [str(root/'tests/manual_config_mock_integration.py')]),
):
    completed = subprocess.run([sys.executable, *args], cwd=root, env=env,
                               capture_output=True, text=True, timeout=120)
    (output/(name+'.log')).write_text(completed.stdout+completed.stderr)
    checks[name] = completed.returncode
    print(name, completed.returncode, completed.stdout[-1500:], completed.stderr[-1500:], flush=True)
report = dict(checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
              controller='direct', backend='mock', hardware_motion_tested=False, checks=checks)
(output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
raise SystemExit(int(any(checks.values())))
