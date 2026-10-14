#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/opt/qq_bot}"
SERVICE="${QQ_BOT_SERVICE:-qq-bot.service}"
LLBOT_CONTAINER="${LLBOT_CONTAINER:-llbot}"
RESTART_CONNECTOR="${RESTART_CONNECTOR:-0}"
WAIT_SECONDS="${QQ_BOT_RESTART_WAIT_SECONDS:-10}"

echo "Restarting ${SERVICE}..."
sudo systemctl restart "$SERVICE"

if [ "$RESTART_CONNECTOR" = "1" ]; then
  echo "Restarting ${LLBOT_CONTAINER}..."
  sudo docker restart "$LLBOT_CONTAINER" >/dev/null
else
  echo "Keeping ${LLBOT_CONTAINER} running to preserve the QQ login session."
fi

echo "Waiting ${WAIT_SECONDS}s for reverse WS reconnect..."
sleep "$WAIT_SECONDS"

bash "${ROOT}/scripts/server/status.sh"
