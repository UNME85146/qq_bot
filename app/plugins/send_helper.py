from __future__ import annotations

import asyncio
import base64
import random

from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment

from app.conversation.reply_formatter import split_reply_messages, truncate_naturally
from app.models import ReplyConfig


GROUP_BUBBLE_INTERVAL_SECONDS = 1.5


def build_reply_bubbles(
    text: str,
    *,
    scope_type: str,
    max_length: int,
    reply_mode: str = "short",
) -> list[str]:
    if scope_type == "private":
        return split_reply_messages(text, reply_mode=reply_mode)

    limit = 3 if reply_mode == "short" else 6
    effective_max_length = max_length
    if reply_mode in {"long_text", "code_block"}:
        effective_max_length = max(max_length * 4, 1200)
    text = truncate_naturally(text, effective_max_length, reply_mode=reply_mode)
    bubbles = split_reply_messages(text, reply_mode=reply_mode)
    selected: list[str] = []
    total = 0
    for bubble in bubbles:
        extra = len(bubble) + (1 if selected else 0)
        if total + extra > effective_max_length:
            break
        selected.append(bubble)
        total += extra
        if len(selected) >= limit:
            break
    return selected or bubbles[:limit]


async def send_reply_bubbles(
    bot: Bot,
    event: Event,
    text: str,
    *,
    scope_type: str,
    reply_config: ReplyConfig,
    on_send_error,
    group_reply_to_message_id: str | None = None,
    group_at_user_id: str | None = None,
    on_sent=None,
    reply_mode: str = "short",
) -> None:
    bubbles = build_reply_bubbles(
        text,
        scope_type=scope_type,
        max_length=reply_config.max_reply_length,
        reply_mode=reply_mode,
    )
    for index, bubble in enumerate(bubbles):
        if index > 0:
            await asyncio.sleep(_message_delay_seconds(reply_config, scope_type=scope_type))
        try:
            message = _build_outgoing_message(
                bubble,
                scope_type=scope_type,
                index=index,
                group_reply_to_message_id=group_reply_to_message_id,
                group_at_user_id=group_at_user_id,
            )
            result = await bot.send(event, message)
            if on_sent is not None:
                await on_sent(index, bubble, _extract_sent_message_id(result))
        except Exception as exc:
            await on_send_error(exc, index, bubble)


async def send_group_image_direct(
    bot: Bot,
    *,
    group_id: str,
    file_path: str,
) -> Any:
    message = Message()
    message += MessageSegment.image(_image_segment_file(file_path))
    return await bot.send_group_msg(group_id=int(group_id), message=message)


async def send_private_image_direct(
    bot: Bot,
    *,
    user_id: str,
    file_path: str,
) -> Any:
    message = Message()
    message += MessageSegment.image(_image_segment_file(file_path))
    return await bot.send_private_msg(user_id=int(user_id), message=message)


async def send_group_record_direct(
    bot: Bot,
    *,
    group_id: str,
    file_path: str,
) -> Any:
    message = Message()
    message += MessageSegment.record(_file_segment_base64(file_path))
    return await bot.send_group_msg(group_id=int(group_id), message=message)


async def send_private_record_direct(
    bot: Bot,
    *,
    user_id: str,
    file_path: str,
) -> Any:
    message = Message()
    message += MessageSegment.record(_file_segment_base64(file_path))
    return await bot.send_private_msg(user_id=int(user_id), message=message)


def _image_segment_file(file_path: str) -> str:
    return _file_segment_base64(file_path)


def _file_segment_base64(file_path: str) -> str:
    data = Path(file_path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"base64://{encoded}"


def build_group_reply_message(
    bubble: str,
    *,
    reply_to_message_id: str | None,
    at_user_id: str | None,
    include_reference: bool = True,
) -> Message | str:
    if not include_reference or (not reply_to_message_id and not at_user_id):
        return bubble
    segments = Message()
    if reply_to_message_id:
        segments += MessageSegment.reply(reply_to_message_id)
    if at_user_id:
        segments += MessageSegment.at(at_user_id)
        segments += MessageSegment.text(" ")
    segments += MessageSegment.text(bubble)
    return segments


def _build_outgoing_message(
    bubble: str,
    *,
    scope_type: str,
    index: int,
    group_reply_to_message_id: str | None,
    group_at_user_id: str | None,
) -> Message | str:
    if scope_type != "group" or index > 0:
        return bubble
    return build_group_reply_message(
        bubble,
        reply_to_message_id=group_reply_to_message_id,
        at_user_id=group_at_user_id,
    )


def _extract_sent_message_id(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, dict):
        for key in ("message_id", "id"):
            if result.get(key) is not None:
                return str(result[key])
        data = result.get("data")
        if isinstance(data, dict):
            return _extract_sent_message_id(data)
    for key in ("message_id", "id"):
        value = getattr(result, key, None)
        if value is not None:
            return str(value)
    return None


def _message_delay_seconds(reply_config: ReplyConfig, *, scope_type: str) -> float:
    if scope_type == "group":
        return GROUP_BUBBLE_INTERVAL_SECONDS
    min_ms = max(300, min(reply_config.min_delay_ms, 800))
    max_ms = max(min_ms, min(reply_config.max_delay_ms, 800))
    return random.uniform(min_ms, max_ms) / 1000
