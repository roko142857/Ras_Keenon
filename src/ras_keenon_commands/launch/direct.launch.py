"""True press/release control through the local browser panel."""
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, RegisterEventHandler, EmitEvent
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


MOTION_TYPES = dict(cycle_ms=float, base_period_ms=float, body_period_ms=float,
                    speed=float, turn_speed=float, head_rate=float, squat_height=float,
                    key_timeout=float, base_timeout=float)


def setup(context):
    config = Path(LaunchConfiguration('config').perform(context)).expanduser()
    if not config.is_file():
        raise ValueError(f'manual control config not found: {config}')
    params = {k: LaunchConfiguration(k).perform(context) for k in ('backend', 'components', 'bind', 'mapping_url')}
    params.update(web=True, port=int(LaunchConfiguration('port').perform(context)))
    # Only explicit CLI values override the file; empty launch defaults preserve it.
    for name, value_type in MOTION_TYPES.items():
        value = LaunchConfiguration(name).perform(context).strip()
        if value:
            params[name] = value_type(value)
    node = Node(package='ras_keenon_commands', executable='server',
                namespace=LaunchConfiguration('namespace'),
                parameters=[str(config), params], output='screen')
    return [node, RegisterEventHandler(OnProcessExit(target_action=node,
            on_exit=[EmitEvent(event=Shutdown(reason='direct controller exited'))]))]


def generate_launch_description():
    config = Path(get_package_share_directory('ras_keenon_commands'))/'config/manual.yaml'
    defaults = dict(backend='mock', components='all', namespace='ras_keenon_commands',
                    bind='0.0.0.0', port='8765', config=str(config), mapping_url='http://127.0.0.1:18781')
    return LaunchDescription([*[DeclareLaunchArgument(k, default_value=v) for k,v in defaults.items()],
        *[DeclareLaunchArgument(k, default_value='', description='Override manual.yaml '+k)
          for k in MOTION_TYPES], OpaqueFunction(function=setup)])
