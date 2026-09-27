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
    root = Path(os.environ.get("QQ_BOT_NAPCAT_ROOT", "/opt/qq_bot/napcat"))
    backend = SystemDashboardBackend(
        qr_path=root / "cache" / "qrcode.png",
        napcat_config_dir=root / "config",
        bot_port=int(os.environ.get("QQ_BOT_DASHBOARD_BOT_PORT", "8081")),
        onebot_token_path=Path(os.environ["QQ_BOT_DASHBOARD_ONEBOT_TOKEN_FILE"]),
        onebot_http_port=int(os.environ.get("QQ_BOT_DASHBOARD_ONEBOT_HTTP_PORT", "3000")),
        webui_token_path=Path(os.environ["QQ_BOT_DASHBOARD_WEBUI_TOKEN_FILE"]),
    )
    return create_dashboard_app(origin=origin, password_hash=read_password_hash(password_path), backend=backend)


def main() -> None:
    app = build_app()
    uvicorn.run(app, host="127.0.0.1", port=3011, workers=1,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
