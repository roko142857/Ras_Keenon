"""Thin standalone action clients; all execution stays in the common action API."""
import argparse
import json
from .catalog import SPECS
from .transport import ActionClient


def run_fixed(action,args=None):
    parser=argparse.ArgumentParser(description=SPECS[action]['description'])
    parser.add_argument('--api-url',default='http://127.0.0.1:8766')
    parser.add_argument('--request-id',default=None)
    parser.add_argument('--timeout',type=float,default=240.)
    parser.add_argument('--dry-run',action='store_true',help='只读取并打印计划，不发送运动命令')
    for name,spec in SPECS[action]['parameters'].items():
        flag='--'+name.replace('_','-')
        kind=spec['type']
        if kind=='boolean':
            parser.add_argument(flag,default=spec['default'],action=argparse.BooleanOptionalAction,
                                help=spec['description'])
        else:
            parser.add_argument(flag,default=spec['default'],choices=spec.get('enum'),
                type=int if kind=='integer' else float if kind=='number' or isinstance(kind,list) else str,
                help=spec['description'])
    cfg=parser.parse_args(args)
    parameters={name:getattr(cfg,name) for name in SPECS[action]['parameters']}
    client=ActionClient(cfg.api_url)
    try:
        result=client.plan(action,parameters) if cfg.dry_run else client.run(action,parameters,cfg.request_id,cfg.timeout)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result.get('state') in ('failed','cancelled') or result.get('stop_error'):return 1
        return 0
    except KeyboardInterrupt:return 130
    except Exception as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False));return 1
