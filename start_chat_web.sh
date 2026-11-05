#!/usr/bin/env bash
# Local frontend/backend startup; no ROS installation or SDK initialization needed.
set -euo pipefail
keenon_chat_ws="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${keenon_chat_ws}/src/ras_keenon_chat:${keenon_chat_ws}/src/ras_keenon_demos${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m ras_keenon_chat.server --host 0.0.0.0 --port 8770 \
  --action-url http://192.168.3.20:8766 \
  --controller-url http://192.168.3.20:8765 "$@"
