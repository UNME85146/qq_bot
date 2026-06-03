from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent

from app.config import load_config
from app.features.runtime_features import create_runtime_feature_hub
from app.models import NormalizedMessage
from app.plugins.send_helper import send_group_image_direct
from app.storage.repositories import GroupMuteStateRepository


_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))
_feature_hub = create_runtime_feature_hub(_config)
_group_mute_repository = GroupMuteStateRepository(_config.storage.database_path)

group_reactions = on_notice(priority=9, block=False)


@group_reactions.handle()
async def _handle_notice(bot: Bot, event: NoticeEvent) -> None:
    data = _event_data(event)
    if not _is_plus_one_reaction(data):
        return
    group_id = _str_or_none(data.get("group_id"))
    message_id = _str_or_none(
        data.get("message_id")
        or data.get("msg_id")
        or data.get("target_message_id")
    )
    user_id = _str_or_none(data.get("user_id") or data.get("operator_id"))
    if not group_id or not message_id or not user_id:
        return
    if group_id not in _config.qq.allowed_group_ids:
        return
    mute_state = await _group_mute_repository.get_by_group_id(group_id)
    if mute_state is not None and mute_state.muted:
        return
    normalized = NormalizedMessage(
        trace_id=uuid4().hex,
        self_id=str(event.self_id),
        message_id=f"notice-{message_id}-{user_id}",
        message_type="notice",
        scope_type="group",
        scope_id=group_id,
        user_id=user_id,
        group_id=group_id,
        user_name=user_id,
        raw_message="plus_one_reaction",
        text="+1",
        is_at_self=False,
        mentioned_user_ids=[],
        received_at=datetime.fromtimestamp(event.time, UTC).isoformat(),
    )
    candidate = await _feature_hub.repeats.candidate_from_notice(
        group_id=group_id,
        message_id=message_id,
    )
    if candidate is None:
        return
    marked = await _feature_hub.repeats.maybe_mark_repeated(
        trigger_message=normalized,
        candidate=candidate,
        plus_one=True,
    )
    if not marked:
        return
    try:
        await asyncio.sleep(0.3)
        if candidate.sticker_asset is not None:
            await send_group_image_direct(
                bot,
                group_id=candidate.group_id,
                file_path=candidate.sticker_asset.file_path,
            )
            await _feature_hub.stickers.mark_used(candidate.sticker_asset.asset_id)
        else:
            await bot.send_group_msg(group_id=int(candidate.group_id), message=candidate.text)
    except Exception as exc:
        await _feature_hub.record_system_event(
            level="ERROR",
            event="plus_one_notice_repeat_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=normalized.trace_id,
        )


def _event_data(event: NoticeEvent) -> dict:
    if hasattr(event, "model_dump"):
        return event.model_dump()
    return dict(event)


def _is_plus_one_reaction(data: dict) -> bool:
    notice_type = str(data.get("notice_type", "")).lower()
    if notice_type not in {
        "group_msg_emoji_like",
        "group_msg_reaction",
        "group_reaction",
        "message_reaction",
    }:
        return False
    values = [
        data.get("emoji_id"),
        data.get("emojiId"),
        data.get("emoji"),
        data.get("id"),
        data.get("code"),
        data.get("raw"),
        data.get("name"),
    ]
    likes = data.get("likes")
    if isinstance(likes, list):
        values.extend(likes)
    text = " ".join(str(value) for value in values if value is not None).lower()
    return "+1" in text or "plus" in text or "1" == text.strip()


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
