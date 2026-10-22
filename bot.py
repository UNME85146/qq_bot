from __future__ import annotations

import os

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from app.config import load_config
from app.model.llm_client import (
    start_model_endpoint_probe_worker,
    stop_model_endpoint_probe_workers,
)


load_dotenv()
_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))

nonebot.init(
    driver="~fastapi",
    host=_config.onebot.host,
    port=_config.onebot.port,
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)


@driver.on_startup
async def _start_model_endpoint_probes() -> None:
    await start_model_endpoint_probe_worker(_config.model)


@driver.on_shutdown
async def _stop_model_endpoint_probes() -> None:
    await stop_model_endpoint_probe_workers()


for required_plugin in (
    "app.plugins.private_chat",
    "app.plugins.group_chat",
    "app.plugins.group_reactions",
    "app.plugins.owner_commands",
):
    if nonebot.load_plugin(required_plugin) is None:
        raise RuntimeError(f"Required Bot plugin failed to load: {required_plugin}")


if __name__ == "__main__":
    nonebot.run()
