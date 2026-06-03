from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.models import PersonaState
from app.storage.repositories import PersonaStateRepository


class PersonaStateService:
    def __init__(self, repository: PersonaStateRepository) -> None:
        self._repository = repository

    async def get_or_create(self, scope_type: str, scope_id: str) -> PersonaState:
        return await self._repository.get_or_create(scope_type, scope_id)

    async def record_successful_reply(
        self,
        scope_type: str,
        scope_id: str,
    ) -> PersonaState:
        current = await self._repository.get_or_create(scope_type, scope_id)
        trust = min(100, current.trust + 1)
        updated = replace(
            current,
            trust=trust,
            relationship_stage=_relationship_stage_for_trust(trust),
            last_interaction_at=datetime.now(UTC).isoformat(),
        )
        return await self._repository.save(updated)


def _relationship_stage_for_trust(trust: int) -> str:
    if trust >= 80:
        return "close"
    if trust >= 50:
        return "familiar"
    return "stranger"

