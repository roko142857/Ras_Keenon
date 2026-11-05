"""Start the existing direct controller and the standalone action API."""
from pathlib import Path
import sys
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument,IncludeLaunchDescription,ExecuteProcess,RegisterEventHandler,EmitEvent,OpaqueFunction
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def setup(context):
    value=lambda name:LaunchConfiguration(name).perform(context)
    own=value('start_controller').lower()=='true'
    controller_port=value('controller_port')
    url=value('controller_url') or 'http://127.0.0.1:'+controller_port
    api=ExecuteProcess(cmd=[sys.executable,'-m','ras_keenon_demos.api',
        '--controller-url',url,'--host',value('bind'),'--port',value('action_port')],output='screen')
    actions=[]
    if own:
        path=Path(get_package_share_directory('ras_keenon_commands'))/'launch/direct.launch.py'
        actions.append(IncludeLaunchDescription(PythonLaunchDescriptionSource(str(path)),launch_arguments={
            'backend':value('backend'),'components':value('components'),'port':controller_port,
            'namespace':value('namespace'),'bind':value('bind')}.items()))
    actions.extend([api,RegisterEventHandler(OnProcessExit(target_action=api,
        on_exit=[EmitEvent(event=Shutdown(reason='action API exited'))]))])
    return actions


def generate_launch_description():
    defaults=dict(backend='mock',components='all',start_controller='true',controller_url='',
                  controller_port='8765',action_port='8766',bind='0.0.0.0',namespace='ras_keenon_commands')
    return LaunchDescription([*[DeclareLaunchArgument(k,default_value=v) for k,v in defaults.items()],
                              OpaqueFunction(function=setup)])
