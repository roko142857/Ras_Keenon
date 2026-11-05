"""Current manual control: continuous inputs with real key press/release events."""
from pathlib import Path
import sys
import json
import urllib.request
from urllib.parse import urlparse
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription, DeclareLaunchArgument, ExecuteProcess, OpaqueFunction, LogInfo
from launch.substitutions import LaunchConfiguration
from launch.launch_description_sources import PythonLaunchDescriptionSource


def mapping_process(context):
    value = lambda name: LaunchConfiguration(name).perform(context)
    if value('start_mapping').lower() not in ('true', '1', 'yes'): return []
    address = value('mapping_url').rstrip('/')
    parsed = urlparse(address)
    if parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1', 'localhost'):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(address+'/api/health', timeout=.5) as response: health = json.load(response)
            if health.get('service') == 'ras_keenon_mapping':
                if health.get('backend') == value('backend'):
                    return [LogInfo(msg='Reusing mapping service at '+address)]
                return [LogInfo(msg='Mapping service backend differs; manual control remains available.')]
        except Exception: pass
    return [ExecuteProcess(cmd=[sys.executable, '-m', 'ras_keenon_navigation.mapping_server',
        '--backend', value('backend'), '--port', value('mapping_port'),
        '--data-dir', value('mapping_data_dir')], output='screen')]



def rtab_process(context):
    if LaunchConfiguration('backend').perform(context) != 'hardware': return []
    root = Path.home()/'ras_keenon_ws'
    config = root/'rtab_panel/settings.json'
    if not config.is_file(): return [LogInfo(msg='RTAB extension is not installed; manual control remains available.')]
    settings = json.loads(config.read_text())
    address = 'http://127.0.0.1:'+str(settings['port'])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(address+'/rtab/health', timeout=.5) as response: health = json.load(response)
        if health.get('backend') == 'hardware' and 'phase' in health:
            return [LogInfo(msg='Reusing idle-capable RTAB service at '+address)]
    except (OSError, ValueError): pass
    return [ExecuteProcess(cmd=[settings['python'], '-m', 'rtab_panel.server', '--config', str(config)],
        cwd=str(root), additional_env={'PYTHONPATH':str(root)}, output='screen',
        sigterm_timeout='45', sigkill_timeout='10')]


def generate_launch_description():
    path = Path(get_package_share_directory('ras_keenon_commands'))/'launch/direct.launch.py'
    return LaunchDescription([
        DeclareLaunchArgument('backend', default_value='mock'),
        DeclareLaunchArgument('start_mapping', default_value='true'),
        DeclareLaunchArgument('mapping_port', default_value='18781'),
        DeclareLaunchArgument('mapping_url', default_value=['http://127.0.0.1:', LaunchConfiguration('mapping_port')]),
        DeclareLaunchArgument('mapping_data_dir', default_value=str(Path.home()/'ras_keenon_ws/run/mapping')),
        OpaqueFunction(function=mapping_process),
        OpaqueFunction(function=rtab_process),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(str(path)))])
