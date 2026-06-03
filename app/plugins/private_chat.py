from __future__ import annotations

import os

from loguru import logger
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent

from app.adapters.onebot_event_adapter import normalize_private_message_event
from app.bootstrap import create_conversation_service
from app.config import load_config
from app.conversation.reply_formatter import ReplyFormatter
from app.features.reminder_service import (
    format_reminder_tasks,
    is_explicit_reminder_request,
    is_reminder_cancel_command,
    is_reminder_command,
    is_reminder_list_command,
    parse_reminder_cancel_id,
)
from app.features.runtime_features import create_runtime_feature_hub, maybe_save_sticker
from app.features.sticker_service import is_sticker_request
from app.models import GeneratedReply
from app.plugins.send_helper import send_private_image_direct, send_reply_bubbles
from app.routing.permission_service import PermissionService
from app.routing.rate_limiter import RateLimiter
from app.storage.database import init_database

_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))
_conversation_service = create_conversation_service(_config)
_feature_hub = create_runtime_feature_hub(_config)
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
        await maybe_save_sticker(_feature_hub, normalized)
        if is_reminder_command(normalized.text):
            reply_text = await _handle_user_reminder_command(
                normalized.user_id,
                normalized.user_name,
                normalized.scope_id,
                normalized.text,
            )
            await send_reply_bubbles(
                bot,
                event,
                reply_text,
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
        reminder = await _feature_hub.reminders.try_create_from_message(normalized)
        if reminder is not None:
            await send_reply_bubbles(
                bot,
                event,
                f"记下了，{reminder.due_at} 提醒你：{reminder.message}",
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
        if is_explicit_reminder_request(normalized.text):
            await send_reply_bubbles(
                bot,
                event,
                _REMINDER_CREATE_HELP_TEXT,
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
        if is_sticker_request(normalized.text):
            asset = await _feature_hub.stickers.choose_for_text(normalized.text)
            if asset is not None:
                try:
                    await send_private_image_direct(
                        bot,
                        user_id=normalized.user_id,
                        file_path=asset.file_path,
                    )
                    await _feature_hub.stickers.mark_used(asset.asset_id)
                except Exception as exc:
                    await _record_send_error(
                        normalized.trace_id,
                        exc,
                        0,
                        "send_private_sticker_failed",
                    )
            else:
                await send_reply_bubbles(
                    bot,
                    event,
                    "还没存到合适的表情包",
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


_REMINDER_CREATE_HELP_TEXT = "要提醒什么？比如：十分钟后提醒我喝水"


async def _handle_user_reminder_command(
    user_id: str,
    user_name: str | None,
    scope_id: str,
    text: str,
) -> str:
    if is_reminder_list_command(text):
        tasks = await _feature_hub.reminders.list_for_user(
            user_id=user_id,
            include_all=False,
            limit=10,
        )
        return format_reminder_tasks(tasks)
    if is_reminder_cancel_command(text):
        task_id = parse_reminder_cancel_id(text)
        if task_id is None:
            return "invalid reminder id"
        cancelled = await _feature_hub.reminders.cancel(
            task_id=task_id,
            user_id=user_id,
            include_all=False,
        )
        return f"reminder_cancelled={1 if cancelled else 0} id={task_id}"
    reminder = await _feature_hub.reminders.try_create_private_reminder(
        user_id=user_id,
        user_name=user_name,
        scope_id=scope_id,
        text=text,
    )
    if reminder is None:
        return _REMINDER_CREATE_HELP_TEXT
    return f"记下了，{reminder.due_at} 提醒你：{reminder.message}"


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
