#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/opt/qq_bot}"
SERVICE="${QQ_BOT_SERVICE:-qq-bot.service}"
LLBOT_CONTAINER="${LLBOT_CONTAINER:-llbot}"

cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "== qq-bot journal =="
sudo journalctl -u "$SERVICE" -n 120 -f &

echo "== llbot docker logs =="
sudo docker logs -f --tail 120 "$LLBOT_CONTAINER" &

wait
