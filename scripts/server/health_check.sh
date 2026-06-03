#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/opt/qq_bot}"

cd "$ROOT"
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python tools/health_check.py
else
  python3 tools/health_check.py
fi
