"""Run the chat console alone, or include the existing action/controller stack."""
from pathlib import Path
import sys
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument,ExecuteProcess,IncludeLaunchDescription,OpaqueFunction,RegisterEventHandler,EmitEvent
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def setup(context):
    v=lambda name:LaunchConfiguration(name).perform(context)
    demo=v('demo').lower()=='true'
    actions=[]
    action_url=v('action_url')
    controller_url=v('controller_url') or 'http://127.0.0.1:'+v('controller_port')
    if v('start_actions').lower()=='true' and not demo:
        path=Path(get_package_share_directory('ras_keenon_demos'))/'launch/demos.launch.py'
        actions.append(IncludeLaunchDescription(PythonLaunchDescriptionSource(str(path)),launch_arguments={
            'backend':v('backend'),'start_controller':v('start_controller'),
            'controller_port':v('controller_port'),'action_port':v('action_port'),
            'controller_url':controller_url}.items()))
        if not action_url:action_url='http://127.0.0.1:'+v('action_port')
    command=[sys.executable,'-m','ras_keenon_chat.server','--host',v('bind'),'--port',v('port'),'--data-dir',v('data_dir')]
    command+=['--controller-url',controller_url]
    if action_url:command+=['--action-url',action_url]
    if v('navigation_url'):command+=['--navigation-url',v('navigation_url')]
    if v('navigation_only').lower()=='true':command+=['--navigation-only']
    if demo:command+=['--demo']
    server=ExecuteProcess(cmd=command,output='screen')
    actions.extend([server,RegisterEventHandler(OnProcessExit(target_action=server,
        on_exit=[EmitEvent(event=Shutdown(reason='chat console exited'))]))])
    return actions


def generate_launch_description():
    defaults=dict(bind='0.0.0.0',port='8770',demo='false',start_actions='false',
                  start_controller='true',backend='mock',controller_port='8765',action_port='8766',
                  action_url='',controller_url='',navigation_url='',navigation_only='false',data_dir=str(Path.home()/'ras_keenon_ws/run/chat'))
    return LaunchDescription([*[DeclareLaunchArgument(k,default_value=v) for k,v in defaults.items()],OpaqueFunction(function=setup)])
