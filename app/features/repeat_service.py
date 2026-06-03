from __future__ import annotations

import re
from dataclasses import dataclass

from app.features.presence_service import BotPresenceService
from app.models import GroupMessageIndex, NormalizedMessage, QQConfig, StickerAsset
from app.routing.permission_service import PermissionService
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    GroupMessageIndexRepository,
    MessageRepeatStateRepository,
    StickerAssetRepository,
)


PLUS_ONE_TEXTS = {"+1", "＋1", "加一"}


@dataclass(frozen=True)
class RepeatCandidate:
    group_id: str
    message_id: str
    user_id: str
    text: str
    sticker_asset: StickerAsset | None
    repeat_kind: str


class RepeatService:
    def __init__(
        self,
        *,
        message_index_repository: GroupMessageIndexRepository,
        repeat_state_repository: MessageRepeatStateRepository,
        sticker_repository: StickerAssetRepository,
        presence_service: BotPresenceService,
        safety_service: SafetyService,
        qq_config: QQConfig,
    ) -> None:
        self._message_index_repository = message_index_repository
        self._repeat_state_repository = repeat_state_repository
        self._sticker_repository = sticker_repository
        self._presence_service = presence_service
        self._safety_service = safety_service
        self._permission_service = PermissionService(qq_config)
        self._self_id = qq_config.self_id

    async def index_group_message(
        self,
        message: NormalizedMessage,
        *,
        sticker_asset_id: str | None = None,
        is_bot: bool = False,
    ) -> None:
        if message.group_id is None:
            return
        media_type = "sticker" if sticker_asset_id else _media_type(message)
        await self._message_index_repository.upsert(
            group_id=message.group_id,
            message_id=message.message_id,
            user_id=message.user_id,
            user_name=message.user_name,
            text=message.text,
            media_type=media_type,
            sticker_asset_id=sticker_asset_id,
            is_bot=is_bot,
        )

    async def candidate_from_plus_one_text(
        self,
        message: NormalizedMessage,
    ) -> RepeatCandidate | None:
        if message.group_id is None or not is_plus_one_text(message.text):
            return None
        if message.reply_to_message_id:
            indexed = await self._message_index_repository.get(
                message.group_id,
                message.reply_to_message_id,
            )
        else:
            indexed = await self._message_index_repository.recent_repeatable(message.group_id)
        return await self._candidate_from_index(indexed)

    async def candidate_from_notice(
        self,
        *,
        group_id: str,
        message_id: str,
    ) -> RepeatCandidate | None:
        indexed = await self._message_index_repository.get(group_id, message_id)
        return await self._candidate_from_index(indexed)

    async def maybe_mark_repeated(
        self,
        *,
        trigger_message: NormalizedMessage,
        candidate: RepeatCandidate,
        plus_one: bool,
    ) -> bool:
        if not self._permission_service.is_group_allowed(candidate.group_id):
            return False
        if candidate.user_id == self._self_id:
            return False
        if not self._presence_service.should_repeat(
            trigger_message,
            repeat_kind=candidate.repeat_kind,
            plus_one=plus_one,
        ):
            return False
        return await self._repeat_state_repository.try_mark_repeated(
            group_id=candidate.group_id,
            source_message_id=candidate.message_id,
            repeat_kind=candidate.repeat_kind,
            repeated_by=self._self_id,
            trigger_user_id=trigger_message.user_id,
        )

    async def _candidate_from_index(
        self,
        indexed: GroupMessageIndex | None,
    ) -> RepeatCandidate | None:
        if indexed is None or indexed.is_bot:
            return None
        sticker = await self._sticker_repository.get_by_asset_id(indexed.sticker_asset_id)
        if sticker is not None:
            return RepeatCandidate(
                group_id=indexed.group_id,
                message_id=indexed.message_id,
                user_id=indexed.user_id,
                text="",
                sticker_asset=sticker,
                repeat_kind="sticker",
            )
        if not is_repeatable_text(indexed.text, self._safety_service):
            return None
        return RepeatCandidate(
            group_id=indexed.group_id,
            message_id=indexed.message_id,
            user_id=indexed.user_id,
            text=indexed.text,
            sticker_asset=None,
            repeat_kind="text",
        )


def is_plus_one_text(text: str) -> bool:
    compact = "".join(text.split())
    return compact in PLUS_ONE_TEXTS


def is_repeatable_text(text: str, safety_service: SafetyService) -> bool:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 60:
        return False
    if cleaned.startswith("/"):
        return False
    if "http://" in cleaned.lower() or "https://" in cleaned.lower():
        return False
    if re.search(r"\[CQ:[^\]]+\]", cleaned):
        return False
    if not safety_service.can_store_long_term_memory(cleaned):
        return False
    check = safety_service.check_input(cleaned, scope_type="group")
    return check.action == "allow"


def _media_type(message: NormalizedMessage) -> str:
    if any(item.type == "image" for item in message.media_items):
        return "image"
    if any(item.type == "face" for item in message.media_items):
        return "face"
    return ""
