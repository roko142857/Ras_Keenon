"""Compile the bridge against a fake SDK only: no vendor archive, CAN or robot connection."""
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import unittest

HEADER = r'''
#pragma once
#include <atomic>
#include <cmath>
#include <iostream>
#include <vector>
void robot_init(int); void robot_end(); void stopRightArm();
double getJointAngle(int); void setJointAngle(int,double);
std::vector<double> getJointAngle_r(); std::vector<double> getJointAngle_l();
std::vector<double> getJointAngle_w(); int moveJ_w(const std::vector<double>&);
int moveJ_r_block(const std::vector<double>&,double);
int moveJ_l_block(const std::vector<double>&,double);
'''
FAKE = r'''
#include "qlrobotarm_canfd.h"
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <thread>
typedef struct { int type; int command; int32_t data; uint32_t id;
 int16_t current; float speed; float position; float position_de; } hal_data_recv_t;
using Callback=void(*)(hal_data_recv_t*,int);
Callback receive_callback=nullptr;
int16_t hal_receive(Callback);
std::atomic<bool> need_query_h{false},running{false};
std::atomic<double> yaw{-12.},pitch{3.};
std::thread producer;
std::vector<double> left(7,0.),right(7,0.),waist(6,0.);
void original(hal_data_recv_t*,int){}
void robot_init(int){
 if(std::getenv("RAS_FAKE_REALISTIC_POSE")){
  right={2.,85.,176.,-1.,90.,-3.,0.};left={-3.,-83.,-11.,3.,101.,2.5,17.};
  waist={0.,-16.,-32.,-16.,0.,0.};
  for(double& v:right)v*=std::acos(-1.)/180.;
  for(double& v:left)v*=std::acos(-1.)/180.;
  for(double& v:waist)v*=std::acos(-1.)/180.;
 }
 hal_receive(original);running=true;
 producer=std::thread([]{
  auto start=std::chrono::steady_clock::now();
  while(running){
   double age=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
   const char* quiet=std::getenv("RAS_FAKE_QUIET_AFTER_S");
   if(need_query_h && (!quiet || age<std::atof(quiet))){
    hal_data_recv_t records[5]{};records[0].type=1;records[0].id=30;
    receive_callback(records,0);records[0].id=31;receive_callback(records,0);
    if(!std::getenv("RAS_FAKE_NO_ARMS"))for(int id=16;id<=29;++id){
      records[0].id=id;receive_callback(records,0);
    }
   }
   std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
 });
}
void robot_end(){running=false;if(producer.joinable())producer.join();}
void stopRightArm(){}
double getJointAngle(int id){return id==30?yaw.load():pitch.load();}
void setJointAngle(int id,double v){if(id==30)yaw=v;else pitch=v;}
// Emulate the real SDK's mixed-unit boundary, not a symmetric fake API.
std::vector<double> degrees(std::vector<double> q){for(double& v:q)v*=180./std::acos(-1.);return q;}
std::vector<double> getJointAngle_r(){return degrees(right);}
std::vector<double> getJointAngle_l(){return degrees(left);}
std::vector<double> getJointAngle_w(){return degrees(waist);}
int moveJ_w(const std::vector<double>& q){waist=q;return 0;}
int moveJ_r_block(const std::vector<double>& q,double){
 if(std::getenv("RAS_FAKE_SDK_FAIL"))return 0;right=q;return 1;}
int moveJ_l_block(const std::vector<double>& q,double){left=q;return 1;}
'''


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        (root/'qlrobotarm_canfd.h').write_text(HEADER)
        (root/'fake.cpp').write_text(FAKE)
        # ld --wrap applies to undefined references across objects, like the real archive.
        (root/'receive.cpp').write_text(r'''
#include <cstdint>
typedef struct { int type; int command; int32_t data; uint32_t id;
 int16_t current; float speed; float position; float position_de; } hal_data_recv_t;
using Callback=void(*)(hal_data_recv_t*,int);
extern Callback receive_callback;
int16_t hal_receive(Callback cb){receive_callback=cb;return 0;}
''')
        source = getattr(cls, 'SOURCE', Path(__file__).resolve().parents[1]/'src/ras_keenon_native/src/sdk_bridge.cpp')
        cls.binary = root/'fake_sdk_bridge'
        subprocess.run(['g++', '-std=c++14', '-pthread', '-I'+str(root), str(source),
                        str(root/'fake.cpp'), str(root/'receive.cpp'),
                        '-Wl,--wrap=_Z11hal_receivePFvP15hal_data_recv_tiE',
                        '-o', str(cls.binary)], check=True, timeout=30, capture_output=True)

    @classmethod
    def tearDownClass(cls): cls.directory.cleanup()

    def start(self, **flags):
        env = os.environ.copy();env.update(flags)
        self.process = subprocess.Popen([str(self.binary), '--hardware'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.identity = 0
        self.assertTrue(self.response()['ok'])

    def response(self):
        self.assertTrue(select.select([self.process.stdout], [], [], 5)[0], 'no fake SDK reply')
        line = self.process.stdout.readline()
        self.assertTrue(line.startswith(b'@RAS '), line)
        return json.loads(line[5:])

    def request(self, op):
        self.identity += 1
        self.process.stdin.write(f'{self.identity} {op}\n'.encode());self.process.stdin.flush()
        return self.response()

    def tearDown(self):
        if hasattr(self, 'process'):
            if self.process.poll() is None:self.request('QUIT')
            self.process.wait(timeout=5)
            for f in (self.process.stdin, self.process.stdout, self.process.stderr):f.close()

    def test_fresh_head_callback_and_small_relative_step(self):
        self.start()
        s = self.request('READ')
        self.assertTrue(s['head_feedback_fresh'])
        self.assertGreater(s['head_yaw_sequence'], 0)
        self.assertEqual(s['yaw_deg'], -12.)
        self.assertTrue(self.request('HEAD -11.5 3')['ok'])
        self.assertEqual(self.request('READ')['yaw_deg'], -11.5)

    def test_refused_step_does_not_terminate_owner(self):
        self.start()
        r = self.request('HEAD -4 3')
        self.assertFalse(r['ok']);self.assertTrue(r['refused'])
        self.assertTrue(self.request('READ')['ok'])
        self.assertEqual(self.request('READ')['yaw_deg'], -12.)

    def test_stale_callback_refuses_head_motion(self):
        self.start(RAS_FAKE_QUIET_AFTER_S='.55')
        time.sleep(.4)
        r = self.request('HEAD -11.5 3')
        self.assertFalse(r['ok']);self.assertTrue(r['refused'])
        self.assertFalse(self.request('READ')['head_feedback_fresh'])

    def test_sdk_failure_is_not_downgraded_to_validation_refusal(self):
        self.start(RAS_FAKE_SDK_FAIL='1')
        r = self.request('ARM right 0 0 0 0.02 0 0 0 0.1')
        self.assertFalse(r['ok']);self.assertFalse(r['refused'])

    def test_vendor_degree_feedback_is_exposed_as_radians(self):
        self.start(RAS_FAKE_REALISTIC_POSE='1')
        state=self.request('READ')
        self.assertEqual(state['arm_angle_unit'],'rad')
        self.assertEqual(state['sdk_feedback_angle_unit'],'deg')
        self.assertAlmostEqual(state['right'][2],__import__('math').radians(176))
        self.assertAlmostEqual(state['left'][4],__import__('math').radians(101))
        self.assertAlmostEqual(state['waist'][2],__import__('math').radians(-32))


if __name__ == '__main__': unittest.main()
