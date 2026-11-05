"""Interface example only: publishes zero velocity, no detector or target-to-motion policy."""
import json
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy
from std_msgs.msg import String
from ras_keenon_interfaces.msg import BaseCommand


class VisionExample(Node):
    def __init__(self):
        super().__init__('vision_example')
        self.state = {}
        self.pub = self.create_publisher(BaseCommand, 'vision/cmd_vel', 1)
        self.create_subscription(String, 'state', self.update,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_timer(.1, self.tick)
        self.get_logger().info('Zero-command example. Select vision and enable explicitly. '
                               'Replace tick with a measured visual controller later.')

    def update(self, msg):
        self.state = json.loads(msg.data)

    def tick(self):
        if self.state.get('armed') and self.state.get('source') == 'vision':
            msg = BaseCommand()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.epoch = self.state['epoch']
            self.pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = VisionExample()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
