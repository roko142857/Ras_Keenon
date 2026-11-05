"""Factory read-only view. No SDK init, mapping start or navigation goal is sent."""
from pathlib import Path
import sys
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction, RegisterEventHandler, EmitEvent
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration


def setup(context):
    v=lambda n:LaunchConfiguration(n).perform(context)
    nav=ExecuteProcess(cmd=[sys.executable,'-m','ras_keenon_navigation.server','--backend','native-readonly',
        '--rosbridge-host',v('rosbridge_host'),'--rosbridge-port',v('rosbridge_port'),
        '--port',v('port'),'--data-dir',str(Path(v('data_dir'))/'navigation')],output='screen')
    web=ExecuteProcess(cmd=[sys.executable,'-m','ras_keenon_chat.server','--host',v('bind'),'--port',v('web_port'),
        '--data-dir',str(Path(v('data_dir'))/'chat'),'--navigation-url','http://127.0.0.1:'+v('port'),
        '--navigation-only','--demo','--action-url','http://127.0.0.1:1','--controller-url','http://127.0.0.1:1'],output='screen')
    return [nav,web,*[RegisterEventHandler(OnProcessExit(target_action=p,on_exit=[EmitEvent(event=Shutdown(reason='read-only view exited'))])) for p in (nav,web)]]


def generate_launch_description():
    defaults=dict(rosbridge_host='192.168.64.20',rosbridge_port='9091',port='18780',web_port='18770',bind='0.0.0.0',
                  data_dir=str(Path.home()/'ras_keenon_ws/run/native_view'))
    return LaunchDescription([*[DeclareLaunchArgument(k,default_value=v) for k,v in defaults.items()],OpaqueFunction(function=setup)])
