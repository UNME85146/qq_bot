from __future__ import annotations

import re

from app.models import GroupContext, QQConfig
from app.safety.safety_service import SafetyService
from app.storage.repositories import GroupContextRepository


class GroupContextService:
    def __init__(
        self,
        *,
        repository: GroupContextRepository,
        qq_config: QQConfig,
        safety_service: SafetyService,
        max_summary_length: int = 360,
        max_keywords_length: int = 120,
    ) -> None:
        self._repository = repository
        self._qq_config = qq_config
        self._safety_service = safety_service
        self._max_summary_length = max_summary_length
        self._max_keywords_length = max_keywords_length

    async def record_group_message(
        self,
        *,
        group_id: str,
        message_id: str | None,
        text: str,
    ) -> GroupContext | None:
        if group_id not in self._qq_config.allowed_group_ids:
            return None
        if not self._safety_service.can_store_long_term_memory(text):
            return None
        cleaned = _clean_group_text(text)
        if len(cleaned) < 2:
            return None

        existing = await self._repository.get_by_group_id(group_id)
        summary = _merge_summary(
            existing.summary if existing else "",
            cleaned,
            max_length=self._max_summary_length,
        )
        topic_keywords = _merge_keywords(
            existing.topic_keywords if existing else "",
            cleaned,
            max_length=self._max_keywords_length,
        )
        return await self._repository.upsert(
            group_id=group_id,
            summary=summary,
            topic_keywords=topic_keywords,
            last_message_id=message_id,
            message_count=(existing.message_count if existing else 0) + 1,
        )

    async def get_prompt_context(self, group_id: str) -> str:
        context = await self._repository.get_by_group_id(group_id)
        if context is None:
            return ""
        parts = []
        if context.summary.strip():
            parts.append(f"摘要：{context.summary.strip()}")
        if context.topic_keywords.strip():
            parts.append(f"关键词：{context.topic_keywords.strip()}")
        return "\n".join(parts)


class NullGroupContextService:
    async def record_group_message(
        self,
        *,
        group_id: str,
        message_id: str | None,
        text: str,
    ) -> None:
        return None

    async def get_prompt_context(self, group_id: str) -> str:
        return ""


def _clean_group_text(text: str) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", text)
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    cleaned = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,。. ")
    if len(cleaned) > 80:
        cleaned = cleaned[:77] + "..."
    return cleaned


def _merge_summary(existing: str, addition: str, *, max_length: int) -> str:
    if not addition or addition in existing:
        return existing[:max_length]
    items = [item for item in existing.split("；") if item.strip()]
    items.append(addition)
    merged = "；".join(items)
    while len(merged) > max_length and len(items) > 1:
        items.pop(0)
        merged = "；".join(items)
    return merged[:max_length]


def _merge_keywords(existing: str, text: str, *, max_length: int) -> str:
    keywords = [item for item in existing.split("，") if item.strip()]
    for token in _extract_keywords(text):
        if token not in keywords:
            keywords.append(token)
    merged = "，".join(keywords)
    while len(merged) > max_length and len(keywords) > 1:
        keywords.pop(0)
        merged = "，".join(keywords)
    return merged[:max_length]


def _extract_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{1,20}|\d{1,6}|[\u4e00-\u9fff]{2,8}", text)
    ignored = {"这个", "那个", "什么", "一下", "可以", "就是", "然后", "我们", "你们"}
    result: list[str] = []
    for token in tokens:
        token = token.strip(" ：:，,。. ")
        if not token or token in ignored or token == "[url]":
            continue
        if token not in result:
            result.append(token)
        if len(result) >= 8:
            break
    return result
