"""Serve the authenticated QQ Bot operator dashboard on loopback only."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ops.web_dashboard import SystemDashboardBackend, create_dashboard_app, read_password_hash


def build_app():
    origin = os.environ["QQ_BOT_DASHBOARD_ORIGIN"]
    password_path = Path(os.environ["QQ_BOT_DASHBOARD_PASSWORD_HASH_FILE"])
    bot_root = Path(os.environ.get("QQ_BOT_ROOT", str(Path.cwd())))
    default_qr_path = Path("/run/qqbot-dashboard/login-qrcode.png")
    backend = SystemDashboardBackend(
        qr_path=Path(os.environ.get("QQ_BOT_DASHBOARD_QR_PATH", str(default_qr_path))),
        bot_port=int(os.environ.get("QQ_BOT_DASHBOARD_BOT_PORT", "8081")),
        onebot_token_path=Path(os.environ["QQ_BOT_DASHBOARD_ONEBOT_TOKEN_FILE"]),
        onebot_http_port=int(os.environ.get("QQ_BOT_DASHBOARD_ONEBOT_HTTP_PORT", "3000")),
        llbot_webui_port=int(os.environ.get("QQ_BOT_DASHBOARD_LLBOT_WEBUI_PORT", "3080")),
        webui_token_path=Path(os.environ["QQ_BOT_DASHBOARD_LLBOT_TOKEN_FILE"]),
        config_path=Path(os.environ.get("QQ_BOT_CONFIG_PATH", str(bot_root / "config" / "config.json"))),
        env_path=Path(os.environ.get("QQ_BOT_ENV_PATH", str(bot_root / ".env"))),
        service_name="qq-bot.service",
    )
    return create_dashboard_app(origin=origin, password_hash=read_password_hash(password_path), backend=backend)


def main() -> None:
    app = build_app()
    uvicorn.run(app, host="127.0.0.1", port=3011, workers=1,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
