"""Regression for the actual all-fresh but degree-valued robot feedback."""
import io
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch
from ras_keenon_commands.arm_status import inspect


class ArmStatusTests(unittest.TestCase):
    def readings(self):
        return json.loads((Path(__file__).parent/'fixtures/sdk_degree_readings.json').read_text())

    def inspect(self, state):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(state).encode())):
            return inspect('http://unused.test')

    def test_fresh_degree_feedback_reports_unit_mismatch(self):
        report=self.inspect(self.readings())
        self.assertTrue(report['available'])
        self.assertTrue(any('deg→rad' in item for item in report['findings']))
        self.assertTrue(any('arm_angle_unit=rad' in item for item in report['findings']))

    def test_canonical_radian_feedback_has_no_findings(self):
        state=self.readings();body=state['body']
        for name in ('right','left','waist'):body[name]=list(map(math.radians,body[name]))
        body['arm_angle_unit']='rad';body['waist_angle_unit']='rad'
        self.assertEqual(self.inspect(state)['findings'],[])
