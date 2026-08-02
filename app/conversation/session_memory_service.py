from __future__ import annotations

import asyncio
import re

from app.models import NormalizedMessage, SessionMemory
from app.safety.safety_service import SafetyService
from app.storage.repositories import SessionMemoryRepository


class SessionMemoryService:
    def __init__(
        self,
        repository: SessionMemoryRepository,
        *,
        safety_service: SafetyService | None = None,
        max_summary_chars: int = 600,
        max_keywords: int = 12,
    ) -> None:
        self._repository = repository
        self._safety_service = safety_service or SafetyService()
        self._max_summary_chars = max_summary_chars
        self._max_keywords = max_keywords
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def get_prompt_context(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        memory = await self._repository.get(session_id)
        if memory is None or not memory.summary.strip():
            return ""
        parts = [f"本会话低敏摘要：{memory.summary.strip()}"]
        if memory.keywords:
            parts.append("本会话关键词：" + "、".join(memory.keywords))
        return "\n".join(parts)

    async def record_exchange(
        self,
        message: NormalizedMessage,
        assistant_text: str,
    ) -> SessionMemory | None:
        if not message.session_id:
            return None
        if not self._safety_service.can_store_long_term_memory(message.text):
            return None
        if not self._safety_service.can_store_long_term_memory(assistant_text):
            return None
        user_text = _clean_memory_text(message.text)
        reply_text = _clean_memory_text(assistant_text)
        if not user_text or not reply_text:
            return None

        lock = self._session_locks.setdefault(message.session_id, asyncio.Lock())
        async with lock:
            return await self._record_exchange_locked(
                message,
                user_text=user_text,
                reply_text=reply_text,
            )

    async def _record_exchange_locked(
        self,
        message: NormalizedMessage,
        *,
        user_text: str,
        reply_text: str,
    ) -> SessionMemory:
        session_id = message.session_id
        if session_id is None:
            raise ValueError("session memory requires session_id")
        existing = await self._repository.get(session_id)
        entry = f"{message.user_name}：{user_text}；机器人：{reply_text}"
        summary = _append_bounded(
            existing.summary if existing else "",
            entry,
            max_chars=self._max_summary_chars,
        )
        keywords = _merge_keywords(
            existing.keywords if existing else (),
            f"{user_text} {reply_text}",
            limit=self._max_keywords,
        )
        return await self._repository.upsert(
            session_id=session_id,
            summary=summary,
            keywords=keywords,
            sample_count=(existing.sample_count if existing else 0) + 1,
            state="temporary",
        )


def _clean_memory_text(text: str) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", str(text or ""))
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    cleaned = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[private]", cleaned)
    cleaned = re.sub(r"\b\d{17}[\dXx]\b", "[private]", cleaned)
    cleaned = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", cleaned)
    cleaned = re.sub(
        r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)",
        "[secret]",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,。.；; ")
    return cleaned[:180]


def _append_bounded(existing: str, entry: str, *, max_chars: int) -> str:
    entries = [item.strip() for item in existing.split("\n") if item.strip()]
    if entry not in entries:
        entries.append(entry)
    while entries and len("\n".join(entries)) > max_chars:
        entries.pop(0)
    return "\n".join(entries)[-max_chars:]


def _merge_keywords(
    existing: tuple[str, ...],
    text: str,
    *,
    limit: int,
) -> tuple[str, ...]:
    keywords = list(existing)
    candidates = re.findall(
        r"[A-Za-z][A-Za-z0-9_+#.-]{1,20}|[\u4e00-\u9fff]{2,8}",
        text,
    )
    ignored = {"这个", "那个", "什么", "一下", "可以", "就是", "然后", "机器人"}
    for candidate in candidates:
        if candidate in ignored or candidate in keywords:
            continue
        keywords.append(candidate)
    return tuple(keywords[-limit:])
