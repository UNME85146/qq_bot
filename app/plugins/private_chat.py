from __future__ import annotations

import os

from loguru import logger
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent

from app.adapters.onebot_event_adapter import normalize_private_message_event
from app.bootstrap import create_conversation_service
from app.config import load_config
from app.conversation.reply_formatter import ReplyFormatter
from app.models import GeneratedReply
from app.plugins.send_helper import send_reply_bubbles
from app.routing.permission_service import PermissionService
from app.routing.rate_limiter import RateLimiter
from app.storage.database import init_database

_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))
_conversation_service = create_conversation_service(_config)
_permission_service = PermissionService(_config.qq)
_rate_limiter = RateLimiter(
    _config.limits.group_cooldown_seconds,
    private_cooldown_seconds=_config.limits.private_cooldown_seconds,
    max_user_messages_per_minute=_config.limits.max_user_messages_per_minute,
    max_group_messages_per_minute=_config.limits.max_group_messages_per_minute,
)
_reply_formatter = ReplyFormatter(_config.reply.max_reply_length)

private_chat = on_message(priority=10, block=False)


@get_driver().on_startup
async def _init_storage() -> None:
    await init_database(_config.storage.database_path)
    logger.info("SQLite storage initialized: {}", _config.storage.database_path)


@private_chat.handle()
async def _handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    normalized = normalize_private_message_event(event)
    if normalized is None:
        return
    if _permission_service.is_private_user_allowed(normalized.user_id):
        if not _rate_limiter.allow_user_minute(normalized.user_id):
            await _conversation_service.record_reply_audit(
                normalized,
                action="silence",
                reason="user_minute_rate_limited",
                model_called=False,
                safety_blocked=False,
            )
            logger.info("Private message ignored by user minute limit: user_id={}", normalized.user_id)
            return
        if not _rate_limiter.allow_private(normalized.user_id):
            await _conversation_service.record_reply_audit(
                normalized,
                action="reply",
                reason="private_cooldown",
                model_called=False,
                safety_blocked=False,
            )
            reply = GeneratedReply(
                text=_reply_formatter.format("等下，太快了。"),
                raw_model_text="等下，太快了。",
                model_name="rate_limiter",
                finish_reason="private_cooldown",
            )
            await send_reply_bubbles(
                bot,
                event,
                reply.text,
                scope_type="private",
                reply_config=_config.reply,
                on_send_error=lambda exc, index, bubble: _record_send_error(
                    normalized.trace_id,
                    exc,
                    index,
                    "send_private_reply_failed",
                ),
            )
            return

    reply = await _conversation_service.handle_private_message(normalized)
    if reply is None:
        logger.info("Private message ignored by whitelist: user_id={}", event.user_id)
        return

    await send_reply_bubbles(
        bot,
        event,
        reply.text,
        scope_type="private",
        reply_config=_config.reply,
        on_send_error=lambda exc, index, bubble: _record_send_error(
            normalized.trace_id,
            exc,
            index,
            "send_private_reply_failed",
        ),
    )


async def _record_send_error(
    trace_id: str,
    exc: Exception,
    index: int,
    event_name: str,
) -> None:
    await _conversation_service.record_system_event(
        level="ERROR",
        event=event_name,
        detail=f"{type(exc).__name__}: bubble_index={index}; {str(exc)[:120]}",
        trace_id=trace_id,
    )
    logger.exception("Failed to send private reply bubble: trace_id={}, index={}", trace_id, index)
