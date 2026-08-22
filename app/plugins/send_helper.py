from __future__ import annotations

import asyncio
import base64
import random

from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment

from app.conversation.reply_formatter import split_reply_messages, truncate_naturally
from app.features.structured_reply import is_message_too_long_error
from app.models import ReplyConfig


GROUP_BUBBLE_INTERVAL_SECONDS = 1.5
TRUNCATED_MARKER = "内容已截断"


def build_reply_bubbles(
    text: str,
    *,
    scope_type: str,
    max_length: int,
    reply_mode: str = "short",
    long_text_max_length: int | None = None,
    long_text_max_bubbles: int | None = None,
) -> list[str]:
    if scope_type == "private":
        return _non_empty_bubbles(split_reply_messages(text, reply_mode=reply_mode))

    is_long_mode = reply_mode in {"long_text", "code_block"}
    limit = 3 if not is_long_mode else (long_text_max_bubbles or 8)
    effective_max_length = max_length
    if is_long_mode:
        effective_max_length = long_text_max_length or max(max_length * 4, 1200)
    bubbles = _non_empty_bubbles(split_reply_messages(text, reply_mode=reply_mode))
    selected: list[str] = []
    total = 0
    truncated = False
    for bubble in bubbles:
        extra = len(bubble) + (1 if selected else 0)
        if total + extra > effective_max_length:
            truncated = True
            break
        selected.append(bubble)
        total += extra
        if len(selected) >= limit:
            truncated = len(selected) < len(bubbles)
            break
    if not truncated:
        return selected or bubbles[:limit]

    while selected and (
        len(selected) >= limit
        or sum(len(item) for item in selected) + max(0, len(selected) - 1)
        + len(TRUNCATED_MARKER) + 1
        > effective_max_length
    ):
        selected.pop()
    if not selected:
        prefix_limit = effective_max_length - len(TRUNCATED_MARKER) - 1
        if prefix_limit > 0 and bubbles:
            prefix = truncate_naturally(
                bubbles[0],
                prefix_limit,
                reply_mode=reply_mode,
            ).strip()
            if prefix:
                selected.append(prefix)
    return [*selected, TRUNCATED_MARKER]


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
        long_text_max_length=reply_config.long_text_max_length,
        long_text_max_bubbles=reply_config.long_text_max_bubbles,
    )
    if not bubbles:
        return
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


async def send_structured_information(
    bot: Bot,
    event: Event,
    messages: tuple[str, ...] | list[str],
    *,
    fallback_messages: tuple[str, ...] | list[str] = (),
    scope_type: str,
    reply_config: ReplyConfig,
    on_send_error,
    on_sent=None,
) -> None:
    bubbles = build_structured_information_messages(messages)
    fallbacks = list(fallback_messages)
    for index, bubble in enumerate(bubbles):
        if index > 0:
            await asyncio.sleep(_message_delay_seconds(reply_config, scope_type=scope_type))
        try:
            result = await bot.send(
                event,
                _build_outgoing_message(
                    bubble,
                    scope_type=scope_type,
                    index=index,
                    group_reply_to_message_id=None,
                    group_at_user_id=None,
                ),
            )
            if on_sent is not None:
                await on_sent(index, bubble, _extract_sent_message_id(result))
        except Exception as exc:
            fallback = fallbacks[index].strip() if index < len(fallbacks) else ""
            if fallback and is_message_too_long_error(exc):
                try:
                    result = await bot.send(
                        event,
                        _build_outgoing_message(
                            fallback,
                            scope_type=scope_type,
                            index=index,
                            group_reply_to_message_id=None,
                            group_at_user_id=None,
                        ),
                    )
                    if on_sent is not None:
                        await on_sent(index, fallback, _extract_sent_message_id(result))
                    continue
                except Exception as fallback_exc:
                    await on_send_error(fallback_exc, index, fallback)
                    continue
            await on_send_error(exc, index, bubble)


def build_structured_information_messages(
    messages: tuple[str, ...] | list[str],
) -> list[str]:
    return _non_empty_bubbles(list(messages))


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


def _non_empty_bubbles(bubbles: list[str]) -> list[str]:
    return [bubble.strip() for bubble in bubbles if bubble and bubble.strip()]


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
