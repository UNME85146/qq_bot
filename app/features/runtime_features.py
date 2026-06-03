from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime

from loguru import logger
from nonebot.adapters.onebot.v11 import Bot

from app.features.reminder_service import ReminderService
from app.features.repeat_service import RepeatService
from app.features.sticker_service import StickerService
from app.features.presence_service import BotPresenceService
from app.models import AppConfig, NormalizedMessage, ScheduledTask
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    AuditRepository,
    GroupMessageIndexRepository,
    MessageRepeatStateRepository,
    ScheduledTaskRepository,
    StickerAssetRepository,
)


class RuntimeFeatureHub:
    def __init__(
        self,
        *,
        config: AppConfig,
        reminder_service: ReminderService,
        sticker_service: StickerService,
        repeat_service: RepeatService,
        presence_service: BotPresenceService,
        scheduled_task_repository: ScheduledTaskRepository,
        audit_repository: AuditRepository,
    ) -> None:
        self.config = config
        self.reminders = reminder_service
        self.stickers = sticker_service
        self.repeats = repeat_service
        self.presence = presence_service
        self.scheduled_tasks = scheduled_task_repository
        self.audit_repository = audit_repository

    async def record_system_event(
        self,
        *,
        level: str,
        event: str,
        detail: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        await self.audit_repository.save_system_event(
            level=level,
            event=event,
            detail=detail,
            trace_id=trace_id,
        )

    def focus_group(self, group_id: str | None) -> None:
        self.presence.focus_group(group_id)


def create_runtime_feature_hub(config: AppConfig) -> RuntimeFeatureHub:
    safety_service = SafetyService(
        identity_disclosure=config.persona.style_profile.identity_disclosure,
        source_user_id=config.persona.style_profile.source_user_id,
    )
    scheduled_tasks = ScheduledTaskRepository(config.storage.database_path)
    stickers = StickerAssetRepository(config.storage.database_path)
    message_index = GroupMessageIndexRepository(config.storage.database_path)
    repeat_states = MessageRepeatStateRepository(config.storage.database_path)
    audit = AuditRepository(config.storage.database_path)
    presence = BotPresenceService(config.presence)
    data_dir = Path(config.storage.database_path).parent
    return RuntimeFeatureHub(
        config=config,
        reminder_service=ReminderService(
            repository=scheduled_tasks,
            qq_config=config.qq,
        ),
        sticker_service=StickerService(
            repository=stickers,
            qq_config=config.qq,
            root_dir=data_dir / "stickers",
            safety_service=safety_service,
        ),
        repeat_service=RepeatService(
            message_index_repository=message_index,
            repeat_state_repository=repeat_states,
            sticker_repository=stickers,
            presence_service=presence,
            safety_service=safety_service,
            qq_config=config.qq,
        ),
        presence_service=presence,
        scheduled_task_repository=scheduled_tasks,
        audit_repository=audit,
    )


async def reminder_worker(bot: Bot, hub: RuntimeFeatureHub, *, interval_seconds: int = 5) -> None:
    while True:
        try:
            now_iso = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            due_tasks = await hub.scheduled_tasks.list_pending_due(now_iso=now_iso, limit=20)
            for task in due_tasks:
                await _send_due_reminder(bot, hub, task)
        except Exception as exc:
            logger.exception("Reminder worker failed")
            await hub.record_system_event(
                level="ERROR",
                event="reminder_worker_failed",
                detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            )
        await asyncio.sleep(interval_seconds)


async def _send_due_reminder(bot: Bot, hub: RuntimeFeatureHub, task: ScheduledTask) -> None:
    if task.scope_type != "private":
        await hub.scheduled_tasks.mark_cancelled(task.id)
        await hub.record_system_event(
            level="INFO",
            event="scheduled_group_task_skipped",
            detail=f"task_id={task.id}; scope={task.scope_type}/{task.scope_id}",
        )
        return
    try:
        text = f"提醒：{task.message}"
        await bot.send_private_msg(user_id=int(task.scope_id), message=text)
    except Exception as exc:
        await hub.scheduled_tasks.mark_failed(task.id)
        await hub.record_system_event(
            level="ERROR",
            event="scheduled_task_send_failed",
            detail=f"task_id={task.id}; {type(exc).__name__}: {str(exc)[:120]}",
        )
        return
    await hub.scheduled_tasks.mark_completed(task.id)
    await hub.record_system_event(
        level="INFO",
        event="scheduled_task_sent",
        detail=f"task_id={task.id}; scope={task.scope_type}/{task.scope_id}",
    )


async def maybe_save_sticker(hub: RuntimeFeatureHub, message: NormalizedMessage) -> str | None:
    result = await hub.stickers.save_from_message(message)
    if result.asset is not None:
        await hub.record_system_event(
            level="INFO",
            event="sticker_saved",
            detail=f"asset_id={result.asset.asset_id[:12]}; scope={message.scope_type}/{message.scope_id}",
            trace_id=message.trace_id,
        )
        return result.asset.asset_id
    if result.reason not in {"no_media", "no_image_url"}:
        await hub.record_system_event(
            level="INFO",
            event="sticker_save_skipped",
            detail=f"reason={result.reason}; scope={message.scope_type}/{message.scope_id}",
            trace_id=message.trace_id,
        )
    return None
