#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/home/maintain/qq_bot}"
SERVICE="${QQ_BOT_SERVICE:-qq-bot.service}"
NAPCAT_CONTAINER="${NAPCAT_CONTAINER:-napcat}"
WAIT_SECONDS="${QQ_BOT_RESTART_WAIT_SECONDS:-10}"

echo "Restarting ${SERVICE}..."
sudo systemctl restart "$SERVICE"

echo "Restarting ${NAPCAT_CONTAINER}..."
sudo docker restart "$NAPCAT_CONTAINER" >/dev/null

echo "Waiting ${WAIT_SECONDS}s for reverse WS reconnect..."
sleep "$WAIT_SECONDS"

bash "${ROOT}/scripts/server/status.sh"
