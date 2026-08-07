from __future__ import annotations

import asyncio
import os
import random
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

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
from app.features.news_service import (
    NewsCommandService,
    run_news_subscription_once,
)
from app.features.rss_news_provider import RssNewsProvider
from app.features.market_service import MarketCommandService, run_market_alert_once
from app.features.market_providers import create_market_providers
from app.features.search_providers import create_search_provider
from app.features.search_service import GroupSearchCommandService
from app.features.video_providers import YtDlpVideoExtractor
from app.features.video_service import GroupVideoService
from app.features.repeat_service import is_plus_one_text
from app.features.runtime_features import (
    create_runtime_feature_hub,
    maybe_save_sticker,
    reminder_worker,
)
from app.features.sticker_service import is_sticker_media
from app.features.image_provider import create_image_provider
from app.features.image_service import ImageGenerationService, parse_image_command
from app.features.speech_provider import create_speech_provider
from app.features.speech_service import SpeechService
from app.features.tts_service import (
    extract_explicit_voice_read_text,
    forced_voice_tts_skip_reason,
    record_explicit_voice_selected,
    record_tts_fallback_text_sent,
    prepare_tts_speech_text,
    tts_candidate_skip_reason,
    tts_enabled_for_scope,
    tts_scope_disabled_reason,
)
from app.model.llm_client import create_model_client
from app.models import MediaItem, NormalizedMessage
from app.plugins.send_helper import (
    _extract_sent_message_id,
    send_group_image_direct,
    send_group_record_direct,
    send_reply_bubbles,
    send_structured_information,
)
from app.routing.group_mute import (
    parse_group_mute_mode_command,
)
from app.routing.group_pending import GroupPendingQuestionService, PendingQuestionTarget
from app.routing.direct_intent import DirectReplyIntent, parse_direct_reply_intent
from app.routing.permission_service import PermissionService
from app.routing.rate_limiter import RateLimiter
from app.routing.group_trigger import contains_nickname, nickname_probability_passes
from app.safety.contextual_safety import GroupSafetyUnavailableError
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    BotSentMessageRepository,
    ConversationRepository,
    ConversationSessionRepository,
    GroupMemberProfileRepository,
    GroupMuteStateRepository,
    GroupNewsSubscriptionRepository,
    StockWatchRepository,
    GroupPendingQuestionRepository,
)

_config = load_config(os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json"))


def _voice_config():
    return _config.speech


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
_news_subscription_repository = GroupNewsSubscriptionRepository(
    _config.storage.database_path
)
_news_provider = (
    RssNewsProvider(
        feeds=_config.news.feeds,
        record_system_event=_conversation_service.record_system_event,
    )
    if _config.news.enabled and any(_config.news.feeds.values())
    else None
)
_news_command_service = NewsCommandService(
    provider=_news_provider,
    repository=_news_subscription_repository,
    qq_config=_config.qq,
    default_time=_config.news.default_time,
    timezone=_config.news.timezone,
    model_client=create_model_client(_config.model),
)
_stock_watch_repository = StockWatchRepository(_config.storage.database_path)
_market_providers = create_market_providers(
    _config.markets,
    record_system_event=_conversation_service.record_system_event,
)
_market_command_service = MarketCommandService(
    repository=_stock_watch_repository,
    providers=_market_providers,
    default_alert_threshold_percent=_config.markets.alert_threshold_percent,
    command_timeout_seconds=_config.markets.command_timeout_seconds,
)
_search_command_service = GroupSearchCommandService(
    search_provider=create_search_provider(
        _config.search,
        record_system_event=_conversation_service.record_system_event,
    ),
    model_client=create_model_client(_config.model),
    conversation_repository=ConversationRepository(_config.storage.database_path),
    session_repository=ConversationSessionRepository(_config.storage.database_path),
    profile_repository=GroupMemberProfileRepository(_config.storage.database_path),
    safety_service=SafetyService(
        identity_disclosure=_config.persona.style_profile.identity_disclosure,
        source_user_id=_config.persona.style_profile.source_user_id,
    ),
)
_video_extractor = (
    YtDlpVideoExtractor(
        _config.video.host_cache_path,
        http_proxy_env=_config.video.http_proxy_env,
        socks_proxy_env=_config.video.socks_proxy_env,
        cookie_file_env=_config.video.cookie_file_env,
    )
    if _config.video.enabled
    else None
)
_video_service = GroupVideoService(
    extractor=_video_extractor,
    config=_config.video,
    retry_policy=_config.retry,
    record_system_event=_conversation_service.record_system_event,
)
_image_service = ImageGenerationService(
    _config.image_generation,
    provider=create_image_provider(_config.image_generation),
    retry_policy=_config.retry,
    record_system_event=_conversation_service.record_system_event,
)
_rate_limiter = RateLimiter(
    _config.limits.group_cooldown_seconds,
    private_cooldown_seconds=_config.limits.private_cooldown_seconds,
    max_user_messages_per_minute=_config.limits.max_user_messages_per_minute,
    max_group_messages_per_minute=_config.limits.max_group_messages_per_minute,
)
_tts_service = SpeechService(
    _config.speech,
    provider=create_speech_provider(_config.speech),
    retry_policy=_config.retry,
    record_system_event=_conversation_service.record_system_event,
)
_voice_safety_service = SafetyService(
    identity_disclosure=_config.persona.style_profile.identity_disclosure,
    source_user_id=_config.persona.style_profile.source_user_id,
)
_active_windows: dict[tuple[str, str], float] = {}
_group_reply_queues: dict[str, asyncio.Queue["GroupReplyTask"]] = {}
_group_reply_workers: dict[str, asyncio.Task] = {}
_group_chat_generations: dict[str, int] = {}
_group_last_sent_at: dict[str, float] = {}
_group_recent_thread_events: list[dict[str, str | float | int]] = []
_group_recent_media_by_message_id: OrderedDict[str, tuple[MediaItem, ...]] = OrderedDict()
_group_recent_direct_actions: dict[tuple[str, str], tuple[str, float]] = {}
_group_voice_windows: dict[str, "GroupVoiceWindow"] = {}
_group_voice_counted_outgoing_keys: OrderedDict[tuple[str, str], None] = OrderedDict()
GROUP_INTERJECTION_PROBABILITY = 0.15
GROUP_RANDOM_VOICE_WINDOW_SIZE = 50
GROUP_RANDOM_VOICE_SELECTED_COUNT = 3
MAX_RECENT_MEDIA_MESSAGES = 200
MAX_GROUP_VOICE_COUNTED_KEYS = 500
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
_runtime_bot: Bot | None = None


class _RuntimeBotProxy:
    @staticmethod
    def _current() -> Bot:
        if _runtime_bot is None:
            raise RuntimeError("OneBot connection is not available")
        return _runtime_bot

    async def call_api(self, action: str, **kwargs):
        return await self._current().call_api(action, **kwargs)

    async def send_private_msg(self, **kwargs):
        return await self._current().send_private_msg(**kwargs)


_runtime_bot_proxy = _RuntimeBotProxy()


@dataclass(frozen=True)
class GroupReplyTask:
    bot: Bot
    event: GroupMessageEvent
    message: NormalizedMessage
    thread_key: str
    reason: str
    queued_at: float
    not_before: float = 0.0
    mute_generation: int = 0
    include_pending_backfill: bool = False


@dataclass(frozen=True)
class ContextStickerSendResult:
    sent: bool = False
    asset_found: bool = False
    send_failed: bool = False


@dataclass
class GroupVoiceWindow:
    window_id: int = 0
    index: int = 0
    selected_indexes: set[int] | None = None


@dataclass(frozen=True)
class GroupRandomVoiceDecision:
    selected: bool
    window_id: int
    window_index: int


group_chat = on_message(priority=10, block=False)


async def _start_reminder_worker(bot: Bot) -> None:
    global _reminder_worker_started, _runtime_bot
    _runtime_bot = bot
    if _reminder_worker_started:
        return
    _reminder_worker_started = True
    asyncio.create_task(reminder_worker(_runtime_bot_proxy, _feature_hub))
    asyncio.create_task(_runtime_news_subscription_worker(_runtime_bot_proxy))
    asyncio.create_task(_runtime_market_alert_worker(_runtime_bot_proxy))


async def _clear_runtime_bot(bot: Bot) -> None:
    global _runtime_bot
    if _runtime_bot is bot:
        _runtime_bot = None


async def _runtime_news_subscription_worker(bot: Bot) -> None:
    while True:
        try:
            zone = ZoneInfo(_config.news.timezone)
            await run_news_subscription_once(
                bot,
                _news_command_service,
                _news_subscription_repository,
                now=datetime.now(zone),
                can_send_group=_scheduled_group_send_allowed,
            )
        except Exception:
            logger.exception("News subscription worker iteration failed")
        await asyncio.sleep(30.0)


async def _runtime_market_alert_worker(bot: Bot) -> None:
    while True:
        try:
            zone = ZoneInfo(_config.news.timezone)
            await run_market_alert_once(
                bot,
                _stock_watch_repository,
                _market_providers,
                trading_date=datetime.now(zone).date(),
                can_send_group=_scheduled_group_send_allowed,
            )
        except Exception:
            logger.exception("Market alert worker iteration failed")
        await asyncio.sleep(float(_config.markets.poll_interval_seconds))


async def _scheduled_group_send_allowed(group_id: str) -> bool:
    state = await _group_mute_repository.get_by_group_id(group_id)
    return state is None or state.mode != "all_muted"


try:
    driver = get_driver()
    driver.on_bot_connect(_start_reminder_worker)
    driver.on_bot_disconnect(_clear_runtime_bot)
except ValueError:
    pass


@group_chat.handle()
async def _handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    normalized = normalize_group_message_event(event)
    if normalized is None:
        return
    referenced_session_id = await _bot_sent_session_id(
        normalized.reply_to_message_id
    )
    reply_to_bot = referenced_session_id is not None or await _bot_sent_repository.is_bot_sent_message(
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

    mute_command = parse_group_mute_mode_command(normalized, _config.qq)
    if mute_command is not None:
        await _group_mute_repository.set_mode(
            group_id=normalized.group_id or "",
            mode=mute_command,
            updated_by=normalized.user_id,
            reason=f"group_mute_{mute_command}",
        )
        if mute_command != "normal" and normalized.group_id is not None:
            await _cancel_group_chat_tasks(normalized.group_id)
        await _conversation_service.record_system_event(
            level="INFO",
            event=f"group_mute_{mute_command}",
            detail=f"group_id={normalized.group_id}; updated_by={normalized.user_id}",
            trace_id=normalized.trace_id,
        )
        return

    mute_state = (
        await _group_mute_repository.get_by_group_id(normalized.group_id)
        if normalized.group_id is not None
        else None
    )
    mute_mode = (
        getattr(
            mute_state,
            "mode",
            "chat_muted" if getattr(mute_state, "muted", False) else "normal",
        )
        if mute_state is not None
        else "normal"
    )
    if mute_mode == "all_muted":
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_all_muted",
            model_called=False,
            safety_blocked=False,
        )
        return

    if await _try_handle_group_video_feature(bot, event, normalized):
        return

    if await _try_handle_group_information_feature(bot, event, normalized):
        return

    if await _try_handle_group_image_generation_feature(bot, event, normalized):
        return

    _remember_message_media(normalized)
    normalized = _with_referenced_media(normalized)
    sticker_asset_id = await maybe_save_sticker(_feature_hub, normalized)
    direct_intent = parse_direct_reply_intent(normalized)
    if (
        trigger_reason == "nickname_probability_skipped"
        and (
            direct_intent.voice_read_text is not None
            or direct_intent.voice_reply_requested
        )
    ):
        trigger_reason = "nickname_trigger"
        normalized = replace(normalized, trigger_reason=trigger_reason)
    direct_intent = _apply_group_followup_intent(normalized, direct_intent)
    if await _try_send_group_speech_unavailable(
        bot,
        event,
        normalized,
        direct_intent,
    ):
        return
    if mute_mode == "normal":
        await _feature_hub.repeats.index_group_message(
            normalized,
            sticker_asset_id=sticker_asset_id,
        )

    pending_question = (
        await _pending_question_service.maybe_enqueue(normalized)
        if mute_mode == "normal"
        else None
    )
    if pending_question is not None and trigger_reason is None:
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_thread_pending",
            model_called=False,
            safety_blocked=False,
        )

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

    if direct_intent.sticker_request or direct_intent.sticker_battle_request:
        sticker_result = await _send_context_sticker(
            bot,
            normalized,
            exclude_asset_id=sticker_asset_id,
        )
        fallback_text_sent = False
        if not sticker_result.sent:
            if sticker_result.send_failed:
                fallback_text_sent = await _send_context_sticker_failed_text(
                    bot,
                    event,
                    normalized,
                )
            else:
                fallback_text_sent = await _send_context_sticker_missing_text(
                    bot,
                    event,
                    normalized,
                )
        await _conversation_service.record_reply_audit(
            normalized,
            action="reply" if sticker_result.sent or fallback_text_sent else "silence",
            reason=_context_sticker_audit_reason(
                sticker_result,
                fallback_text_sent=fallback_text_sent,
                sticker_battle_request=direct_intent.sticker_battle_request,
            ),
            model_called=False,
            safety_blocked=False,
        )
        if sticker_result.sent or fallback_text_sent:
            _remember_group_direct_action(
                normalized,
                "sticker_battle" if direct_intent.sticker_battle_request else "sticker",
            )
        return

    if await _try_send_group_explicit_voice(
        bot,
        event,
        normalized,
        direct_intent.voice_read_text,
    ):
        return

    if mute_mode == "chat_muted":
        await _conversation_service.record_reply_audit(
            normalized,
            action="silence",
            reason="group_chat_muted",
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
        if _should_enqueue_sticker_intent_reply(normalized, sticker_asset_id=sticker_asset_id):
            normalized = replace(normalized, trigger_reason="sticker_intent_interjection")
            trigger_reason = normalized.trigger_reason
        elif sticker_asset_id is not None:
            await _conversation_service.record_reply_audit(
                normalized,
                action="silence",
                reason="sticker_intent_interjection_skipped",
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

    prepared = await _prepare_group_chat_session_with_feedback(
        bot,
        event,
        normalized,
        referenced_session_id=referenced_session_id,
    )
    if prepared is None:
        return
    normalized = prepared

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


async def _try_handle_group_information_feature(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> bool:
    result = await _news_command_service.handle(message)
    if result is None:
        result = await _market_command_service.handle(message)
    if result is None:
        result = await _search_command_service.handle(message)
    if result is None:
        return False
    send_error = lambda exc, index, bubble: _record_send_error(
        message.trace_id,
        exc,
        index,
        "send_group_feature_reply_failed",
    )
    structured = getattr(result, "structured", None)
    if structured is not None:
        await send_structured_information(
            bot,
            event,
            structured.messages,
            fallback_messages=structured.fallback_messages,
            scope_type="group",
            reply_config=_config.reply,
            on_send_error=send_error,
        )
    else:
        await send_reply_bubbles(
            bot,
            event,
            result.text,
            scope_type="group",
            reply_config=_config.reply,
            on_send_error=send_error,
        )
    await _conversation_service.record_reply_audit(
        message,
        action="reply",
        reason=result.reason,
        model_called=bool(getattr(result, "model_called", False)),
        safety_blocked=False,
    )
    return True


async def _try_handle_group_image_generation_feature(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> bool:
    command = parse_image_command(message.text)
    if command is None:
        return False
    try:
        allowed = await _conversation_service.allow_group_feature_request(message)
    except GroupSafetyUnavailableError:
        await _send_group_safety_unavailable_reply(bot, event, message)
        return True
    if not allowed:
        return True
    if message.group_id is None:
        return True

    async def send_generated_image(file_path: str):
        await _wait_group_interval(message.group_id)
        result = await send_group_image_direct(
            bot,
            group_id=message.group_id,
            file_path=file_path,
        )
        await _record_sent_group_voice_message(
            message,
            _extract_sent_message_id(result),
        )
        _remember_group_non_random_outgoing(message.group_id, message.message_id)
        _group_last_sent_at[message.group_id] = time.monotonic()
        return result

    sent = await _image_service.execute(
        message,
        command.prompt,
        edit=command.edit,
        send=send_generated_image,
    )
    if sent is not None:
        await _conversation_service.record_reply_audit(
            message,
            action="reply",
            reason="group_image_edited" if command.edit else "group_image_generated",
            model_called=True,
            safety_blocked=False,
        )
        return True

    category = _image_service.failure_category(message.trace_id)
    if category == "safety_rejected":
        await _conversation_service.record_reply_audit(
            message,
            action="silence",
            reason="group_image_safety_blocked",
            model_called=True,
            safety_blocked=True,
        )
        return True

    failure_text = _image_service.failure_message(message.trace_id) or "图片处理失败：未知错误"
    await send_reply_bubbles(
        bot,
        event,
        failure_text,
        scope_type="group",
        reply_config=_config.reply,
        on_send_error=lambda exc, index, bubble: _record_send_error(
            message.trace_id,
            exc,
            index,
            "send_group_image_status_failed",
        ),
    )
    await _conversation_service.record_reply_audit(
        message,
        action="reply",
        reason=f"group_image_{category or 'failed'}",
        model_called=category not in {"unconfigured", "empty_prompt", "no_recent_image", "edit_window_expired"},
        safety_blocked=False,
    )
    return True


async def _try_handle_group_video_feature(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> bool:
    async def send_progress(text: str) -> None:
        await send_reply_bubbles(
            bot,
            event,
            text,
            scope_type="group",
            reply_config=_config.reply,
            on_send_error=lambda exc, index, bubble: _record_send_error(
                message.trace_id,
                exc,
                index,
                "send_group_video_progress_failed",
            ),
        )

    result = await _video_service.handle(bot, message, on_progress=send_progress)
    if result is None:
        return False
    if result.text:
        await send_structured_information(
            bot,
            event,
            (result.text,),
            scope_type="group",
            reply_config=_config.reply,
            on_send_error=lambda exc, index, bubble: _record_send_error(
                message.trace_id,
                exc,
                index,
                "send_group_video_status_failed",
            ),
        )
    await _conversation_service.record_reply_audit(
        message,
        action="reply",
        reason=result.reason,
        model_called=False,
        safety_blocked=False,
    )
    return True


def _is_explicit_group_sticker_request(message: NormalizedMessage) -> bool:
    return parse_direct_reply_intent(message).sticker_request


def _apply_group_followup_intent(
    message: NormalizedMessage,
    direct_intent: DirectReplyIntent,
) -> DirectReplyIntent:
    if (
        direct_intent.sticker_request
        or direct_intent.sticker_battle_request
        or direct_intent.voice_read_text is not None
        or direct_intent.voice_reply_requested
    ):
        return direct_intent
    recent_action = _recent_group_direct_action(message)
    if recent_action is None:
        return direct_intent
    if recent_action == "sticker_battle" and _has_sticker_battle_followup_media(message):
        return DirectReplyIntent(sticker_battle_request=True)
    if _is_repeat_previous_sticker_send_request(message.text):
        return DirectReplyIntent(
            sticker_request=recent_action == "sticker",
            sticker_battle_request=recent_action == "sticker_battle",
        )
    return direct_intent


def _remember_group_direct_action(message: NormalizedMessage, action: str) -> None:
    if message.group_id is None or not action:
        return
    _group_recent_direct_actions[(message.group_id, message.user_id)] = (
        action,
        time.monotonic() + _group_direct_action_window_seconds(),
    )


def _recent_group_direct_action(message: NormalizedMessage) -> str | None:
    if message.group_id is None:
        return None
    key = (message.group_id, message.user_id)
    item = _group_recent_direct_actions.get(key)
    if item is None:
        return None
    action, expires_at = item
    if time.monotonic() >= expires_at:
        _group_recent_direct_actions.pop(key, None)
        return None
    return action


def _group_direct_action_window_seconds() -> float:
    reply_config = getattr(_config, "reply", None)
    return float(getattr(reply_config, "active_window_seconds", 120))


def _has_sticker_battle_followup_media(message: NormalizedMessage) -> bool:
    return any(item.type == "face" or is_sticker_media(item) for item in message.media_items)


def _is_repeat_previous_sticker_send_request(text: str) -> bool:
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
        "换一个",
        "换个",
        "再换一个",
    }


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


async def _bot_sent_session_id(message_id: str | None) -> str | None:
    getter = getattr(_bot_sent_repository, "get_session_id", None)
    if getter is None:
        return None
    return await getter(message_id)


async def _prepare_group_chat_session(
    message: NormalizedMessage,
    *,
    referenced_session_id: str | None,
) -> NormalizedMessage | None:
    prepare = getattr(_conversation_service, "prepare_chat_message", None)
    if prepare is None:
        return message
    return await prepare(
        message,
        referenced_session_id=referenced_session_id,
    )


async def _prepare_group_chat_session_with_feedback(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
    *,
    referenced_session_id: str | None,
) -> NormalizedMessage | None:
    try:
        return await _prepare_group_chat_session(
            message,
            referenced_session_id=referenced_session_id,
        )
    except GroupSafetyUnavailableError:
        await _send_group_safety_unavailable_reply(bot, event, message)
        return None


async def _send_group_safety_unavailable_reply(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> None:
    await send_reply_bubbles(
        bot,
        event,
        "服务暂时超时，请稍后再试。",
        scope_type="group",
        reply_config=_config.reply,
        group_reply_to_message_id=message.message_id,
        group_at_user_id=message.user_id,
        on_send_error=lambda exc, index, bubble: _record_send_error(
            message.trace_id,
            exc,
            index,
            "send_group_safety_timeout_reply_failed",
        ),
    )
    await _conversation_service.record_reply_audit(
        message,
        action="reply",
        reason="group_contextual_safety_unavailable",
        model_called=False,
        safety_blocked=True,
    )


async def _enqueue_group_reply(task: GroupReplyTask) -> None:
    group_id = task.message.group_id
    if group_id is None:
        return
    generation = _group_chat_generations.get(group_id, 0)
    if task.not_before <= 0:
        delay_seconds = random.uniform(
            _config.conversation_sessions.chat_delay_min_ms / 1000,
            _config.conversation_sessions.chat_delay_max_ms / 1000,
        )
        task = replace(
            task,
            not_before=task.queued_at + delay_seconds,
            mute_generation=generation,
        )
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
            await _wait_until_not_before(task.not_before)
            if not _is_group_chat_task_current(task):
                continue
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


async def _wait_until_not_before(not_before: float) -> None:
    wait_seconds = not_before - time.monotonic()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


def _is_group_chat_task_current(task: GroupReplyTask) -> bool:
    group_id = task.message.group_id
    return group_id is not None and task.mute_generation == _group_chat_generations.get(
        group_id,
        0,
    )


async def _cancel_group_chat_tasks(group_id: str) -> int:
    generation = _group_chat_generations.get(group_id, 0) + 1
    _group_chat_generations[group_id] = generation

    queue = _group_reply_queues.pop(group_id, None)
    if queue is not None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                queue.task_done()

    worker = _group_reply_workers.pop(group_id, None)
    if worker is not None and not worker.done() and worker is not asyncio.current_task():
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    suspend = getattr(_conversation_service, "suspend_group_sessions", None)
    if suspend is not None:
        try:
            await suspend(group_id)
        except Exception:
            logger.exception("Failed to suspend group sessions: group_id={}", group_id)
    return generation


async def _wait_group_interval(group_id: str) -> None:
    return None


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
        force_voice_reply = parse_direct_reply_intent(
            target_message,
        ).voice_reply_requested
        model_question_text = (
            _voice_reply_model_text(target.question_text)
            if force_voice_reply
            else target.question_text
        )
        if target_message.media_items:
            reply = await _conversation_service.handle_group_image_message(
                target_message,
                reason=task.reason,
                can_commit=lambda: _is_group_chat_task_current(task),
            )
        else:
            reply = await _conversation_service.handle_group_question(
                target_message,
                question_text=target.question_text,
                question_user_name=target.user_name,
                reason=task.reason,
                prompt_question_text=model_question_text
                if force_voice_reply
                else None,
                can_commit=lambda: _is_group_chat_task_current(task),
            )
        if not _is_group_chat_task_current(task):
            await _conversation_service.record_reply_audit(
                message,
                action="silence",
                reason="group_reply_cancelled_by_mute",
                model_called=reply is not None,
                safety_blocked=False,
            )
            return
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
        voice_sent = await _maybe_send_group_voice_reply(
            task.bot,
            target_message,
            reply,
            force=force_voice_reply,
        )
        if voice_sent:
            if message.group_id is not None:
                _group_last_sent_at[message.group_id] = time.monotonic()
                _feature_hub.focus_group(message.group_id)
            await _pending_question_service.mark_answered(target)
            if target is not targets[-1] and message.group_id is not None:
                await _wait_group_interval(message.group_id)
            continue

        reply_to_message_id, at_user_id = _group_reply_addressing(task, target)
        await send_reply_bubbles(
            task.bot,
            task.event,
            reply.text,
            scope_type="group",
            reply_config=_config.reply,
            group_reply_to_message_id=reply_to_message_id,
            group_at_user_id=at_user_id,
            reply_mode=reply.reply_mode,
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
                target_message.session_id,
            ),
        )
        if message.group_id is not None:
            _remember_group_non_random_outgoing(
                target_message.group_id,
                target_message.message_id,
            )
            _group_last_sent_at[message.group_id] = time.monotonic()
            _feature_hub.focus_group(message.group_id)
        await _pending_question_service.mark_answered(target)
        if target is not targets[-1] and message.group_id is not None:
            await _wait_group_interval(message.group_id)


def _group_reply_addressing(task: GroupReplyTask, target) -> tuple[str | None, str | None]:
    if task.include_pending_backfill:
        return target.message_id, target.user_id
    return None, None


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


async def _maybe_send_group_voice_reply(
    bot: Bot,
    message: NormalizedMessage,
    reply,
    *,
    force: bool = False,
) -> bool:
    disabled_reason = tts_scope_disabled_reason(_voice_config(), message.scope_type)
    if disabled_reason is not None:
        if force:
            await record_tts_fallback_text_sent(
                message,
                reason=f"forced_tts_skipped_{disabled_reason}",
                record_system_event=_conversation_service.record_system_event,
            )
        return False
    explicit_text = extract_explicit_voice_read_text(message)
    if explicit_text is not None:
        return False
    if force:
        skip_reason = forced_voice_tts_skip_reason(
            _voice_config(),
            reply,
            scope_type=message.scope_type,
        )
        if skip_reason is not None:
            await record_tts_fallback_text_sent(
                message,
                reason=f"forced_tts_skipped_{skip_reason}",
                record_system_event=_conversation_service.record_system_event,
            )
            return False
        if reply.model_name == "fallback":
            await _conversation_service.record_system_event(
                level="INFO",
                event="tts_forced_model_fallback_selected",
                detail=(
                    f"scope={message.scope_type}; reason={reply.finish_reason}; "
                    f"chars={len(reply.text)}"
                ),
                trace_id=message.trace_id,
            )
        await record_explicit_voice_selected(
            message,
            config=_voice_config(),
            chars=len(reply.text),
            record_system_event=_conversation_service.record_system_event,
        )
        sent = await _maybe_send_group_tts_text(
            bot,
            message,
            reply.text,
            ignore_cooldown=True,
        )
        if sent:
            return True
        failure_text = _current_speech_failure(message.trace_id)
        if failure_text and message.group_id:
            await bot.send_group_msg(group_id=int(message.group_id), message=failure_text)
            return True
        await record_tts_fallback_text_sent(
            message,
            reason="forced_tts_failed",
            record_system_event=_conversation_service.record_system_event,
        )
        return False

    group_decision = _next_group_random_voice_decision(message)
    if group_decision is None:
        return False
    if not group_decision.selected:
        speech_text = prepare_tts_speech_text(reply.text)
        await _record_group_random_voice_decision(
            message,
            selected=False,
            reason="random_not_selected",
            chars=len(speech_text),
            window_id=group_decision.window_id,
            window_index=group_decision.window_index,
        )
        return False
    speech_text = prepare_tts_speech_text(reply.text)
    skip_reason = tts_candidate_skip_reason(
        _voice_config(),
        reply,
        scope_type=message.scope_type,
    )
    if skip_reason is not None:
        await _record_group_random_voice_decision(
            message,
            selected=False,
            reason=skip_reason,
            chars=len(speech_text),
            window_id=group_decision.window_id,
            window_index=group_decision.window_index,
        )
        return False
    await _record_group_random_voice_decision(
        message,
        selected=True,
        reason="random",
        chars=len(speech_text),
        window_id=group_decision.window_id,
        window_index=group_decision.window_index,
    )
    sent = await _maybe_send_group_tts_text(bot, message, speech_text)
    if not sent:
        await record_tts_fallback_text_sent(
            message,
            reason="random_tts_failed",
            record_system_event=_conversation_service.record_system_event,
        )
    return sent


def _voice_reply_model_text(text: str) -> str:
    return (
        f"{text}\n"
        "请直接生成这条语音里要说的内容，不要解释正在发语音，"
        "不要说“好的现在来一段语音”。"
    )


async def _maybe_send_group_tts(
    bot: Bot,
    message: NormalizedMessage,
    reply,
) -> bool:
    return await _maybe_send_group_tts_text(bot, message, reply.text)


async def _maybe_send_group_tts_text(
    bot: Bot,
    message: NormalizedMessage,
    text: str,
    *,
    exact_short: bool = False,
    ignore_cooldown: bool = False,
    single_request: bool = False,
    count_voice_window: bool = True,
) -> bool:
    del single_request
    if message.group_id is None:
        return False
    result = await _tts_service.generate_for_text(
        message,
        text,
        exact_short=exact_short,
        ignore_cooldown=ignore_cooldown,
    )
    if result is None:
        return False
    await _wait_group_interval(message.group_id)

    async def send_record():
        return await send_group_record_direct(
            bot,
            group_id=message.group_id,
            file_path=result.audio_path,
        )

    send_with_cleanup = getattr(_tts_service, "send_and_cleanup", None)
    if send_with_cleanup is not None:
        send_result = await send_with_cleanup(message, result, send_record)
        if send_result is None:
            return False
        await _record_sent_group_voice_message(
            message,
            _extract_sent_message_id(send_result),
        )
        if count_voice_window:
            _remember_group_non_random_outgoing(message.group_id, message.message_id)
        _group_last_sent_at[message.group_id] = time.monotonic()
        return True
    try:
        send_result = await send_record()
        await _record_sent_group_voice_message(
            message,
            _extract_sent_message_id(send_result),
        )
        if count_voice_window:
            _remember_group_non_random_outgoing(message.group_id, message.message_id)
        _group_last_sent_at[message.group_id] = time.monotonic()
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="tts_send_failed",
            detail=f"scope=group; profile={result.voice_profile_id}; reason={type(exc).__name__}; detail={str(exc)[:120]}",
            trace_id=message.trace_id,
        )
        return False
    return True


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


def _should_enqueue_sticker_intent_reply(
    normalized: NormalizedMessage,
    *,
    sticker_asset_id: str | None,
) -> bool:
    if sticker_asset_id is None or not normalized.media_items:
        return False
    return _feature_hub.presence.should_repeat(
        normalized,
        repeat_kind="sticker",
        plus_one=False,
    )


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


def _next_group_random_voice_decision(
    message: NormalizedMessage,
) -> GroupRandomVoiceDecision | None:
    if message.group_id is None:
        return None
    decision = _advance_group_voice_window(message.group_id, eligible=True)
    _mark_group_outgoing_counted(message.group_id, message.message_id)
    return decision


def _remember_group_non_random_outgoing(
    group_id: str | None,
    message_id: str | None = None,
) -> None:
    if group_id is None:
        return
    if message_id is not None and _is_group_outgoing_counted(group_id, message_id):
        return
    _advance_group_voice_window(group_id, eligible=False)
    if message_id is not None:
        _mark_group_outgoing_counted(group_id, message_id)


def _is_group_outgoing_counted(group_id: str, message_id: str) -> bool:
    return (group_id, message_id) in _group_voice_counted_outgoing_keys


def _mark_group_outgoing_counted(group_id: str, message_id: str) -> None:
    key = (group_id, message_id)
    _group_voice_counted_outgoing_keys[key] = None
    _group_voice_counted_outgoing_keys.move_to_end(key)
    while len(_group_voice_counted_outgoing_keys) > MAX_GROUP_VOICE_COUNTED_KEYS:
        _group_voice_counted_outgoing_keys.popitem(last=False)


def _advance_group_voice_window(
    group_id: str,
    *,
    eligible: bool,
) -> GroupRandomVoiceDecision:
    window = _group_voice_windows.setdefault(group_id, GroupVoiceWindow())
    if (
        window.index <= 0
        or window.index >= GROUP_RANDOM_VOICE_WINDOW_SIZE
        or window.selected_indexes is None
    ):
        _start_group_voice_window(window)
    window.index += 1
    return GroupRandomVoiceDecision(
        selected=eligible and window.index in (window.selected_indexes or set()),
        window_id=window.window_id,
        window_index=window.index,
    )


def _start_group_voice_window(window: GroupVoiceWindow) -> None:
    window.window_id += 1
    window.index = 0
    count = min(GROUP_RANDOM_VOICE_SELECTED_COUNT, GROUP_RANDOM_VOICE_WINDOW_SIZE)
    window.selected_indexes = set(
        random.sample(range(1, GROUP_RANDOM_VOICE_WINDOW_SIZE + 1), count)
    )


async def _record_group_random_voice_decision(
    message: NormalizedMessage,
    *,
    selected: bool,
    reason: str,
    chars: int,
    window_id: int,
    window_index: int,
) -> None:
    profile_id = _voice_config().voice or "unconfigured"
    await _conversation_service.record_system_event(
        level="INFO",
        event="tts_selected_random" if selected else "tts_skipped_random",
        detail=(
            f"scope=group; window={window_id}; index={window_index}; "
            f"profile={profile_id}; chars={chars}; reason={reason}"
        ),
        trace_id=message.trace_id,
    )


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


async def _try_send_group_explicit_voice(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
    explicit_text: str | None = None,
) -> bool:
    if not message.is_at_self:
        return False
    if not tts_enabled_for_scope(_voice_config(), message.scope_type):
        return False
    if explicit_text is None:
        explicit_text = extract_explicit_voice_read_text(message)
    if explicit_text is None:
        return False
    safety = _voice_safety_service.check_input(explicit_text, scope_type=message.scope_type)
    if safety.action != "allow":
        return False
    await record_explicit_voice_selected(
        message,
        config=_voice_config(),
        chars=len(explicit_text),
        record_system_event=_conversation_service.record_system_event,
    )
    sent = await _maybe_send_group_tts_text(
        bot,
        message,
        explicit_text,
        exact_short=True,
        ignore_cooldown=True,
        single_request=True,
    )
    if sent:
        await _conversation_service.record_reply_audit(
            message,
            action="reply",
            reason="group_explicit_voice_sent",
            model_called=False,
            safety_blocked=False,
        )
        return True
    await record_tts_fallback_text_sent(
        message,
        reason="explicit_tts_failed",
        record_system_event=_conversation_service.record_system_event,
    )
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
        _speech_failure_text(message.trace_id, fallback=explicit_text),
        scope_type="group",
        reply_config=_config.reply,
        group_reply_to_message_id=message.message_id,
        group_at_user_id=message.user_id,
        on_send_error=on_send_error,
    )
    if not failed:
        _remember_group_non_random_outgoing(message.group_id, message.message_id)
    await _conversation_service.record_reply_audit(
        message,
        action="reply" if not failed else "silence",
        reason=(
            "group_explicit_voice_text_fallback"
            if not failed
            else "group_explicit_voice_text_fallback_failed"
        ),
        model_called=False,
        safety_blocked=False,
    )
    return True


async def _try_send_group_speech_unavailable(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
    direct_intent: DirectReplyIntent,
) -> bool:
    if not (
        direct_intent.voice_read_text is not None
        or direct_intent.voice_reply_requested
    ):
        return False
    if tts_enabled_for_scope(_voice_config(), message.scope_type):
        return False
    await send_reply_bubbles(
        bot,
        event,
        _speech_failure_text(message.trace_id),
        scope_type="group",
        reply_config=_config.reply,
        on_send_error=lambda exc, index, bubble: _record_send_error(
            message.trace_id,
            exc,
            index,
            "send_group_speech_status_failed",
        ),
    )
    await _conversation_service.record_reply_audit(
        message,
        action="reply",
        reason="speech_unconfigured",
        model_called=False,
        safety_blocked=False,
    )
    return True


def _speech_failure_text(trace_id: str, *, fallback: str | None = None) -> str:
    failure_message = getattr(_tts_service, "failure_message", None)
    if failure_message is not None:
        text = failure_message(trace_id)
        if text:
            return text
    if fallback:
        return fallback
    return (
        "当前配置不支持语音：请管理员配置支持 /v1/audio/speech "
        "的语音模型和 API Key"
    )


def _current_speech_failure(trace_id: str) -> str | None:
    failure_message = getattr(_tts_service, "failure_message", None)
    return failure_message(trace_id) if failure_message is not None else None


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


def _context_sticker_audit_reason(
    result: ContextStickerSendResult,
    *,
    fallback_text_sent: bool,
    sticker_battle_request: bool,
) -> str:
    if result.sent:
        return (
            "context_sticker_battle_sent"
            if sticker_battle_request
            else "context_sticker_sent"
        )
    if result.send_failed:
        return (
            "context_sticker_send_failed_text_sent"
            if fallback_text_sent
            else "context_sticker_send_failed"
        )
    return (
        "context_sticker_missing_text_sent"
        if fallback_text_sent
        else "context_sticker_missing"
    )


async def _send_context_sticker(
    bot: Bot,
    message: NormalizedMessage,
    *,
    exclude_asset_id: str | None = None,
) -> ContextStickerSendResult:
    if message.group_id is None:
        return ContextStickerSendResult()
    asset = await _choose_safe_sticker(
        _sticker_battle_query_text(message),
        exclude_asset_id=exclude_asset_id,
    )
    if asset is None:
        return ContextStickerSendResult()
    try:
        await asyncio.sleep(_repeat_delay_seconds())
        await send_group_image_direct(
            bot,
            group_id=message.group_id,
            file_path=asset.file_path,
        )
        _remember_group_non_random_outgoing(message.group_id, message.message_id)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="send_context_sticker_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=message.trace_id,
        )
        return ContextStickerSendResult(asset_found=True, send_failed=True)
    try:
        await _feature_hub.stickers.mark_used(asset.asset_id)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="WARNING",
            event="mark_context_sticker_used_failed",
            detail=f"asset_id={asset.asset_id}; {type(exc).__name__}: {str(exc)[:120]}",
            trace_id=message.trace_id,
        )
    return ContextStickerSendResult(sent=True, asset_found=True)


async def _send_reply_sticker_if_requested(
    bot: Bot,
    message: NormalizedMessage,
    intent_text: str,
) -> bool:
    if message.group_id is None:
        return False
    asset = await _choose_safe_sticker(intent_text)
    if asset is None:
        return False
    try:
        await _wait_group_interval(message.group_id)
        await send_group_image_direct(
            bot,
            group_id=message.group_id,
            file_path=asset.file_path,
        )
        await _feature_hub.stickers.mark_used(asset.asset_id)
        _remember_group_non_random_outgoing(message.group_id, message.message_id)
        _group_last_sent_at[message.group_id] = time.monotonic()
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="send_reply_sticker_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=message.trace_id,
        )
        return False
    return True


async def _choose_safe_sticker(
    intent_text: str,
    *,
    exclude_asset_id: str | None = None,
):
    for _ in range(3):
        asset = await _feature_hub.stickers.choose_for_text(intent_text)
        if asset is None:
            return None
        if exclude_asset_id is not None and asset.asset_id == exclude_asset_id:
            continue
        if await _is_sticker_asset_sendable(asset):
            return asset
    return None


def _sticker_battle_query_text(message: NormalizedMessage) -> str:
    parts = [message.text.strip()]
    media_parts: list[str] = []
    for item in message.media_items:
        if item.type != "face" and not is_sticker_media(item):
            continue
        for value in (item.summary, item.sub_type, item.file, item.type):
            if value and value not in media_parts:
                media_parts.append(value)
    if media_parts:
        parts.append(" ".join(media_parts))
    return " ".join(part for part in parts if part).strip() or "表情包"


async def _is_sticker_asset_sendable(asset) -> bool:
    if _feature_hub.sticker_analysis is None:
        return True
    analysis = await _feature_hub.sticker_analysis.get_completed_analysis(asset.asset_id)
    return not (
        analysis is not None
        and analysis.safety_category in {"adult", "illegal", "violence", "privacy"}
    )


async def _send_context_sticker_missing_text(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> bool:
    return await _send_context_sticker_fallback_text(bot, event, message, "没有")


async def _send_context_sticker_failed_text(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
) -> bool:
    return await _send_context_sticker_fallback_text(
        bot,
        event,
        message,
        "图发不出去，卡了",
    )


async def _send_context_sticker_fallback_text(
    bot: Bot,
    event: GroupMessageEvent,
    message: NormalizedMessage,
    text: str,
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
        text,
        scope_type="group",
        reply_config=_config.reply,
        group_reply_to_message_id=message.message_id,
        group_at_user_id=message.user_id,
        on_send_error=on_send_error,
    )
    if not failed:
        _remember_group_non_random_outgoing(message.group_id, message.message_id)
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
        _remember_group_non_random_outgoing(candidate.group_id, candidate.message_id)
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


async def _record_send_error(
    trace_id: str,
    exc: Exception,
    index: int,
    event_name: str = "send_group_reply_failed",
) -> None:
    await _conversation_service.record_system_event(
        level="ERROR",
        event=event_name,
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
    session_id: str | None = None,
) -> None:
    if sent_message_id is None or group_id is None:
        return
    values = dict(
        message_id=sent_message_id,
        trace_id=trace_id,
        group_id=group_id,
        user_id=self_id,
        original_message_id=target.message_id,
    )
    if session_id is not None:
        values["session_id"] = session_id
    await _bot_sent_repository.save_group_message(**values)


async def _record_sent_group_voice_message(
    message: NormalizedMessage,
    sent_message_id: str | None,
) -> None:
    if sent_message_id is None or message.group_id is None:
        return
    values = dict(
        message_id=sent_message_id,
        trace_id=message.trace_id,
        group_id=message.group_id,
        user_id=message.self_id,
        original_message_id=message.message_id,
    )
    if message.session_id is not None:
        values["session_id"] = message.session_id
    await _bot_sent_repository.save_group_message(**values)
