from __future__ import annotations

import asyncio
import re

from app.models import GroupMemberProfile, NormalizedMessage, QQConfig
from app.safety.safety_service import SafetyService
from app.storage.repositories import GroupMemberProfileRepository


_METRIC_KEYS = (
    "total_text_chars",
    "short_message_count",
    "medium_message_count",
    "question_count",
    "punctuation_count",
    "media_message_count",
    "sticker_message_count",
    "at_mention_count",
    "reply_count",
)


class GroupMemberProfileService:
    def __init__(
        self,
        *,
        repository: GroupMemberProfileRepository,
        qq_config: QQConfig,
        safety_service: SafetyService,
    ) -> None:
        self._repository = repository
        self._qq_config = qq_config
        self._safety_service = safety_service
        self._profile_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def record_message(
        self,
        message: NormalizedMessage,
    ) -> GroupMemberProfile | None:
        if (
            message.scope_type != "group"
            or not message.group_id
            or message.group_id not in self._qq_config.allowed_group_ids
        ):
            return None
        if not self._safety_service.can_store_long_term_memory(message.text):
            return None
        key = (message.group_id, message.user_id)
        lock = self._profile_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._record_message_locked(message)

    async def _record_message_locked(
        self,
        message: NormalizedMessage,
    ) -> GroupMemberProfile:
        group_id = message.group_id
        if group_id is None:
            raise ValueError("group member profile requires group_id")
        existing = await self._repository.get(group_id, message.user_id)
        metrics = {key: 0 for key in _METRIC_KEYS}
        if existing is not None:
            for key in _METRIC_KEYS:
                metrics[key] = max(0, int(existing.metrics.get(key, 0)))
        text = _clean_display_text(message.text)
        metrics["total_text_chars"] += len(text)
        metrics["short_message_count"] += int(len(text) <= 12)
        metrics["medium_message_count"] += int(12 < len(text) <= 40)
        metrics["question_count"] += int(any(mark in text for mark in ("?", "？")))
        metrics["punctuation_count"] += int(bool(re.search(r"[，。！？!?、；;,.]", text)))
        metrics["media_message_count"] += int(bool(message.media_items))
        metrics["sticker_message_count"] += int(
            any(
                item.type == "face"
                or str(item.sub_type or "").lower() in {"sticker", "emoji"}
                for item in message.media_items
            )
        )
        metrics["at_mention_count"] += int(bool(message.mentioned_user_ids))
        metrics["reply_count"] += int(bool(message.reply_to_message_id))
        message_count = (existing.message_count if existing else 0) + 1
        preference_notes = _merge_preference_notes(
            existing.preference_notes if existing else "",
            _extract_preference_note(message.text, self._safety_service),
        )
        return await self._repository.upsert(
            group_id=group_id,
            user_id=message.user_id,
            display_name=_clean_display_name(message.user_name),
            summary=_build_summary(metrics, message_count),
            metrics=metrics,
            message_count=message_count,
            preference_notes=preference_notes,
        )

    async def get_prompt_context(self, group_id: str, user_id: str) -> str:
        profile = await self._repository.get(group_id, user_id)
        if profile is None:
            return ""
        lines = []
        if profile.display_name:
            lines.append(f"当前显示名/称呼：{_clean_display_name(profile.display_name)}")
        if profile.summary.strip():
            lines.append("低敏表达习惯：" + profile.summary.strip())
        if profile.preference_notes.strip():
            lines.append("明确偏好约束（优先于历史习惯）：" + profile.preference_notes.strip())
        return "该成员在本群的" + "；".join(lines)


def _build_summary(metrics: dict[str, int], message_count: int) -> str:
    count = max(1, message_count)
    average_length = metrics["total_text_chars"] / count
    traits = ["表达以短句为主" if average_length <= 12 else "表达通常较完整"]
    if metrics["question_count"] / count >= 0.35:
        traits.append("较常用提问推进话题")
    if metrics["media_message_count"] / count >= 0.2:
        traits.append("会配合图片或表情互动")
    if metrics["reply_count"]:
        traits.append("有引用消息继续对话的习惯")
    return "，".join(traits)


def _clean_display_text(text: str) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", str(text or ""))
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_display_name(value: str) -> str | None:
    cleaned = re.sub(r"[\r\n\t]", " ", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:32] or None


def _extract_preference_note(text: str, safety_service: SafetyService) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip(" ：:，,。.!！?？")
    if not cleaned:
        return ""
    match = re.search(
        r"(?:拒绝|禁止|不准|不要|别)[^，。！？!?；;]{0,20}?"
        r"(?:例如|比如|像)[“\"']?([^，。！？!?；;“”\"']{2,40}?)[”\"']?"
        r"(?:这种|这类)(?:莫名其妙的)?(?:词|话术|话|称呼|口头禅)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r"(?:不要|别|不想|请不要|拒绝|禁止|不准)(?:再)?"
            r"(?:说|提|用|喊|叫)([^，。！？!?；;]{2,40})",
            cleaned,
            flags=re.IGNORECASE,
        )
    if match is None:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip(" ：:，,。.!！?？")
    if not value or len(value) > 40:
        return ""
    if not safety_service.can_store_long_term_memory(value):
        return ""
    return f"不要固定使用“{value}”"


def _merge_preference_notes(existing: str, addition: str, *, max_chars: int = 240) -> str:
    values = [item.strip() for item in str(existing or "").split("；") if item.strip()]
    if addition and addition not in values:
        values.append(addition)
    while values and len("；".join(values)) > max_chars:
        values.pop(0)
    return "；".join(values)
