#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/home/your-server-user/qq_bot}"
SERVICE="${QQ_BOT_SERVICE:-qq-bot.service}"
NAPCAT_CONTAINER="${NAPCAT_CONTAINER:-napcat}"

cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "== qq-bot journal =="
sudo journalctl -u "$SERVICE" -n 120 -f &

echo "== napcat docker logs =="
sudo docker logs -f --tail 120 "$NAPCAT_CONTAINER" &

wait
