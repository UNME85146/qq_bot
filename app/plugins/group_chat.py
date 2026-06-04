from __future__ import annotations

import asyncio
import os
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import replace

from loguru import logger
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from app.adapters.onebot_event_adapter import normalize_group_message_event
from app.bootstrap import create_conversation_service
from app.config import load_config
from app.features.reminder_service import (
    is_explicit_reminder_request,
    is_reminder_command,
)
from app.features.repeat_service import is_plus_one_text
from app.features.runtime_features import (
    create_runtime_feature_hub,
    maybe_save_sticker,
    reminder_worker,
)
from app.features.sticker_service import is_sticker_request
from app.models import MediaItem, NormalizedMessage
from app.plugins.send_helper import send_group_image_direct, send_reply_bubbles
from app.routing.group_mute import (
    is_group_mute_enable_command,
    should_group_mute_wake_for_message,
)
from app.routing.group_pending import GroupPendingQuestionService, PendingQuestionTarget
from app.routing.permission_service import PermissionService
from app.routing.rate_limiter import RateLimiter
from app.routing.group_trigger import contains_nickname, nickname_probability_passes
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    BotSentMessageRepository,
    GroupMuteStateRepository,
    GroupPendingQuestionRepository,
)

_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))
_conversation_service = create_conversation_service(_config)
_feature_hub = create_runtime_feature_hub(_config)
_permission_service = PermissionService(_config.qq)
_group_mute_repository = GroupMuteStateRepository(_config.storage.database_path)
_bot_sent_repository = BotSentMessageRepository(_config.storage.database_path)
_pending_question_repository = GroupPendingQuestionRepository(_config.storage.database_path)
_pending_question_service = GroupPendingQuestionService(
    repository=_pending_question_repository,
    safety_service=SafetyService(
        identity_disclosure=_config.persona.style_profile.identity_disclosure,
        source_user_id=_config.persona.style_profile.source_user_id,
    ),
)
_rate_limiter = RateLimiter(
    _config.limits.group_cooldown_seconds,
    private_cooldown_seconds=_config.limits.private_cooldown_seconds,
    max_user_messages_per_minute=_config.limits.max_user_messages_per_minute,
    max_group_messages_per_minute=_config.limits.max_group_messages_per_minute,
)
_active_windows: dict[tuple[str, str], float] = {}
_group_reply_queues: dict[str, asyncio.Queue["GroupReplyTask"]] = {}
_group_reply_workers: dict[str, asyncio.Task] = {}
_group_last_sent_at: dict[str, float] = {}
_group_recent_thread_events: list[dict[str, str | float | int]] = []
_group_recent_media_by_message_id: OrderedDict[str, tuple[MediaItem, ...]] = OrderedDict()
GROUP_REPLY_INTERVAL_SECONDS = 1.5
GROUP_INTERJECTION_PROBABILITY = 0.15
MAX_RECENT_MEDIA_MESSAGES = 200
FOLLOW_UP_MARKERS = (
    "那",
    "所以",
    "怎么",
    "为啥",
    "为什么",
    "继续",
    "刚才",
    "这个",
    "你说的",
    "然后",
    "咋",
)
BACKFILL_MARKERS = (
    "补答",
    "回答上面的问题",
    "把刚才的问题答一下",
    "把上面的问题答一下",
    "上面的问题",
)
_reminder_worker_started = False


@dataclass(frozen=True)
class GroupReplyTask:
    bot: Bot
    event: GroupMessageEvent
    message: NormalizedMessage
    thread_key: str
    reason: str
    queued_at: float
    include_pending_backfill: bool = False

group_chat = on_message(priority=10, block=False)


async def _start_reminder_worker(bot: Bot) -> None:
    global _reminder_worker_started
    if _reminder_worker_started:
        return
    _reminder_worker_started = True
    asyncio.create_task(reminder_worker(bot, _feature_hub))


try:
    get_driver().on_bot_connect(_start_reminder_worker)
except ValueError:
    pass


@group_chat.handle()
async def _handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    normalized = normalize_group_message_event(event)
    if normalized is None:
        return
    reply_to_bot = await _bot_sent_repository.is_bot_sent_message(
        normalized.reply_to_message_id
    )
    trigger_reason = _group_trigger_reason(normalized)
    trigger_reason = _apply_reply_thread_trigger(
        normalized,
        trigger_reason,
        reply_to_bot=reply_to_bot,
    )
    normalized = replace(normalized, trigger_reason=trigger_reason)

    if not _permission_service.is_group_allowed(normalized.group_id or ""):
        reply = await _conversation_service.handle_group_message(normalized)
        if reply is None:
            logger.info(
                "Group message ignored: group_id={}, user_id={}, is_at_self={}",
                normalized.group_id,
                normalized.user_id,
                normalized.is_at_self,
        )
        return

    _remember_message_media(normalized)
    normalized = _with_referenced_media(normalized)
    sticker_asset_id = await maybe_save_sticker(_feature_hub, normalized)
    await _feature_hub.repeats.index_group_message(
        normalized,
        sticker_asset_id=sticker_asset_id,
    )

    pending_question = await _pending_question_service.maybe_enqueue(normalized)
    if pending_question is not None and trigger_reason is None:
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_thread_pending",
            model_called=False,
            safety_blocked=False,
        )

    mute_wake_triggered = False
    if is_group_mute_enable_command(normalized, _config.qq):
        await _group_mute_repository.set_muted(
            group_id=normalized.group_id or "",
            muted=True,
            updated_by=normalized.user_id,
            reason="group_mute_enabled",
        )
        await _conversation_service.record_silent_group_message(
            normalized,
            reason="group_mute_enabled",
        )
        logger.info(
            "Group muted by controller: group_id={}, user_id={}",
            normalized.group_id,
            normalized.user_id,
        )
        return

    mute_state = (
        await _group_mute_repository.get_by_group_id(normalized.group_id)
        if normalized.group_id is not None
        else None
    )
    if mute_state is not None and mute_state.muted:
        if should_group_mute_wake_for_message(normalized, _config.qq):
            await _group_mute_repository.set_muted(
                group_id=normalized.group_id or "",
                muted=False,
                updated_by=normalized.user_id,
                reason="group_mute_disabled",
            )
            await _conversation_service.record_system_event(
                level="INFO",
                event="group_mute_disabled",
                detail=f"group_id={normalized.group_id}; updated_by={normalized.user_id}",
                trace_id=normalized.trace_id,
            )
            mute_wake_triggered = True
            logger.info(
                "Group mute disabled by controller: group_id={}, user_id={}",
                normalized.group_id,
                normalized.user_id,
            )
        else:
            await _conversation_service.record_silent_group_message(
                normalized,
                reason="group_muted",
            )
            logger.info(
                "Group message ignored by mute: group_id={}, user_id={}",
                normalized.group_id,
                normalized.user_id,
            )
            return

    if is_reminder_command(normalized.text):
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_reminder_disabled",
            model_called=False,
            safety_blocked=False,
        )
        return

    if is_explicit_reminder_request(normalized.text):
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_reminder_disabled",
            model_called=False,
            safety_blocked=False,
        )
        return

    if is_plus_one_text(normalized.text):
        repeated = await _try_repeat_from_plus_one_text(bot, normalized)
        await _conversation_service.record_reply_audit(
            normalized,
            action="reply" if repeated else "silence",
            reason="plus_one_repeat_sent" if repeated else "plus_one_repeat_skipped",
            model_called=False,
            safety_blocked=False,
        )
        return

    if is_sticker_request(normalized.text):
        sent = await _send_context_sticker(bot, normalized)
        if not sent:
            sent = await _send_context_sticker_missing_text(bot, event, normalized)
        await _conversation_service.record_reply_audit(
            normalized,
            action="reply" if sent else "silence",
            reason="context_sticker_sent" if sent else "context_sticker_missing",
            model_called=False,
            safety_blocked=False,
        )
        return

    if _should_try_probabilistic_repeat(
        normalized,
        trigger_reason=trigger_reason,
        pending_question=pending_question,
    ):
        repeated = await _try_probabilistic_repeat(bot, normalized)
        if repeated:
            await _conversation_service.record_reply_audit(
                normalized,
                action="reply",
                reason="probabilistic_repeat_sent",
                model_called=False,
                safety_blocked=False,
            )
            return

    if trigger_reason == "nickname_probability_skipped":
        await _conversation_service.handle_group_message(normalized)
        logger.info(
            "Group message ignored by nickname probability: group_id={}, user_id={}",
            normalized.group_id,
            normalized.user_id,
        )
        return

    if trigger_reason is None:
        if _inside_active_window(normalized) and _is_follow_up_text(normalized.text):
            normalized = replace(normalized, trigger_reason="active_window")
        elif contains_nickname(normalized.text.strip(), _config.qq.nicknames):
            await _conversation_service.record_reply_audit(
                normalized,
                action="silence",
                reason="group_interjection_skipped",
                model_called=False,
                safety_blocked=False,
            )
            logger.info(
                "Group interjection skipped: group_id={}, user_id={}",
                normalized.group_id,
                normalized.user_id,
            )
            return
        else:
            reply = await _conversation_service.handle_group_message(normalized)
            if reply is None:
                logger.info(
                    "Group message ignored: group_id={}, user_id={}, is_at_self={}",
                    normalized.group_id,
                    normalized.user_id,
                    normalized.is_at_self,
                )
            return

    if (
        not mute_wake_triggered
        and normalized.group_id is not None
        and not _rate_limiter.allow_group_minute(normalized.group_id)
    ):
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_minute_rate_limited",
            model_called=False,
            safety_blocked=False,
        )
        logger.info("Group message ignored by minute limit: group_id={}", normalized.group_id)
        return

    if not mute_wake_triggered and not _rate_limiter.allow_user_minute(normalized.user_id):
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="user_minute_rate_limited",
            model_called=False,
            safety_blocked=False,
        )
        logger.info("Group message ignored by user minute limit: user_id={}", normalized.user_id)
        return

    await _enqueue_group_reply(
        GroupReplyTask(
            bot=bot,
            event=event,
            message=normalized,
            thread_key=_thread_key(normalized),
            reason=normalized.trigger_reason or "group_mention",
            queued_at=time.monotonic(),
            include_pending_backfill=_is_backfill_request(normalized.text),
        )
    )
    await _conversation_service.record_reply_audit(
        normalized,
        action="reply",
        reason="group_reply_queued",
        model_called=False,
        safety_blocked=False,
    )


def _group_trigger_reason(normalized) -> str | None:
    if normalized.is_at_self:
        return "group_mention"
    if _inside_active_window(normalized) and _is_follow_up_text(normalized.text):
        return "active_window"
    text = normalized.text.strip()
    if contains_nickname(text, _config.qq.nicknames):
        if nickname_probability_passes(_config.reply.nickname_reply_probability):
            return "nickname_trigger"
        return "nickname_probability_skipped"
    return None


def _apply_reply_thread_trigger(
    normalized,
    trigger_reason: str | None,
    *,
    reply_to_bot: bool,
) -> str | None:
    if not reply_to_bot:
        return trigger_reason
    if normalized.is_at_self:
        return "reply_to_bot_mention"
    return None


async def _enqueue_group_reply(task: GroupReplyTask) -> None:
    group_id = task.message.group_id
    if group_id is None:
        return
    queue = _group_reply_queues.setdefault(group_id, asyncio.Queue())
    await queue.put(task)
    _remember_thread_event(task, queue.qsize())
    worker = _group_reply_workers.get(group_id)
    if worker is None or worker.done():
        _group_reply_workers[group_id] = asyncio.create_task(_group_reply_worker(group_id))


async def _group_reply_worker(group_id: str) -> None:
    queue = _group_reply_queues[group_id]
    while True:
        task = await queue.get()
        try:
            await _wait_group_interval(group_id)
            await _process_group_reply_task(task)
        except Exception:
            logger.exception("Group reply worker failed: group_id={}", group_id)
            await _conversation_service.record_system_event(
                level="ERROR",
                event="group_reply_worker_failed",
                detail=f"group_id={group_id}",
                trace_id=task.message.trace_id,
            )
        finally:
            queue.task_done()


async def _wait_group_interval(group_id: str) -> None:
    last_sent_at = _group_last_sent_at.get(group_id)
    if last_sent_at is None:
        return
    wait_seconds = GROUP_REPLY_INTERVAL_SECONDS - (time.monotonic() - last_sent_at)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


async def _process_group_reply_task(task: GroupReplyTask) -> None:
    message = task.message
    await _conversation_service.record_reply_audit(
        message,
        action="reply",
        reason="group_reply_dequeued",
        model_called=False,
        safety_blocked=False,
    )
    targets = await _targets_for_task(task)
    for target in targets:
        target_message = replace(
            message,
            user_id=target.user_id,
            user_name=target.user_name,
            message_id=target.message_id,
            text=target.question_text,
            trigger_reason=task.reason,
        )
        if target_message.media_items:
            reply = await _conversation_service.handle_group_image_message(
                target_message,
                reason=task.reason,
            )
        else:
            reply = await _conversation_service.handle_group_question(
                target_message,
                question_text=target.question_text,
                question_user_name=target.user_name,
                reason=task.reason,
            )
        if reply is None:
            logger.info(
                "Group queued reply produced no message: group_id={}, user_id={}, message_id={}",
                message.group_id,
                target.user_id,
                target.message_id,
            )
            await _pending_question_service.mark_answered(target)
            continue

        if message.group_id is not None:
            _active_windows[(message.group_id, target.user_id)] = (
                time.monotonic() + _config.reply.active_window_seconds
            )

        await send_reply_bubbles(
            task.bot,
            task.event,
            reply.text,
            scope_type="group",
            reply_config=_config.reply,
            group_reply_to_message_id=target.message_id,
            group_at_user_id=target.user_id,
            on_send_error=lambda exc, index, bubble: _record_send_error(
                message.trace_id,
                exc,
                index,
            ),
            on_sent=lambda index, bubble, sent_message_id, target=target: _record_sent_group_message(
                message.trace_id,
                message.group_id,
                message.self_id,
                target,
                sent_message_id,
                index,
            ),
        )
        if message.group_id is not None:
            _group_last_sent_at[message.group_id] = time.monotonic()
            _feature_hub.focus_group(message.group_id)
        await _pending_question_service.mark_answered(target)
        if target is not targets[-1] and message.group_id is not None:
            await _wait_group_interval(message.group_id)


async def _targets_for_task(task: GroupReplyTask):
    if task.include_pending_backfill:
        return await _pending_backfill_targets(task.message)
    if task.message.media_items:
        return [_single_message_target(task.message)]
    current = await _pending_question_service.maybe_enqueue(task.message)
    if current is not None:
        return [
            PendingQuestionTarget(
                user_id=current.user_id,
                user_name=current.user_name,
                message_id=current.message_id,
                question_text=current.question_text,
                pending_id=current.id,
                is_current=True,
            )
        ]
    if task.message.text.strip():
        return [_single_message_target(task.message)]
    return []


async def _pending_backfill_targets(message: NormalizedMessage) -> list[PendingQuestionTarget]:
    if message.group_id is None:
        return [_single_message_target(message)]
    pending = await _pending_question_repository.list_pending(message.group_id, limit=4)
    targets = [
        PendingQuestionTarget(
            user_id=question.user_id,
            user_name=question.user_name,
            message_id=question.message_id,
            question_text=question.question_text,
            pending_id=question.id,
            is_current=False,
        )
        for question in pending
        if question.message_id != message.message_id
    ][:3]
    return targets or [_single_message_target(message)]


def _inside_active_window(normalized) -> bool:
    if normalized.group_id is None:
        return False
    expires_at = _active_windows.get((normalized.group_id, normalized.user_id))
    return expires_at is not None and time.monotonic() < expires_at


def _is_follow_up_text(text: str) -> bool:
    compact = "".join(text.split()).lower()
    if not compact:
        return False
    return any(marker.lower() in compact for marker in FOLLOW_UP_MARKERS)


def _is_backfill_request(text: str) -> bool:
    compact = "".join(text.split())
    return any(marker in compact for marker in BACKFILL_MARKERS)


def _should_try_probabilistic_repeat(
    normalized: NormalizedMessage,
    *,
    trigger_reason: str | None,
    pending_question,
) -> bool:
    if trigger_reason is not None:
        return False
    if pending_question is not None:
        return False
    return bool(normalized.media_items or normalized.text.strip())


def _thread_key(normalized) -> str:
    return normalized.reply_to_message_id or normalized.message_id


def group_queue_status() -> dict[str, object]:
    now = time.monotonic()
    return {
        "queues": {
            group_id: {
                "size": queue.qsize(),
                "workerRunning": bool(
                    _group_reply_workers.get(group_id)
                    and not _group_reply_workers[group_id].done()
                ),
                "oldestWaitSeconds": _oldest_queue_wait_seconds(queue, now),
            }
            for group_id, queue in _group_reply_queues.items()
        },
        "recentThreadEvents": list(_group_recent_thread_events[-5:]),
    }


def _oldest_queue_wait_seconds(queue: asyncio.Queue[GroupReplyTask], now: float) -> float:
    queued = list(getattr(queue, "_queue", []))
    if not queued:
        return 0.0
    return round(max(0.0, max(now - task.queued_at for task in queued)), 1)


def _remember_thread_event(task: GroupReplyTask, queue_size: int) -> None:
    _group_recent_thread_events.append(
        {
            "group_id": task.message.group_id or "",
            "thread_key": task.thread_key,
            "reason": task.reason,
            "user_id": task.message.user_id,
            "queue_size": queue_size,
            "queued_at": task.queued_at,
        }
    )
    del _group_recent_thread_events[:-20]


def _remember_message_media(message: NormalizedMessage) -> None:
    if not message.media_items:
        return
    _group_recent_media_by_message_id[message.message_id] = message.media_items
    _group_recent_media_by_message_id.move_to_end(message.message_id)
    while len(_group_recent_media_by_message_id) > MAX_RECENT_MEDIA_MESSAGES:
        _group_recent_media_by_message_id.popitem(last=False)


def _with_referenced_media(message: NormalizedMessage) -> NormalizedMessage:
    if message.media_items or not message.reply_to_message_id:
        return message
    media_items = _group_recent_media_by_message_id.get(message.reply_to_message_id)
    if not media_items:
        return message
    return replace(message, media_items=media_items)


def _single_message_target(normalized):
    return PendingQuestionTarget(
        user_id=normalized.user_id,
        user_name=normalized.user_name,
        message_id=normalized.message_id,
        question_text=normalized.text,
        pending_id=None,
        is_current=True,
    )


async def _try_repeat_from_plus_one_text(bot: Bot, message: NormalizedMessage) -> bool:
    candidate = await _feature_hub.repeats.candidate_from_plus_one_text(message)
    if candidate is None:
        return False
    marked = await _feature_hub.repeats.maybe_mark_repeated(
        trigger_message=message,
        candidate=candidate,
        plus_one=True,
    )
    if not marked:
        return False
    return await _send_repeat_candidate(bot, candidate, trace_id=message.trace_id)


async def _send_context_sticker(bot: Bot, message: NormalizedMessage) -> bool:
    if message.group_id is None:
        return False
    asset = await _feature_hub.stickers.choose_for_text(message.text)
    if asset is None:
        return False
    try:
        await asyncio.sleep(_repeat_delay_seconds())
        await send_group_image_direct(
            bot,
            group_id=message.group_id,
            file_path=asset.file_path,
        )
        await _feature_hub.stickers.mark_used(asset.asset_id)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="send_context_sticker_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=message.trace_id,
        )
        return False
    return True


async def _send_context_sticker_missing_text(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> bool:
    failed = False

    async def on_send_error(exc, index, bubble) -> None:
        nonlocal failed
        failed = True
        await _record_send_error(
            message.trace_id,
            exc,
            index,
            "send_group_reply_failed",
        )

    await send_reply_bubbles(
        bot,
        event,
        "没有",
        scope_type="group",
        reply_config=_config.reply,
        group_reply_to_message_id=message.message_id,
        group_at_user_id=message.user_id,
        on_send_error=on_send_error,
    )
    return not failed


async def _try_probabilistic_repeat(bot: Bot, message: NormalizedMessage) -> bool:
    if message.group_id is None:
        return False
    indexed = await _feature_hub.repeats.candidate_from_probabilistic_message(message)
    if indexed is None:
        await _conversation_service.record_system_event(
            level="INFO",
            event="probabilistic_repeat_skipped",
            detail=f"group_id={message.group_id}; message_id={message.message_id}; reason=no_candidate",
            trace_id=message.trace_id,
        )
        return False
    marked = await _feature_hub.repeats.maybe_mark_repeated(
        trigger_message=message,
        candidate=indexed,
        plus_one=False,
    )
    if not marked:
        await _conversation_service.record_system_event(
            level="INFO",
            event="probabilistic_repeat_skipped",
            detail=f"group_id={message.group_id}; message_id={message.message_id}; kind={indexed.repeat_kind}; reason=presence_or_duplicate",
            trace_id=message.trace_id,
        )
        return False
    return await _send_repeat_candidate(bot, indexed, trace_id=message.trace_id)


async def _send_repeat_candidate(bot: Bot, candidate, *, trace_id: str) -> bool:
    try:
        await asyncio.sleep(_repeat_delay_seconds())
        if candidate.sticker_asset is not None:
            await send_group_image_direct(
                bot,
                group_id=candidate.group_id,
                file_path=candidate.sticker_asset.file_path,
            )
            await _feature_hub.stickers.mark_used(candidate.sticker_asset.asset_id)
        else:
            await bot.send_group_msg(group_id=int(candidate.group_id), message=candidate.text)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="probabilistic_repeat_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=trace_id,
        )
        return False
    await _conversation_service.record_system_event(
        level="INFO",
        event="probabilistic_repeat_sent",
        detail=f"group_id={candidate.group_id}; message_id={candidate.message_id}; kind={candidate.repeat_kind}",
        trace_id=trace_id,
    )
    return True


def _repeat_delay_seconds() -> float:
    return random.uniform(0.3, 1.2)


async def _record_send_error(trace_id: str, exc: Exception, index: int) -> None:
    await _conversation_service.record_system_event(
        level="ERROR",
        event="send_group_reply_failed",
        detail=f"{type(exc).__name__}: bubble_index={index}; {str(exc)[:120]}",
        trace_id=trace_id,
    )
    logger.exception("Failed to send group reply bubble: trace_id={}, index={}", trace_id, index)


async def _record_sent_group_message(
    trace_id: str,
    group_id: str | None,
    self_id: str,
    target,
    sent_message_id: str | None,
    index: int,
) -> None:
    if sent_message_id is None or group_id is None:
        return
    await _bot_sent_repository.save_group_message(
        message_id=sent_message_id,
        trace_id=trace_id,
        group_id=group_id,
        user_id=self_id,
        original_message_id=target.message_id,
    )
