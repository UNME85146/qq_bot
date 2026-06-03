from __future__ import annotations

import os

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from app.config import load_config


load_dotenv()
_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))

nonebot.init(
    driver="~fastapi",
    host=_config.onebot.host,
    port=_config.onebot.port,
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)
nonebot.load_plugin("app.plugins.private_chat")
nonebot.load_plugin("app.plugins.group_chat")
nonebot.load_plugin("app.plugins.group_reactions")
nonebot.load_plugin("app.plugins.owner_commands")


if __name__ == "__main__":
    nonebot.run()
