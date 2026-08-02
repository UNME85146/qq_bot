from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent

from app.models import MediaItem, NormalizedMessage


def normalize_private_message_event(
    event: PrivateMessageEvent,
) -> NormalizedMessage | None:
    self_id = str(event.self_id)
    user_id = str(event.user_id)
    if self_id == user_id:
        return None

    user_name = event.sender.nickname or event.sender.card or user_id
    media_items = _extract_media_items(event.message, event.raw_message)
    text = event.message.extract_plain_text().strip() or _clean_text_from_raw(
        event.raw_message,
        media_items=media_items,
    )

    return NormalizedMessage(
        trace_id=uuid4().hex,
        self_id=self_id,
        message_id=str(event.message_id),
        message_type="private",
        scope_type="private",
        scope_id=user_id,
        user_id=user_id,
        group_id=None,
        user_name=user_name,
        raw_message=event.raw_message,
        text=text,
        is_at_self=False,
        mentioned_user_ids=[],
        received_at=datetime.fromtimestamp(event.time, UTC).isoformat(),
        media_items=media_items,
        group_role="unknown",
    )


def normalize_group_message_event(
    event: GroupMessageEvent,
) -> NormalizedMessage | None:
    self_id = str(event.self_id)
    user_id = str(event.user_id)
    if self_id == user_id:
        return None

    mentioned_user_ids = _extract_mentioned_user_ids(event.message, event.raw_message)
    group_id = str(event.group_id)
    user_name = event.sender.nickname or event.sender.card or user_id
    text = _extract_group_text(event.message, event.raw_message)
    media_items = _extract_media_items(event.message, event.raw_message)

    return NormalizedMessage(
        trace_id=uuid4().hex,
        self_id=self_id,
        message_id=str(event.message_id),
        message_type="group",
        scope_type="group",
        scope_id=group_id,
        user_id=user_id,
        group_id=group_id,
        user_name=user_name,
        raw_message=event.raw_message,
        text=text,
        is_at_self=self_id in mentioned_user_ids,
        mentioned_user_ids=mentioned_user_ids,
        received_at=datetime.fromtimestamp(event.time, UTC).isoformat(),
        reply_to_message_id=_extract_reply_to_message_id(event.message, event.raw_message),
        media_items=media_items,
        group_role=_group_role_from_event(event),
    )


def _group_role_from_event(event: GroupMessageEvent) -> str:
    try:
        role = getattr(event.sender, "role", None)
        normalized = str(role).strip().lower()
    except Exception:
        return "unknown"
    return normalized if normalized in {"owner", "admin", "member"} else "unknown"


def _extract_mentioned_user_ids(message: Message, raw_message: str) -> list[str]:
    mentioned_user_ids: list[str] = []
    for segment in message:
        if segment.type != "at":
            continue
        qq = segment.data.get("qq") or segment.data.get("user_id") or segment.data.get("id")
        if qq is None or str(qq) == "all":
            continue
        mentioned_user_ids.append(str(qq))

    for pattern in (
        r"\[CQ:at,qq=(\d+)(?:,[^\]]*)?\]",
        r"\[at:qq=(\d+)(?:,[^\]]*)?\]",
        r"@\S+\s*\((\d+)\)",
    ):
        for match in re.findall(pattern, raw_message):
            mentioned_user_ids.append(str(match))

    return list(dict.fromkeys(mentioned_user_ids))


def _extract_group_text(message: Message, raw_message: str) -> str:
    text = message.extract_plain_text().strip()
    if not text or _contains_at_markup(text):
        text = raw_message.strip()

    text = re.sub(r"\[CQ:at,qq=\d+(?:,[^\]]*)?\]", " ", text)
    text = re.sub(r"\[CQ:reply,id=[^\]]+\]", " ", text)
    text = re.sub(r"\[at:qq=\d+(?:,[^\]]*)?\]", " ", text)
    text = re.sub(r"\[reply:id=[^\]]+\]", " ", text)
    text = re.sub(r"\[CQ:image,[^\]]+\]", " ", text)
    text = re.sub(r"\[CQ:face,[^\]]+\]", " ", text)
    text = re.sub(r"\[image:[^\]]+\]", " ", text)
    text = re.sub(r"\[face:[^\]]+\]", " ", text)
    text = re.sub(r"@\S+\s*\(\d+\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_reply_to_message_id(message: Message, raw_message: str) -> str | None:
    for segment in message:
        if segment.type != "reply":
            continue
        reply_id = (
            segment.data.get("id")
            or segment.data.get("message_id")
            or segment.data.get("reply_id")
        )
        if reply_id is not None:
            return str(reply_id)

    for pattern in (
        r"\[CQ:reply,id=([^\],]+)(?:,[^\]]*)?\]",
        r"\[reply:id=([^\],]+)(?:,[^\]]*)?\]",
    ):
        match = re.search(pattern, raw_message)
        if match:
            return str(match.group(1))
    return None


def _extract_media_items(message: Message, raw_message: str) -> tuple[MediaItem, ...]:
    items: list[MediaItem] = []
    for segment in message:
        if segment.type == "image":
            url = _str_or_none(segment.data.get("url"))
            file = _str_or_none(segment.data.get("file"))
            items.append(
                MediaItem(
                    type="image",
                    url=url or (file if _looks_like_url(file) else None),
                    file=file,
                    summary=_media_summary(segment.data),
                    sub_type=_media_sub_type(segment.data),
                )
            )
        elif segment.type == "face":
            items.append(
                MediaItem(
                    type="face",
                    file=_str_or_none(segment.data.get("id")),
                    summary=_str_or_none(segment.data.get("raw"))
                    or _str_or_none(segment.data.get("summary")),
                )
            )

    for match in re.finditer(r"\[CQ:image,([^\]]+)\]", raw_message):
        data = _parse_cq_data(match.group(1))
        items.append(
            MediaItem(
                type="image",
                url=data.get("url"),
                file=data.get("file"),
                summary=_media_summary(data),
                sub_type=_media_sub_type(data),
            )
        )
    for match in re.finditer(r"\[CQ:face,([^\]]+)\]", raw_message):
        data = _parse_cq_data(match.group(1))
        items.append(
            MediaItem(
                type="face",
                file=data.get("id") or data.get("file"),
                summary=data.get("raw") or data.get("summary"),
            )
        )

    deduped: list[MediaItem] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in items:
        key = (item.type, item.url, item.file)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return tuple(deduped)


def _parse_cq_data(data: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in data.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _media_summary(data: dict) -> str | None:
    values = []
    for key in ("summary", "raw", "name", "type", "image_type", "biz_type"):
        value = _str_or_none(data.get(key))
        if value:
            values.append(value)
    return " ".join(dict.fromkeys(values)) or None


def _media_sub_type(data: dict) -> str | None:
    for key in ("sub_type", "image_type", "biz_type", "type"):
        value = _str_or_none(data.get(key))
        if value:
            return value
    return None


def _looks_like_url(value: str | None) -> bool:
    return value is not None and value.lower().startswith(("http://", "https://"))


def _clean_text_from_raw(raw_message: str, *, media_items: tuple[MediaItem, ...]) -> str:
    text = re.sub(r"\[CQ:image,[^\]]+\]", " ", raw_message)
    text = re.sub(r"\[CQ:face,[^\]]+\]", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        return text
    return "[media]" if media_items else raw_message.strip()


def _contains_at_markup(text: str) -> bool:
    return "[CQ:at,qq=" in text or "[at:qq=" in text
