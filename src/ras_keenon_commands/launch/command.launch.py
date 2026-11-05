from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, RegisterEventHandler, EmitEvent
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def setup(context):
    from ras_keenon_commands.instructions import parse
    command = LaunchConfiguration('command').perform(context).strip()
    instruction = parse(command) if command else None  # Validate before initializing SDK.
    backend = LaunchConfiguration('backend').perform(context)
    if backend not in ('mock', 'hardware'): raise ValueError('backend must be mock or hardware')
    components = LaunchConfiguration('components').perform(context)
    if components == 'auto': components = ('base' if instruction.op == 'base' else 'body') if instruction else 'all'
    if components not in ('body', 'base', 'all'): raise ValueError('components must be auto/body/base/all')
    own = LaunchConfiguration('start_controller').perform(context).lower() == 'true'
    keep = LaunchConfiguration('keep_alive').perform(context).lower() == 'true'
    namespace = LaunchConfiguration('namespace').perform(context)
    server = Node(package='ras_keenon_commands', executable='server', namespace=namespace,
                  output='screen', parameters=[dict(backend=backend, components=components,
                    timeout=float(LaunchConfiguration('timeout').perform(context)))])
    client = Node(package='ras_keenon_commands', executable='terminal', namespace=namespace,
                  output='screen', parameters=[{'command': command,
                    'request_timeout': float(LaunchConfiguration('timeout').perform(context))+30.}])
    actions = [server] if own else []
    actions.append(client)
    if own and not keep:
        actions.append(RegisterEventHandler(OnProcessExit(target_action=client,
            on_exit=[EmitEvent(event=Shutdown(reason='command terminal finished'))])))
    if own:
        actions.append(RegisterEventHandler(OnProcessExit(target_action=server,
            on_exit=[EmitEvent(event=Shutdown(reason='command server exited'))])))
    return actions


def generate_launch_description():
    defaults = dict(backend='mock', command='', components='auto', start_controller='true',
                    keep_alive='false', namespace='ras_keenon_commands', timeout='185.0')
    return LaunchDescription([*[DeclareLaunchArgument(k, default_value=v) for k, v in defaults.items()],
                              OpaqueFunction(function=setup)])
