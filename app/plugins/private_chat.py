from __future__ import annotations

import asyncio
import os

from loguru import logger
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent

from app.adapters.onebot_event_adapter import normalize_private_message_event
from app.bootstrap import create_conversation_service
from app.config import load_config
from app.conversation.reply_formatter import ReplyFormatter
from app.features.reminder_service import (
    format_reminder_tasks,
    is_explicit_reminder_request,
    is_reminder_cancel_command,
    is_reminder_command,
    is_reminder_list_command,
    parse_reminder_cancel_id,
)
from app.features.runtime_features import create_runtime_feature_hub, maybe_save_sticker
from app.features.sticker_service import is_sticker_save_request
from app.features.tts_service import (
    DEFAULT_VOICE_REPLY_DECIDER,
    EXACT_TTS_SEGMENT_MAX_CHARS,
    TTS_SEGMENT_MAX_CHARS,
    TTSService,
    extract_explicit_voice_read_text,
    forced_voice_tts_skip_reason,
    record_explicit_voice_selected,
    record_tts_fallback_text_sent,
    tts_enabled_for_scope,
    tts_scope_disabled_reason,
)
from app.models import GeneratedReply
from app.plugins.send_helper import (
    send_private_image_direct,
    send_private_record_direct,
    send_reply_bubbles,
)
from app.routing.direct_intent import DirectReplyIntent, parse_direct_reply_intent
from app.routing.permission_service import PermissionService
from app.routing.rate_limiter import RateLimiter
from app.safety.safety_service import SafetyService
from app.storage.database import init_database

_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))
_conversation_service = create_conversation_service(_config)
_feature_hub = create_runtime_feature_hub(_config)
_permission_service = PermissionService(_config.qq)
_rate_limiter = RateLimiter(
    _config.limits.group_cooldown_seconds,
    private_cooldown_seconds=_config.limits.private_cooldown_seconds,
    max_user_messages_per_minute=_config.limits.max_user_messages_per_minute,
    max_group_messages_per_minute=_config.limits.max_group_messages_per_minute,
)
_tts_service = TTSService(
    _config.tts,
    record_system_event=_conversation_service.record_system_event,
)
_voice_safety_service = SafetyService(
    identity_disclosure=_config.persona.style_profile.identity_disclosure,
    source_user_id=_config.persona.style_profile.source_user_id,
)
_voice_reply_decider = DEFAULT_VOICE_REPLY_DECIDER
_reply_formatter = ReplyFormatter(_config.reply.max_reply_length)
_recent_private_sticker_assets: dict[str, str] = {}
_recent_private_direct_actions: dict[str, str] = {}
_private_user_locks: dict[str, asyncio.Lock] = {}
_pending_private_greetings: set[str] = set()

private_chat = on_message(priority=10, block=False)


@get_driver().on_startup
async def _init_storage() -> None:
    await init_database(_config.storage.database_path)
    logger.info("SQLite storage initialized: {}", _config.storage.database_path)


@private_chat.handle()
async def _handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    normalized = normalize_private_message_event(event)
    if normalized is None:
        return
    if _is_duplicate_pending_greeting(normalized.user_id, normalized.text):
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="private_superseded_duplicate",
            model_called=False,
            safety_blocked=False,
        )
        return
    if _is_simple_greeting(normalized.text):
        _pending_private_greetings.add(normalized.user_id)
    try:
        async with _private_lock_for(normalized.user_id):
            await _handle_private_message_locked(bot, event, normalized)
    finally:
        _pending_private_greetings.discard(normalized.user_id)


async def _handle_private_message_locked(bot: Bot, event: PrivateMessageEvent, normalized) -> None:
    saved_sticker_asset_id: str | None = None
    direct_intent = None
    if _permission_service.is_private_user_allowed(normalized.user_id):
        saved_sticker_asset_id = await maybe_save_sticker(_feature_hub, normalized)
        direct_intent = parse_direct_reply_intent(normalized)
        direct_intent = _apply_private_followup_intent(
            normalized.user_id,
            normalized.text,
            direct_intent,
        )
        if saved_sticker_asset_id is not None:
            _recent_private_sticker_assets[normalized.user_id] = saved_sticker_asset_id
        if is_sticker_save_request(normalized.text):
            _clear_private_direct_action(normalized.user_id)
            await send_reply_bubbles(
                bot,
                event,
                _sticker_save_reply_text(
                    saved_sticker_asset_id=saved_sticker_asset_id,
                    recent_sticker_asset_id=_recent_private_sticker_assets.get(normalized.user_id),
                ),
                scope_type="private",
                reply_config=_config.reply,
                on_send_error=lambda exc, index, bubble: _record_send_error(
                    normalized.trace_id,
                    exc,
                    index,
                    "send_private_reply_failed",
                ),
            )
            return
        if is_reminder_command(normalized.text):
            _clear_private_direct_action(normalized.user_id)
            reply_text = await _handle_user_reminder_command(
                normalized.user_id,
                normalized.user_name,
                normalized.scope_id,
                normalized.text,
            )
            await send_reply_bubbles(
                bot,
                event,
                reply_text,
                scope_type="private",
                reply_config=_config.reply,
                on_send_error=lambda exc, index, bubble: _record_send_error(
                    normalized.trace_id,
                    exc,
                    index,
                    "send_private_reply_failed",
                ),
            )
            return
        reminder = await _feature_hub.reminders.try_create_from_message(normalized)
        if reminder is not None:
            _clear_private_direct_action(normalized.user_id)
            await send_reply_bubbles(
                bot,
                event,
                f"记下了，{reminder.due_at} 提醒你：{reminder.message}",
                scope_type="private",
                reply_config=_config.reply,
                on_send_error=lambda exc, index, bubble: _record_send_error(
                    normalized.trace_id,
                    exc,
                    index,
                    "send_private_reply_failed",
                ),
            )
            return
        if is_explicit_reminder_request(normalized.text):
            _clear_private_direct_action(normalized.user_id)
            await send_reply_bubbles(
                bot,
                event,
                _REMINDER_CREATE_HELP_TEXT,
                scope_type="private",
                reply_config=_config.reply,
                on_send_error=lambda exc, index, bubble: _record_send_error(
                    normalized.trace_id,
                    exc,
                    index,
                    "send_private_reply_failed",
                ),
            )
            return
        if direct_intent.sticker_request or direct_intent.sticker_battle_request:
            asset = await _choose_safe_sticker(
                normalized.text,
                exclude_asset_id=saved_sticker_asset_id,
            )
            if asset is not None:
                sticker_sent = False
                try:
                    await send_private_image_direct(
                        bot,
                        user_id=normalized.user_id,
                        file_path=asset.file_path,
                    )
                    await _feature_hub.stickers.mark_used(asset.asset_id)
                    sticker_sent = True
                except Exception as exc:
                    await _record_send_error(
                        normalized.trace_id,
                        exc,
                        0,
                        "send_private_sticker_failed",
                    )
                await _conversation_service.record_reply_audit(
                    normalized,
                    action="reply" if sticker_sent else "silence",
                    reason=(
                        "private_sticker_sent"
                        if sticker_sent
                        else "private_sticker_send_failed"
                    ),
                    model_called=False,
                    safety_blocked=False,
                )
            else:
                await send_reply_bubbles(
                    bot,
                    event,
                    "没有",
                    scope_type="private",
                    reply_config=_config.reply,
                    on_send_error=lambda exc, index, bubble: _record_send_error(
                        normalized.trace_id,
                        exc,
                        index,
                        "send_private_reply_failed",
                    ),
                )
                await _conversation_service.record_reply_audit(
                    normalized,
                    action="reply",
                    reason="private_sticker_missing",
                    model_called=False,
                    safety_blocked=False,
                )
            _remember_private_direct_action(normalized.user_id, "sticker")
            return
        if await _try_send_private_explicit_voice(
            bot,
            event,
            normalized,
            direct_intent.voice_read_text,
        ):
            _remember_private_direct_action(normalized.user_id, "voice")
            return
    if _permission_service.is_private_user_allowed(normalized.user_id):
        if not _rate_limiter.allow_user_minute(normalized.user_id):
            await _conversation_service.record_reply_audit(
                normalized,
                action="silence",
                reason="user_minute_rate_limited",
                model_called=False,
                safety_blocked=False,
            )
            logger.info("Private message ignored by user minute limit: user_id={}", normalized.user_id)
            return
        if not _rate_limiter.allow_private(normalized.user_id):
            await _conversation_service.record_reply_audit(
                normalized,
                action="reply",
                reason="private_cooldown",
                model_called=False,
                safety_blocked=False,
            )
            reply = GeneratedReply(
                text=_reply_formatter.format("等下，太快了。"),
                raw_model_text="等下，太快了。",
                model_name="rate_limiter",
                finish_reason="private_cooldown",
            )
            await send_reply_bubbles(
                bot,
                event,
                reply.text,
                scope_type="private",
                reply_config=_config.reply,
                on_send_error=lambda exc, index, bubble: _record_send_error(
                    normalized.trace_id,
                    exc,
                    index,
                    "send_private_reply_failed",
                ),
            )
            return

    force_voice_reply = (
        direct_intent.voice_reply_requested if direct_intent is not None else False
    )
    if force_voice_reply:
        reply = await _conversation_service.handle_private_message(
            normalized,
            prompt_user_text=_voice_reply_model_text(normalized.text),
        )
    else:
        reply = await _conversation_service.handle_private_message(normalized)
    if reply is None:
        logger.info("Private message ignored by whitelist: user_id={}", event.user_id)
        return
    voice_sent = await _maybe_send_private_voice_reply(
        bot,
        normalized,
        reply,
        force=force_voice_reply,
    )
    if voice_sent:
        _remember_private_direct_action(normalized.user_id, "voice")
        return

    if force_voice_reply:
        _remember_private_direct_action(normalized.user_id, "voice")
    else:
        _clear_private_direct_action(normalized.user_id)
    await send_reply_bubbles(
        bot,
        event,
        reply.text,
        scope_type="private",
        reply_config=_config.reply,
        reply_mode=reply.reply_mode,
        on_send_error=lambda exc, index, bubble: _record_send_error(
            normalized.trace_id,
            exc,
            index,
            "send_private_reply_failed",
        ),
    )
    if normalized.media_items and _can_pair_sticker_with_media_reply(reply):
        await _send_reply_sticker_if_requested(
            bot,
            normalized.user_id,
            reply.text,
            trace_id=normalized.trace_id,
            exclude_asset_id=saved_sticker_asset_id,
        )


_REMINDER_CREATE_HELP_TEXT = "要提醒什么？比如：十分钟后提醒我喝水"


async def _try_send_private_explicit_voice(
    bot: Bot,
    event: PrivateMessageEvent,
    normalized,
    explicit_text: str | None = None,
) -> bool:
    if not tts_enabled_for_scope(_config.tts, normalized.scope_type):
        return False
    if explicit_text is None:
        explicit_text = extract_explicit_voice_read_text(normalized)
    if explicit_text is None:
        return False
    safety = _voice_safety_service.check_input(explicit_text, scope_type=normalized.scope_type)
    if safety.action != "allow":
        return False
    await record_explicit_voice_selected(
        normalized,
        config=_config.tts,
        chars=len(explicit_text),
        record_system_event=_conversation_service.record_system_event,
    )
    sent = await _maybe_send_private_tts_text(
        bot,
        normalized,
        explicit_text,
        exact_short=True,
        ignore_cooldown=True,
    )
    if sent:
        await _conversation_service.record_reply_audit(
            normalized,
            action="reply",
            reason="private_explicit_voice_sent",
            model_called=False,
            safety_blocked=False,
        )
        return True
    await record_tts_fallback_text_sent(
        normalized,
        reason="explicit_tts_failed",
        record_system_event=_conversation_service.record_system_event,
    )
    await send_reply_bubbles(
        bot,
        event,
        explicit_text,
        scope_type="private",
        reply_config=_config.reply,
        on_send_error=lambda exc, index, bubble: _record_send_error(
            normalized.trace_id,
            exc,
            index,
            "send_private_reply_failed",
        ),
    )
    await _conversation_service.record_reply_audit(
        normalized,
        action="reply",
        reason="private_explicit_voice_text_fallback",
        model_called=False,
        safety_blocked=False,
    )
    return True


async def _maybe_send_private_voice_reply(
    bot: Bot,
    normalized,
    reply: GeneratedReply,
    *,
    force: bool = False,
) -> bool:
    disabled_reason = tts_scope_disabled_reason(_config.tts, normalized.scope_type)
    if disabled_reason is not None:
        if force:
            await record_tts_fallback_text_sent(
                normalized,
                reason=f"forced_tts_skipped_{disabled_reason}",
                record_system_event=_conversation_service.record_system_event,
            )
        return False
    explicit_text = extract_explicit_voice_read_text(normalized)
    if explicit_text is not None:
        return False
    if force:
        skip_reason = forced_voice_tts_skip_reason(
            _config.tts,
            reply,
            scope_type=normalized.scope_type,
        )
        if skip_reason is not None:
            await record_tts_fallback_text_sent(
                normalized,
                reason=f"forced_tts_skipped_{skip_reason}",
                record_system_event=_conversation_service.record_system_event,
            )
            return False
        if reply.model_name == "fallback":
            await _conversation_service.record_system_event(
                level="INFO",
                event="tts_forced_model_fallback_selected",
                detail=(
                    f"scope={normalized.scope_type}; reason={reply.finish_reason}; "
                    f"chars={len(reply.text)}"
                ),
                trace_id=normalized.trace_id,
            )
        await record_explicit_voice_selected(
            normalized,
            config=_config.tts,
            chars=len(reply.text),
            record_system_event=_conversation_service.record_system_event,
        )
        sent = await _maybe_send_private_tts_text(
            bot,
            normalized,
            reply.text,
            ignore_cooldown=True,
        )
        if sent:
            return True
        await record_tts_fallback_text_sent(
            normalized,
            reason="forced_tts_failed",
            record_system_event=_conversation_service.record_system_event,
        )
        return False

    decision = await _voice_reply_decider.decide_random(
        normalized,
        reply,
        config=_config.tts,
        record_system_event=_conversation_service.record_system_event,
    )
    if not decision.selected:
        return False
    sent = await _maybe_send_private_tts_text(bot, normalized, decision.speech_text)
    if not sent:
        await record_tts_fallback_text_sent(
            normalized,
            reason="random_tts_failed",
            record_system_event=_conversation_service.record_system_event,
        )
    return sent


async def _maybe_send_private_tts(bot: Bot, normalized, reply: GeneratedReply) -> bool:
    return await _maybe_send_private_tts_text(bot, normalized, reply.text)


def _voice_reply_model_text(text: str) -> str:
    return (
        f"{text}\n"
        "请直接生成这条语音里要说的内容，不要解释正在发语音，"
        "不要说“好的现在来一段语音”。"
    )


async def _maybe_send_private_tts_text(
    bot: Bot,
    normalized,
    text: str,
    *,
    exact_short: bool = False,
    ignore_cooldown: bool = False,
) -> bool:
    result = await _tts_service.generate_for_text(
        normalized,
        text,
        exact_short=exact_short,
        ignore_cooldown=ignore_cooldown,
        segment_max_chars=_tts_segment_max_chars()
        if exact_short or ignore_cooldown
        else None,
    )
    if result is None:
        return False
    try:
        await send_private_record_direct(
            bot,
            user_id=normalized.user_id,
            file_path=result.audio_path,
        )
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="tts_send_failed",
            detail=f"scope=private; profile={result.voice_profile_id}; reason={type(exc).__name__}; detail={str(exc)[:120]}",
            trace_id=normalized.trace_id,
        )
        return False
    return True


def _tts_segment_max_chars() -> int:
    configured_max = max(1, int(getattr(_config.tts, "max_chars", TTS_SEGMENT_MAX_CHARS)))
    return min(
        configured_max,
        EXACT_TTS_SEGMENT_MAX_CHARS,
    )


async def _handle_user_reminder_command(
    user_id: str,
    user_name: str | None,
    scope_id: str,
    text: str,
) -> str:
    if is_reminder_list_command(text):
        tasks = await _feature_hub.reminders.list_for_user(
            user_id=user_id,
            include_all=False,
            limit=10,
        )
        return format_reminder_tasks(tasks)
    if is_reminder_cancel_command(text):
        task_id = parse_reminder_cancel_id(text)
        if task_id is None:
            return "invalid reminder id"
        cancelled = await _feature_hub.reminders.cancel(
            task_id=task_id,
            user_id=user_id,
            include_all=False,
        )
        return f"reminder_cancelled={1 if cancelled else 0} id={task_id}"
    reminder = await _feature_hub.reminders.try_create_private_reminder(
        user_id=user_id,
        user_name=user_name,
        scope_id=scope_id,
        text=text,
    )
    if reminder is None:
        return _REMINDER_CREATE_HELP_TEXT
    return f"记下了，{reminder.due_at} 提醒你：{reminder.message}"


async def _record_send_error(
    trace_id: str,
    exc: Exception,
    index: int,
    event_name: str,
) -> None:
    await _conversation_service.record_system_event(
        level="ERROR",
        event=event_name,
        detail=f"{type(exc).__name__}: bubble_index={index}; {str(exc)[:120]}",
        trace_id=trace_id,
    )
    logger.exception("Failed to send private reply bubble: trace_id={}, index={}", trace_id, index)


def _is_plain_media_message(text: str) -> bool:
    return text.strip() in {"[media]", "[image]", "[face]"}


def _sticker_save_reply_text(
    *,
    saved_sticker_asset_id: str | None,
    recent_sticker_asset_id: str | None,
) -> str:
    if saved_sticker_asset_id is not None:
        return "存好了"
    if recent_sticker_asset_id is not None:
        return "刚才那张已经自动存好了"
    return "发出来我会自动存，之后说发个表情包就能用了。"


async def _send_reply_sticker_if_requested(
    bot: Bot,
    user_id: str,
    intent_text: str,
    *,
    trace_id: str,
    exclude_asset_id: str | None = None,
) -> bool:
    asset = await _choose_safe_sticker(intent_text, exclude_asset_id=exclude_asset_id)
    if asset is None:
        return False
    try:
        await send_private_image_direct(
            bot,
            user_id=user_id,
            file_path=asset.file_path,
        )
        await _feature_hub.stickers.mark_used(asset.asset_id)
    except Exception as exc:
        await _record_send_error(trace_id, exc, 0, "send_private_sticker_failed")
        return False
    return True


async def _choose_safe_sticker(intent_text: str, *, exclude_asset_id: str | None = None):
    for _ in range(3):
        asset = await _feature_hub.stickers.choose_for_text(intent_text)
        if asset is None:
            return None
        if exclude_asset_id is not None and asset.asset_id == exclude_asset_id:
            continue
        if await _is_sticker_asset_sendable(asset):
            return asset
    return None


def _private_lock_for(user_id: str) -> asyncio.Lock:
    lock = _private_user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _private_user_locks[user_id] = lock
    return lock


def _is_duplicate_pending_greeting(user_id: str, text: str) -> bool:
    return user_id in _pending_private_greetings and _is_simple_greeting(text)


def _is_simple_greeting(text: str) -> bool:
    compact = "".join(str(text or "").strip().lower().split()).strip("，。！？!?~～")
    return compact in {"你好", "hi", "hello", "在吗"}


async def _is_sticker_asset_sendable(asset) -> bool:
    if _feature_hub.sticker_analysis is None:
        return True
    analysis = await _feature_hub.sticker_analysis.get_completed_analysis(asset.asset_id)
    return not (
        analysis is not None
        and analysis.safety_category in {"adult", "illegal", "violence", "privacy"}
    )


def _can_pair_sticker_with_media_reply(reply: GeneratedReply) -> bool:
    if reply.finish_reason in {
        "adult",
        "illegal",
        "violence",
        "privacy",
        "vision_unavailable",
        "image_url_missing",
    }:
        return False
    text = reply.text.strip()
    if not text:
        return False
    return "看不了" not in text and "不太适合" not in text


def _apply_private_followup_intent(
    user_id: str,
    text: str,
    direct_intent: DirectReplyIntent,
) -> DirectReplyIntent:
    if (
        direct_intent.sticker_request
        or direct_intent.sticker_battle_request
        or direct_intent.voice_read_text is not None
        or direct_intent.voice_reply_requested
    ):
        return direct_intent
    if (
        _recent_private_direct_actions.get(user_id) == "sticker"
        and _is_repeat_previous_send_request(text)
    ):
        return DirectReplyIntent(sticker_request=True)
    if (
        _recent_private_direct_actions.get(user_id) == "voice"
        and _is_repeat_previous_voice_request(text)
    ):
        return DirectReplyIntent(voice_reply_requested=True)
    return direct_intent


def _remember_private_direct_action(user_id: str, action: str) -> None:
    if action:
        _recent_private_direct_actions[user_id] = action


def _clear_private_direct_action(user_id: str) -> None:
    _recent_private_direct_actions.pop(user_id, None)


def _is_repeat_previous_send_request(text: str) -> bool:
    compact = "".join(str(text or "").split()).strip("，,。.!！?？~～").lower()
    if not compact or any(marker in compact for marker in ("语音", "读", "念", "朗读")):
        return False
    return compact in {
        "再发一个",
        "再来一个",
        "再整一个",
        "再发个",
        "再来个",
        "再整一个吧",
        "再发一个吧",
        "再来一个吧",
        "继续发一个",
        "继续来一个",
        "还要一个",
        "还有吗",
        "还有没",
        "还有没有",
        "再发张",
        "再来张",
        "再发一张",
        "再来一张",
    }


def _is_repeat_previous_voice_request(text: str) -> bool:
    compact = "".join(str(text or "").split()).strip("，,。.!！?？~～").lower()
    if not compact or any(marker in compact for marker in ("表情", "图")):
        return False
    return compact in {
        "换一个",
        "换个",
        "再换一个",
        "再来一个",
        "再发一个",
        "再整一个",
        "继续来一个",
        "继续发一个",
        "还有吗",
        "还有没",
        "还有没有",
    }
