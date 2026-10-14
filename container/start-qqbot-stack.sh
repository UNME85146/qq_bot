#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  pmhq)
    test -r /app/llbot/data/auth_token.txt
    export PMHQ_AUTH_TOKEN="$(cat /app/llbot/data/auth_token.txt)"
    export AUTO_LOGIN_QQ="${QQ_ACCOUNT_ID:-}"
    exec /startup.sh
    ;;
  llbot)
    test -n "${QQ_ACCOUNT_ID:-}"
    cd /app/llbot
    exec /usr/local/bin/node --enable-source-maps ./llbot.js \
      --pmhq-port=13000 --pmhq-host=127.0.0.1 -q "${QQ_ACCOUNT_ID}"
    ;;
  bot)
    cd /app/qq_bot
    export QQ_BOT_CONFIG_PATH="${QQ_BOT_CONFIG_PATH:-/app/qq_bot/config/config.json}"
    export QQ_BOT_ENV_PATH="${QQ_BOT_ENV_PATH:-/app/qq_bot/.env}"
    export QQ_BOT_CONTROL_TOKEN_FILE="${QQ_BOT_CONTROL_TOKEN_FILE:-/app/qq_bot/runtime_artifacts/secrets/bot-control.token}"
    bot_user="$(getent passwd 1000 | cut -d: -f1)"
    test -n "$bot_user"
    exec runuser -u "$bot_user" -- /opt/qqbot-venv/bin/python -B -m app.ops.container_bot
    ;;
  *)
    echo "unknown qqbot stack component" >&2
    exit 64
    ;;
esac
