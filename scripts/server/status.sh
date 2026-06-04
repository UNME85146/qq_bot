#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/opt/qq_bot}"
SERVICE="${QQ_BOT_SERVICE:-qq-bot.service}"
TTS_SERVICE="${QQ_BOT_TTS_SERVICE:-qq-bot-tts.service}"
NAPCAT_CONTAINER="${NAPCAT_CONTAINER:-napcat}"

echo "== service =="
if command -v systemctl >/dev/null 2>&1; then
  systemctl --no-pager --plain --full status "$SERVICE" | sed -n '1,12p' || true
else
  echo "systemctl not found"
fi

echo
echo "== tts service =="
if command -v systemctl >/dev/null 2>&1; then
  systemctl --no-pager --plain --full status "$TTS_SERVICE" | sed -n '1,10p' || true
else
  echo "systemctl not found"
fi

echo
echo "== napcat container =="
if command -v docker >/dev/null 2>&1; then
  if docker ps >/dev/null 2>&1; then
    docker ps --filter "name=^/${NAPCAT_CONTAINER}$" --format 'name={{.Names}} status={{.Status}} ports={{.Ports}}' || true
  else
    sudo docker ps --filter "name=^/${NAPCAT_CONTAINER}$" --format 'name={{.Names}} status={{.Status}} ports={{.Ports}}' || true
  fi
else
  echo "docker not found"
fi

echo
echo "== ports =="
if command -v ss >/dev/null 2>&1; then
  ss -ltnp '( sport = :8080 or sport = :6099 or sport = :18100 )' || true
  echo
  echo "== onebot established =="
  ss -tan | awk '$1 ~ /ESTAB/ && $4 ~ /:8080$/ {print}' || true
else
  echo "ss not found"
fi

echo
echo "== runtime status =="
cd "$ROOT"
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python tools/inspect_runtime_status.py --limit 3
else
  python3 tools/inspect_runtime_status.py --limit 3
fi
