"""Read an existing controller only. Never initialize the SDK or send commands."""
import argparse
import json
import math
import shutil
import subprocess
import time
import urllib.request


def inspect(url):
    report = dict(controller_url=url, checked_at=time.time(), findings=[])
    try:
        with urllib.request.urlopen(url.rstrip('/')+'/readings', timeout=3) as response:
            state = json.load(response)
    except (OSError, ValueError) as exc:
        report.update(available=False, error=str(exc))
        report['findings'].append('控制器不可访问；请先启动手动控制器或完整动作 launch。')
        return report
    report.update(available=True, readings=state)
    if state.get('backend') != 'hardware':
        report['findings'].append('当前为 mock，动作不会驱动真实机械臂。')
    if state.get('body_error'): report['findings'].append('关节控制错误：'+str(state['body_error']))
    body = state.get('body') or {}
    if not body: report['findings'].append('当前控制器未启动 body 组件。')
    for side in ('right', 'left'):
        q = body.get(side)
        if not isinstance(q, list) or len(q) != 7:
            report['findings'].append(side+' 缺少七轴读数；查看控制器 SDK 初始化日志。')
        else:
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in q):
                report['findings'].append(side+' 存在无效关节角度。')
            elif any(abs(v)>3.2 for v in q):
                report['findings'].append(side+' 读数超出动作接口的弧度范围；若原始值为度，需更新 SDK 桥接的 deg→rad 转换并重启控制器。')
            if state.get('backend')=='hardware' and body.get('arm_angle_unit')!='rad':
                report['findings'].append(side+' 缺少 arm_angle_unit=rad 标记；当前可能仍运行未修复单位的旧桥接程序。')
        status = body.get('arm_status', {}).get(side)
        if state.get('backend') == 'hardware' and status is None:
            report['findings'].append(side+' 尚无回调诊断字段；SDK 可能仍在启动，或需要编译并重启控制器。')
        if state.get('backend') == 'hardware' and status and not status.get('feedback_fresh'):
            report['findings'].append(side+' 存在未收到或超过 1 秒的关节反馈；缓存角度不能证明当前在线。')
    if state.get('body_command'):
        report['findings'].append('当前 SDK 正在执行 '+state['body_command']+'；阻塞期间 READ 快照不会刷新。')
    report['note'] = '只读取已有服务；反馈新鲜度是诊断信息，不作为使能条件。电机电源和故障码未独立查询。'
    return report


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-url', default='http://127.0.0.1:8765')
    parser.add_argument('--local-system', action='store_true', help='同时读取执行本命令主机上的 CAN 链路和进程')
    cfg = parser.parse_args(args)
    report = inspect(cfg.controller_url)
    if cfg.local_system:
        for name, command in [('can_links', ['ip','-details','-statistics','link','show','type','can']),
                              ('processes', ['ps','-eo','pid,stat,etimes,args'])]:
            if not shutil.which(command[0]): continue
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            output = result.stdout
            if name == 'processes':
                output = '\n'.join(line for line in output.splitlines() if any(
                    token in line for token in ('command_sdk_bridge','ras_keenon_commands/server','ras_keenon_demos','ras_keenon_chat')))
            report[name] = output
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['available'] else 2


if __name__ == '__main__': raise SystemExit(main())
