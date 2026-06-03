from __future__ import annotations

import re

from app.models import MemoryProfile, QQConfig
from app.safety.safety_service import SafetyService
from app.storage.repositories import AuditRepository, MemoryProfileRepository


class MemoryService:
    def __init__(
        self,
        *,
        repository: MemoryProfileRepository,
        qq_config: QQConfig,
        safety_service: SafetyService,
        audit_repository: AuditRepository | None = None,
    ) -> None:
        self._repository = repository
        self._qq_config = qq_config
        self._safety_service = safety_service
        self._audit_repository = audit_repository

    async def get_prompt_memory(self, user_id: str) -> str:
        profile = await self._repository.get_by_user_id(user_id)
        if profile is None:
            return ""
        parts = [
            ("长期摘要", profile.summary),
            ("偏好称呼", profile.preferred_name),
            ("喜欢", profile.likes),
            ("不喜欢", profile.dislikes),
            ("重要事件", profile.important_events),
            ("安全备注", profile.safety_notes),
        ]
        return "\n".join(f"{label}：{value}" for label, value in parts if value.strip())

    async def record_user_message(
        self,
        *,
        user_id: str,
        user_name: str,
        text: str,
    ) -> MemoryProfile | None:
        if user_id not in self._qq_config.memory_allowed_user_ids:
            await self._record_event(
                "memory_skipped_not_allowed",
                f"user_id={user_id}",
            )
            return None
        if not self._safety_service.can_store_long_term_memory(text):
            await self._record_event(
                "memory_skipped_sensitive",
                f"user_id={user_id}",
            )
            return None
        existing = await self._repository.get_by_user_id(user_id)
        update = _memory_update_from_text(text)
        if update.clear:
            await self._repository.clear(user_id)
            await self._record_event("memory_cleared", f"user_id={user_id}")
            return None
        if update.skip:
            return None
        if existing is None and update.is_deletion_only():
            await self._record_event(
                "memory_updated",
                f"user_id={user_id}; fields={','.join(update.changed_fields())}",
            )
            return None
        summary = _merge_field(existing.summary if existing else "", update.summary)
        likes = _merge_field(existing.likes if existing else "", update.likes)
        dislikes = _merge_field(existing.dislikes if existing else "", update.dislikes)
        likes = _remove_field_item(likes, update.remove_likes)
        dislikes = _remove_field_item(dislikes, update.remove_dislikes)
        if update.remove_text:
            summary = _remove_field_item(summary, update.remove_text)
            likes = _remove_field_item(likes, update.remove_text)
            dislikes = _remove_field_item(dislikes, update.remove_text)
        preferred_name = update.preferred_name or (existing.preferred_name if existing else "")
        important_events = _merge_field(
            existing.important_events if existing else "",
            update.important_events,
        )
        if update.remove_text:
            important_events = _remove_field_item(important_events, update.remove_text)
        if update.clear_preferred_name:
            preferred_name = ""
        profile = await self._repository.upsert_summary(
            user_id=user_id,
            display_name=user_name,
            summary=summary,
            preferred_name=preferred_name,
            likes=likes,
            dislikes=dislikes,
            important_events=important_events,
            safety_notes=existing.safety_notes if existing else "",
        )
        await self._record_event(
            "memory_updated",
            f"user_id={user_id}; fields={','.join(update.changed_fields())}",
        )
        return profile

    async def clear_user_memory(self, user_id: str) -> None:
        await self._repository.clear(user_id)
        await self._record_event("memory_cleared", f"user_id={user_id}")

    async def _record_event(self, event: str, detail: str) -> None:
        if self._audit_repository is None:
            return
        await self._audit_repository.save_system_event(
            level="INFO",
            event=event,
            detail=detail,
        )


class NullMemoryService:
    async def get_prompt_memory(self, user_id: str) -> str:
        return ""

    async def record_user_message(
        self,
        *,
        user_id: str,
        user_name: str,
        text: str,
    ) -> None:
        return None

    async def clear_user_memory(self, user_id: str) -> None:
        return None


class _MemoryUpdate:
    def __init__(
        self,
        *,
        preferred_name: str = "",
        summary: str = "",
        likes: str = "",
        dislikes: str = "",
        remove_likes: str = "",
        remove_dislikes: str = "",
        remove_text: str = "",
        important_events: str = "",
        clear_preferred_name: bool = False,
        clear: bool = False,
        skip: bool = False,
    ) -> None:
        self.preferred_name = preferred_name
        self.summary = summary
        self.likes = likes
        self.dislikes = dislikes
        self.remove_likes = remove_likes
        self.remove_dislikes = remove_dislikes
        self.remove_text = remove_text
        self.important_events = important_events
        self.clear_preferred_name = clear_preferred_name
        self.clear = clear
        self.skip = skip

    def changed_fields(self) -> list[str]:
        fields: list[str] = []
        for name in (
            "preferred_name",
            "summary",
            "likes",
            "dislikes",
            "remove_likes",
            "remove_dislikes",
            "remove_text",
            "important_events",
        ):
            if getattr(self, name):
                fields.append(name)
        if self.clear_preferred_name:
            fields.append("clear_preferred_name")
        return fields or ["noop"]

    def is_deletion_only(self) -> bool:
        has_addition = any(
            (
                self.preferred_name,
                self.summary,
                self.likes,
                self.dislikes,
                self.important_events,
            )
        )
        has_deletion = any(
            (
                self.remove_likes,
                self.remove_dislikes,
                self.remove_text,
                self.clear_preferred_name,
            )
        )
        return has_deletion and not has_addition


def _memory_update_from_text(text: str) -> _MemoryUpdate:
    cleaned = " ".join(text.strip().split())
    if any(marker in cleaned for marker in ("清空记忆", "忘掉刚才说的")):
        return _MemoryUpdate(clear=True)
    if "别记这个" in cleaned:
        return _MemoryUpdate(skip=True)
    if len(cleaned) < 4:
        return _MemoryUpdate(skip=True)
    remove_likes = _extract_first(
        cleaned,
        (
            r"(?:别记|忘掉|删除)我喜欢(.{1,60})",
            r"(?:别记|忘掉|删除)喜欢(.{1,60})",
        ),
    )
    remove_dislikes = _extract_first(
        cleaned,
        (
            r"(?:别记|忘掉|删除)我不喜欢(.{1,60})",
            r"(?:别记|忘掉|删除)我讨厌(.{1,60})",
            r"(?:别记|忘掉|删除)不喜欢(.{1,60})",
        ),
    )
    if remove_likes or remove_dislikes:
        return _MemoryUpdate(
            remove_likes=_clean_memory_value(remove_likes),
            remove_dislikes=_clean_memory_value(remove_dislikes),
        )
    remove_text = _extract_first(
        cleaned,
        (
            r"别记(.{1,60})",
            r"忘掉(.{1,60})",
            r"删除(.{1,60})",
        ),
    )
    if remove_text:
        return _MemoryUpdate(remove_text=_clean_memory_value(remove_text))
    if re.search(r"(?:别|不要)叫我", cleaned):
        return _MemoryUpdate(clear_preferred_name=True)
    preferred_name = _extract_first(cleaned, (r"(?:以后)?叫我(.{1,20})",))
    likes = _extract_first(cleaned, (r"记住我喜欢(.{1,60})", r"我喜欢(.{1,60})"))
    dislikes = _extract_first(cleaned, (r"我不喜欢(.{1,60})", r"我讨厌(.{1,60})"))
    summary = "" if likes or dislikes or preferred_name else _summarize_user_message(cleaned)
    return _MemoryUpdate(
        preferred_name=_clean_memory_value(preferred_name),
        likes=_clean_memory_value(likes),
        dislikes=_clean_memory_value(dislikes),
        summary=summary,
    )


def _summarize_user_message(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) > 160:
        cleaned = cleaned[:157] + "..."
    return f"用户曾提到：{cleaned}"


def _extract_first(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _clean_memory_value(value: str) -> str:
    return value.strip(" ：:，,。. ")


def _merge_field(existing: str, addition: str, *, max_length: int = 300) -> str:
    addition = addition.strip()
    if not addition:
        return existing
    if addition in existing:
        return existing
    merged = f"{existing}；{addition}" if existing.strip() else addition
    return merged[:max_length]


def _remove_field_item(existing: str, target: str) -> str:
    target = target.strip()
    if not target:
        return existing
    items = [item.strip() for item in existing.split("；") if item.strip()]
    kept = [item for item in items if target not in item and item not in target]
    return "；".join(kept)
