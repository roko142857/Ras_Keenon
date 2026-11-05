#!/usr/bin/env python3
"""Read provider-neutral tool calls as JSON lines and forward them to the robot API.

Input: {"name":"robot_shake_hand","arguments":{"cycles":2},"call_id":"dialogue-1"}
Output is a job acknowledgement; use ActionClient.wait(id) for a final result.
"""
import argparse
import json
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
if (root/'ras_keenon_demos').is_dir():sys.path.insert(0,str(root))
from ras_keenon_demos import ActionClient,tool_schemas


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url',default='http://127.0.0.1:8766')
    parser.add_argument('--print-tools',action='store_true')
    args=parser.parse_args()
    if args.print_tools:
        print(json.dumps(tool_schemas(),ensure_ascii=False,indent=2));return
    client=ActionClient(args.api_url)
    for line in sys.stdin:
        if not line.strip():continue
        try:
            call=json.loads(line)
            result=client.call_tool(call['name'],call.get('arguments',{}),call.get('call_id'))
            print(json.dumps({'call_id':call.get('call_id'),'result':result},ensure_ascii=False),flush=True)
        except Exception as exc:
            print(json.dumps({'error':str(exc)},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
