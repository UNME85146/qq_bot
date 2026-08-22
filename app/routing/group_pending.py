from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import GroupPendingQuestion, NormalizedMessage
from app.storage.repositories import GroupPendingQuestionRepository


QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "呢",
    "怎么",
    "为啥",
    "为什么",
    "咋",
    "如何",
    "能不能",
    "可以",
    "是不是",
    "api",
    "API",
    "报错",
    "error",
    "Error",
)


@dataclass(frozen=True)
class PendingQuestionTarget:
    user_id: str
    user_name: str
    message_id: str
    question_text: str
    pending_id: int | None = None
    is_current: bool = False


class GroupPendingQuestionService:
    def __init__(
        self,
        *,
        repository: GroupPendingQuestionRepository,
        max_question_length: int = 220,
    ) -> None:
        self._repository = repository
        self._max_question_length = max_question_length

    async def maybe_enqueue(self, message: NormalizedMessage) -> GroupPendingQuestion | None:
        if message.group_id is None:
            return None
        if not is_question_like(message.text):
            return None
        cleaned = clean_question_text(message.text, max_length=self._max_question_length)
        if len(cleaned) < 2:
            return None
        return await self._repository.upsert_pending(
            group_id=message.group_id,
            user_id=message.user_id,
            user_name=message.user_name,
            message_id=message.message_id,
            question_text=cleaned,
        )

    async def select_targets(
        self,
        trigger_message: NormalizedMessage,
        *,
        limit: int = 3,
        include_current_non_question: bool = True,
    ) -> list[PendingQuestionTarget]:
        if trigger_message.group_id is None:
            return []
        targets: list[PendingQuestionTarget] = []
        current = await self.maybe_enqueue(trigger_message)
        if current is not None:
            targets.append(_target_from_pending(current, is_current=True))
        elif include_current_non_question and trigger_message.text.strip():
            targets.append(
                PendingQuestionTarget(
                    user_id=trigger_message.user_id,
                    user_name=trigger_message.user_name,
                    message_id=trigger_message.message_id,
                    question_text=clean_question_text(
                        trigger_message.text,
                        max_length=self._max_question_length,
                    ),
                    pending_id=None,
                    is_current=True,
                )
            )

        seen_message_ids = {target.message_id for target in targets}
        pending = await self._repository.list_pending(
            trigger_message.group_id,
            limit=max(limit + len(seen_message_ids), limit),
        )
        for question in pending:
            if question.message_id in seen_message_ids:
                continue
            targets.append(_target_from_pending(question, is_current=False))
            seen_message_ids.add(question.message_id)
            if len(targets) >= limit:
                break
        return targets[:limit]

    async def mark_answered(self, target: PendingQuestionTarget) -> None:
        if target.pending_id is None:
            return
        await self._repository.mark_answered(target.pending_id)


def is_question_like(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    if len(compact) < 2:
        return False
    return any(marker in compact for marker in QUESTION_MARKERS)


def clean_question_text(text: str, *, max_length: int) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", text)
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


def _target_from_pending(
    question: GroupPendingQuestion,
    *,
    is_current: bool,
) -> PendingQuestionTarget:
    return PendingQuestionTarget(
        user_id=question.user_id,
        user_name=question.user_name,
        message_id=question.message_id,
        question_text=question.question_text,
        pending_id=question.id,
        is_current=is_current,
    )
