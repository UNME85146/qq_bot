from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from app.models import (
    GroupMemberProfileVector,
    GroupProfileConfig,
    NormalizedMessage,
    QQConfig,
)
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    GroupMemberProfileVectorRepository,
)


VECTOR_DIMENSION = 256
_SHANGHAI = timezone(timedelta(hours=8))
_ALIAS_PATTERN = re.compile(
    r"(?:我叫|叫我|大家叫我|外号是|昵称是|称呼我为|人称)([^，。！？!?\s]{1,16})"
)


class GroupMemberProfileVectorService:
    """SQLite-backed hybrid vector/profile store for group members.

    The store keeps only sanitized profile summaries and vectors. Raw messages are
    read from group_message_index during bounded refreshes and are never copied into
    the profile vector table.
    """

    def __init__(
        self,
        *,
        repository: GroupMemberProfileVectorRepository,
        qq_config: QQConfig,
        profile_config: GroupProfileConfig,
        safety_service: SafetyService,
        model_client: object | None = None,
    ) -> None:
        self._repository = repository
        self._qq_config = qq_config
        self._config = profile_config
        self._safety_service = safety_service
        self._model_client = model_client
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def record_message(self, message: NormalizedMessage) -> GroupMemberProfileVector | None:
        group_id = message.group_id
        if (
            not self._config.enabled
            or message.scope_type != "group"
            or not group_id
            or group_id not in self._config.group_ids
            or not self._safety_service.can_store_long_term_memory(message.text)
        ):
            return None
        key = (group_id, message.user_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = await self._repository.get(group_id, message.user_id)
            aliases = set(existing.aliases if existing else ())
            display_name = _clean_display_name(message.user_name)
            if display_name and display_name != message.user_id:
                aliases.add(display_name)
                await self._repository.upsert_alias(
                    group_id=group_id,
                    alias=display_name,
                    user_id=message.user_id,
                    confidence=0.7,
                    source="display_name",
                )
            for alias in _extract_self_aliases(message.text):
                if _safe_profile_text(alias, self._safety_service):
                    aliases.add(alias)
                    await self._repository.upsert_alias(
                        group_id=group_id,
                        alias=alias,
                        user_id=message.user_id,
                        confidence=0.85,
                        source="self_declared",
                    )
            count = (existing.message_count if existing else 0) + 1
            summary = existing.summary if existing else _basic_summary(count, message.text)
            preference_notes = existing.preference_notes if existing else ""
            profile_text = _profile_text(display_name or (existing.display_name if existing else None), aliases, summary, preference_notes)
            return await self._repository.upsert_profile(
                group_id=group_id,
                user_id=message.user_id,
                display_name=display_name or (existing.display_name if existing else None),
                aliases=sorted(aliases),
                profile_text=profile_text,
                summary=summary,
                preference_notes=preference_notes,
                embedding=hashing_embedding(profile_text),
                message_count=count,
                source_window_start=existing.source_window_start if existing else None,
                source_window_end=datetime.now(UTC).replace(microsecond=0).isoformat(),
                last_enriched_at=existing.last_enriched_at if existing else None,
            )

    async def get_prompt_context(
        self,
        group_id: str,
        user_id: str,
        query_text: str = "",
    ) -> str:
        if not self._config.enabled or group_id not in self._config.group_ids:
            return ""
        current = await self._repository.get(group_id, user_id)
        aliases = await self._repository.list_aliases(group_id)
        lines: list[str] = []
        if current is not None:
            if current.display_name:
                lines.append(f"当前成员显示名：{_clean_display_name(current.display_name)}")
            if current.aliases:
                lines.append("当前成员常用称呼：" + "、".join(current.aliases[:8]))
            if current.summary:
                lines.append("当前成员画像：" + _clip(current.summary, 220))
            if current.preference_notes:
                lines.append("当前成员偏好：" + _clip(current.preference_notes, 180))

        query = _clean_profile_text(query_text)
        if query:
            matched = [item for item in aliases if item.alias and item.alias in query]
            seen_users = {user_id}
            for item in matched[:4]:
                if item.user_id in seen_users:
                    continue
                profile = await self._repository.get(group_id, item.user_id)
                if profile is None:
                    continue
                seen_users.add(item.user_id)
                lines.append(
                    f"群内称呼映射：{item.alias} 指向 {profile.display_name or '该成员'}；"
                    f"画像：{_clip(profile.summary, 140)}"
                )
            if not matched:
                query_vector = hashing_embedding(query)
                profiles = await self._repository.list_profiles(group_id)
                ranked = sorted(
                    (
                        (_cosine(query_vector, profile.embedding), profile)
                        for profile in profiles
                        if profile.user_id != user_id and profile.embedding
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                for score, profile in ranked[:2]:
                    if score < 0.28:
                        continue
                    lines.append(
                        f"可能相关成员：{profile.display_name or '该成员'}；"
                        f"常用称呼：{'、'.join(profile.aliases[:5])}；"
                        f"画像：{_clip(profile.summary, 140)}"
                    )
        return "该成员在本群的" + "；".join(lines) if lines else ""

    async def refresh_configured_groups(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if not self._config.enabled:
            return {"status": "disabled", "groups": []}
        current = (now or datetime.now(UTC)).astimezone(UTC)
        results = []
        for group_id in self._config.group_ids:
            results.append(await self.refresh_group(group_id, now=current, force=force))
        return {"status": "completed", "groups": results}

    async def refresh_group(
        self,
        group_id: str,
        *,
        now: datetime | None = None,
        force: bool = False,
        aliases: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        if group_id not in self._config.group_ids:
            return {"group_id": group_id, "status": "not_configured"}
        current = (now or datetime.now(UTC)).astimezone(UTC)
        run_date = current.astimezone(_SHANGHAI).date().isoformat()
        claimed = await self._repository.claim_run(group_id, run_date)
        if not force and not claimed:
            return {"group_id": group_id, "status": "already_claimed", "run_date": run_date}
        cutoff = current - timedelta(days=self._config.lookback_days)
        messages = await self._repository.list_messages_since(
            group_id,
            cutoff.strftime("%Y-%m-%d %H:%M:%S"),
            limit=100_000,
        )
        grouped: dict[str, list] = defaultdict(list)
        for message in messages:
            if message.text.strip():
                grouped[message.user_id].append(message)
        profile_count = 0
        try:
            for user_id, user_messages in grouped.items():
                existing = await self._repository.get(group_id, user_id)
                user_messages = user_messages[-self._config.max_messages_per_user :]
                display_name = _clean_display_name(user_messages[-1].user_name) if user_messages else None
                alias_values = set(existing.aliases if existing else ())
                if display_name and display_name != user_id:
                    alias_values.add(display_name)
                    await self._repository.upsert_alias(
                        group_id=group_id, alias=display_name, user_id=user_id,
                        confidence=0.7, source="display_name",
                    )
                if aliases and user_id in aliases:
                    for alias in aliases[user_id]:
                        cleaned_alias = _clean_display_name(alias)
                        if cleaned_alias:
                            alias_values.add(cleaned_alias)
                            await self._repository.upsert_alias(
                                group_id=group_id, alias=cleaned_alias, user_id=user_id,
                                confidence=1.0, source="operator",
                            )
                for message in user_messages:
                    for alias in _extract_self_aliases(message.text):
                        if _safe_profile_text(alias, self._safety_service):
                            alias_values.add(alias)
                            await self._repository.upsert_alias(
                                group_id=group_id, alias=alias, user_id=user_id,
                                confidence=0.85, source="self_declared",
                            )
                summary, preference_notes, model_aliases = await self._enrich_user(
                    display_name=display_name,
                    messages=[message.text for message in user_messages],
                    existing=existing,
                )
                alias_values.update(model_aliases)
                for alias in model_aliases:
                    await self._repository.upsert_alias(
                        group_id=group_id, alias=alias, user_id=user_id,
                        confidence=0.75, source="model",
                    )
                profile_text = _profile_text(display_name, alias_values, summary, preference_notes)
                await self._repository.upsert_profile(
                    group_id=group_id,
                    user_id=user_id,
                    display_name=display_name or (existing.display_name if existing else None),
                    aliases=sorted(alias_values),
                    profile_text=profile_text,
                    summary=summary,
                    preference_notes=preference_notes,
                    embedding=hashing_embedding(profile_text),
                    message_count=len(user_messages),
                    source_window_start=messages[0].created_at if messages else None,
                    source_window_end=messages[-1].created_at if messages else None,
                    last_enriched_at=current.replace(microsecond=0).isoformat(),
                )
                profile_count += 1
            await self._repository.finish_run(
                group_id=group_id, run_date=run_date, status="completed",
                message_count=len(messages), profile_count=profile_count,
            )
            return {"group_id": group_id, "status": "completed", "messages": len(messages), "profiles": profile_count, "run_date": run_date}
        except Exception as exc:
            await self._repository.finish_run(
                group_id=group_id, run_date=run_date, status="failed",
                message_count=len(messages), profile_count=profile_count,
                error_text=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def _enrich_user(
        self,
        *,
        display_name: str | None,
        messages: list[str],
        existing: GroupMemberProfileVector | None,
    ) -> tuple[str, str, set[str]]:
        fallback_summary = existing.summary if existing and existing.summary else _basic_summary(len(messages), " ".join(messages))
        fallback_preferences = existing.preference_notes if existing else ""
        if not self._config.model_enrichment_enabled or self._model_client is None or not messages:
            return fallback_summary, fallback_preferences, set()
        generate = getattr(self._model_client, "generate_with_options", None)
        if not callable(generate):
            return fallback_summary, fallback_preferences, set()
        prompt = {
            "display_name": display_name or "",
            "messages": [_clip(_clean_profile_text(text), 180) for text in messages[-self._config.max_messages_per_user :]],
            "existing_summary": _clip(existing.summary, 180) if existing else "",
        }
        system = (
            "你是群聊人物画像抽取器。只根据给出的低敏群聊片段，输出严格 JSON："
            "{aliases:[string],summary:string,preferences:[string]}。"
            "aliases 只保留群内称呼或外号，不要编造身份；summary 只写表达习惯、兴趣主题和互动方式；"
            "preferences 只写明确表达的偏好。不要输出 QQ 号、手机号、住址、学校、公司、财务、政治或敏感推断。"
        )
        try:
            reply = await generate(
                [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                max_tokens=360,
                reasoning_effort=None,
            )
            payload = _parse_json_object(getattr(reply, "text", ""))
            summary = _safe_model_text(payload.get("summary"), fallback_summary, self._safety_service, 260)
            preferences = _safe_model_list(payload.get("preferences"), self._safety_service, 220)
            aliases = _safe_model_aliases(payload.get("aliases"), self._safety_service)
            return summary, "；".join(preferences), set(aliases)
        except Exception:
            return fallback_summary, fallback_preferences, set()


def hashing_embedding(text: str) -> list[float]:
    values = [0.0] * VECTOR_DIMENSION
    normalized = _clean_profile_text(text)
    grams = set()
    for token in normalized.split():
        grams.add(token)
        grams.update(token[index : index + 2] for index in range(max(0, len(token) - 1)))
    for gram in grams:
        digest = int.from_bytes(hashlib.sha256(gram.encode('utf-8')).digest()[:4], 'big') % VECTOR_DIMENSION
        values[digest] += 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 7) for value in values]


def _cosine(left: tuple[float, ...] | list[float], right: tuple[float, ...] | list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def _clean_profile_text(value: str) -> str:
    text = re.sub(r"\[CQ:[^\]]+\]", " ", str(value or ""))
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", text)
    return re.sub(r"\s+", " ", text).strip(" ：:，,。. ")


def _clean_display_name(value: str | None) -> str | None:
    text = re.sub(r"[\r\n\t]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:32] or None


def _extract_self_aliases(text: str) -> set[str]:
    return {match.group(1).strip("，。！？!?、:：") for match in _ALIAS_PATTERN.finditer(text or "")}


def _profile_text(display_name: str | None, aliases: set[str], summary: str, preferences: str) -> str:
    parts = []
    if display_name:
        parts.append(f"显示名 {display_name}")
    if aliases:
        parts.append("称呼 " + " ".join(sorted(aliases)))
    if summary:
        parts.append(summary)
    if preferences:
        parts.append(preferences)
    return _clean_profile_text(" ".join(parts))[:800]


def _basic_summary(count: int, text: str) -> str:
    normalized = _clean_profile_text(text)
    average = len(normalized) / max(1, count)
    return "以短句交流" if average <= 16 else "通常使用较完整句子交流"


def _safe_profile_text(value: str, safety_service: SafetyService) -> bool:
    return bool(value and len(value) <= 40 and safety_service.can_store_long_term_memory(value))


def _safe_model_text(value: object, fallback: str, safety_service: SafetyService, limit: int) -> str:
    text = _clean_profile_text(str(value or ""))[:limit]
    return text if text and safety_service.can_store_long_term_memory(text) else fallback


def _safe_model_list(value: object, safety_service: SafetyService, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _clean_profile_text(str(item))[:80]
        if text and safety_service.can_store_long_term_memory(text):
            result.append(text)
    return "；".join(result)[:limit].split("；") if result else []


def _safe_model_aliases(value: object, safety_service: SafetyService) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        _clean_display_name(str(item))
        for item in value
        if _clean_display_name(str(item)) and _safe_profile_text(str(item), safety_service)
    }


def _parse_json_object(text: str) -> dict[str, object]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").replace("json", "", 1).strip()
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."
