"""Local mock only. Never connects to robot services or starts hardware controllers."""
from pathlib import Path
import sys
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction, RegisterEventHandler, EmitEvent
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration


def setup(context):
    v=lambda name:LaunchConfiguration(name).perform(context)
    nav=ExecuteProcess(cmd=[sys.executable,'-m','ras_keenon_navigation.server',
            '--host','127.0.0.1','--port',v('port'),'--data-dir',str(Path(v('data_dir'))/'navigation')],output='screen')
    actions=[nav]
    if v('web').lower()=='true':
        web=ExecuteProcess(cmd=[sys.executable,'-m','ras_keenon_chat.server','--host','127.0.0.1','--port',v('web_port'),
            '--data-dir',str(Path(v('data_dir'))/'chat'),'--navigation-url','http://127.0.0.1:'+v('port'),
            '--navigation-only','--demo','--action-url','http://127.0.0.1:1','--controller-url','http://127.0.0.1:1'],output='screen')
        actions.append(web)
    for process in list(actions):
        actions.append(RegisterEventHandler(OnProcessExit(target_action=process,on_exit=[EmitEvent(event=Shutdown(reason='local exploration service exited'))])))
    return actions


def generate_launch_description():
    return LaunchDescription([DeclareLaunchArgument('port',default_value='18780'),DeclareLaunchArgument('web',default_value='false'),
        DeclareLaunchArgument('web_port',default_value='18770'),
        DeclareLaunchArgument('data_dir',default_value=str(Path.home()/'ras_keenon_ws/run/local_exploration')),OpaqueFunction(function=setup)])
