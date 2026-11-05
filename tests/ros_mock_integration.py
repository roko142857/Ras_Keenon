"""Run only against a mock controller. No SDK process or robot network connection."""
import json
import time
import rclpy
from rclpy.qos import QoSProfile, DurabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger
from ras_keenon_interfaces.msg import HeadCommand, WaistCommand, ArmCommand, BaseCommand
from ras_keenon_interfaces.srv import SelectSource

rclpy.init()
node = rclpy.create_node('ras_integration_test')
state = {}
node.create_subscription(String, '/ras_keenon/state', lambda msg: state.update(json.loads(msg.data)),
    QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))


def wait(check, seconds=6):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        rclpy.spin_once(node, timeout_sec=.03)
        if check(): return
    raise AssertionError('condition timed out: '+str(state))


def service(typ, name, req):
    client = node.create_client(typ, '/ras_keenon/'+name)
    assert client.wait_for_service(timeout_sec=5)
    future = client.call_async(req)
    wait(future.done)
    result = future.result()
    assert result.success, result
    return result


def send(typ, topic, **values):
    pub = node.create_publisher(typ, '/ras_keenon/'+topic, 1)
    wait(lambda: pub.get_subscription_count() > 0)
    msg = typ(**values)
    msg.header.stamp = node.get_clock().now().to_msg()
    msg.epoch = state['epoch']
    pub.publish(msg)
    return pub


try:
    wait(lambda: state.get('body', {}).get('ready'))
    assert state['backend'] == 'mock' and not state['armed']
    service(SetBool, 'enable', SetBool.Request(data=True)); wait(lambda: state.get('armed'))
    send(HeadCommand, 'manual/head', relative=True, yaw_deg=.5, pitch_deg=0.)
    wait(lambda: state['body']['yaw_deg'] == -3.5)
    send(WaistCommand, 'manual/waist', height_m=.03)
    wait(lambda: state['body']['waist_height_m'] == .03)
    send(WaistCommand, 'manual/waist', height_m=0.)
    wait(lambda: state['body']['waist_height_m'] == 0.)
    send(ArmCommand, 'manual/arm', side='right', relative=True, joints_rad=[0.,0.,0.,.02,0.,0.,0.], speed_factor=.1)
    wait(lambda: state['body']['right'][3] == .02)
    pub = send(BaseCommand, 'manual/cmd_vel')
    msg = BaseCommand(); msg.header.stamp = node.get_clock().now().to_msg(); msg.epoch=state['epoch']; msg.twist.linear.x=.02
    pub.publish(msg); wait(lambda: state['base_output'][0] == .02)
    wait(lambda: state['base_output'] == [0.,0.])
    before = state['epoch']
    service(SelectSource, 'select_source', SelectSource.Request(source='vision'))
    wait(lambda: state['source'] == 'vision' and not state['armed'] and state['epoch'] > before)
    service(SetBool, 'enable', SetBool.Request(data=True)); wait(lambda: state.get('armed'))
    send(HeadCommand, 'manual/head', relative=True, yaw_deg=-.5, pitch_deg=0.)
    wait(lambda: 'inactive source' in state.get('error',''))
    send(HeadCommand, 'vision/head', relative=True, yaw_deg=-.5, pitch_deg=0.)
    wait(lambda: state['body']['yaw_deg'] == -4.)
    service(Trigger, 'stop', Trigger.Request()); wait(lambda: not state['armed'])
    print('PASS: mock head/waist/arm, base timeout, source handover, rejected inactive source and stop')
finally:
    node.destroy_node();rclpy.shutdown()
