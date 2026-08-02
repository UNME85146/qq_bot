from __future__ import annotations

import os
import re
import shlex
import time

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent

from app.adapters.onebot_event_adapter import normalize_private_message_event
from app.bootstrap import create_conversation_service
from app.config import load_config
from app.conversation.reply_formatter import ReplyFormatter
from app.features.reminder_service import (
    format_reminder_tasks,
    is_reminder_cancel_command,
    is_reminder_list_command,
    parse_reminder_cancel_id,
)
from app.features.runtime_features import create_runtime_feature_hub
from app.features.image_provider import create_image_provider
from app.features.image_service import ImageGenerationService
from app.features.speech_provider import create_speech_provider
from app.features.speech_service import SpeechService
from app.model.llm_client import create_model_client
from app.ops.config_editor import (
    handle_allow_config_command,
    handle_owner_config_command,
    handle_voice_config_command,
)
from app.ops.runtime_status import build_owner_status_text
from app.ops.video_upload_acceptance import (
    VideoUploadAcceptanceError,
    VideoUploadPlanGate,
    acceptance_audit_detail,
    apply_video_upload_plan,
)
from app.plugins.send_helper import send_reply_bubbles
from app.routing.permission_service import PermissionService
from app.routing.rate_limiter import RateLimiter
from app.storage.repositories import (
    AuditRepository,
    GroupMuteStateRepository,
    MemoryProfileRepository,
)

_config_path = os.getenv("QQ_BOT_CONFIG_PATH", "config/config.json")
_config = load_config(_config_path)
_conversation_service = create_conversation_service(_config)
_feature_hub = create_runtime_feature_hub(_config)
_audit_repository = AuditRepository(_config.storage.database_path)
_group_mute_repository = GroupMuteStateRepository(_config.storage.database_path)
_memory_repository = MemoryProfileRepository(_config.storage.database_path)
_video_upload_plan_gate = VideoUploadPlanGate()

OWNER_COMMAND_PREFIXES = (
    "/help",
    "/status",
    "/memory",
    "/audit",
    "/reload",
    "/ping",
    "/allow",
    "/mute",
    "/owner",
    "/remind",
    "/voice",
    "/video",
)


async def _is_owner_command(event: PrivateMessageEvent) -> bool:
    normalized = normalize_private_message_event(event)
    if normalized is None or not _is_known_owner_command(normalized.text):
        return False
    if normalized.text.strip().startswith("/remind"):
        return _is_reminder_owner_command(normalized.text) and _is_effective_owner(
            normalized.user_id
        )
    return True


owner_commands = on_message(rule=_is_owner_command, priority=1, block=True)


@owner_commands.handle()
async def _handle_owner_command(bot: Bot, event: PrivateMessageEvent) -> None:
    normalized = normalize_private_message_event(event)
    if normalized is None:
        return
    text = normalized.text.strip()

    is_owner = _is_effective_owner(normalized.user_id)
    is_root = _is_root(normalized.user_id)
    if not is_owner:
        return

    if text == "/help":
        await _send_owner_help(bot, event, normalized.trace_id, is_root=is_root)
        return
    if text == "/status":
        reply = _status_text()
    elif text == "/memory":
        profile = await _memory_repository.get_by_user_id(normalized.user_id)
        reply = _memory_text(profile)
    elif text == "/memory clear":
        await _memory_repository.clear(normalized.user_id)
        reply = "记忆清掉了"
    elif text == "/audit last":
        reply = await _audit_text()
    elif text.startswith("/allow "):
        reply = await _allow_text(text)
    elif text.startswith("/owner "):
        if not is_root:
            return
        reply = await _owner_text(text)
    elif text.startswith("/voice "):
        reply = await _voice_text(text)
    elif text.startswith("/video upload "):
        if not is_root:
            return
        reply = await _video_upload_acceptance_text(
            bot,
            text,
            trace_id=normalized.trace_id,
        )
    elif text == "/mute status":
        reply = await _mute_status_text()
    elif text == "/mute clear" or text.startswith("/mute clear "):
        group_id = text.removeprefix("/mute clear").strip() or None
        reply = await _mute_clear_text(
            updated_by=normalized.user_id,
            group_id=group_id,
        )
    elif is_reminder_list_command(text):
        reply = await _remind_list_text(
            user_id=normalized.user_id,
            include_all=is_root,
        )
    elif is_reminder_cancel_command(text):
        reply = await _remind_cancel_text(
            text,
            user_id=normalized.user_id,
            include_all=is_root,
        )
    elif text == "/reload profile":
        reply = "这个版本暂时需要重启服务后重新加载画像。"
    elif text == "/ping model":
        reply = await _ping_model()
    else:
        return

    await send_reply_bubbles(
        bot,
        event,
        reply,
        scope_type="private",
        reply_config=_config.reply,
        on_send_error=lambda exc, index, bubble: _conversation_service.record_system_event(
            level="ERROR",
            event="send_owner_command_failed",
            detail=f"{type(exc).__name__}: bubble_index={index}; {str(exc)[:120]}",
            trace_id=normalized.trace_id,
        ),
    )


def _is_known_owner_command(text: str) -> bool:
    stripped = text.strip()
    return any(
        stripped == prefix or stripped.startswith(prefix + " ")
        for prefix in OWNER_COMMAND_PREFIXES
    )


def _is_reminder_owner_command(text: str) -> bool:
    return is_reminder_list_command(text) or is_reminder_cancel_command(text)


def _is_root(user_id: str) -> bool:
    return str(user_id) in _config.qq.root_user_ids


def _is_effective_owner(user_id: str) -> bool:
    user_id = str(user_id)
    return user_id in _config.qq.root_user_ids or user_id in _config.qq.owner_user_ids


def _status_text() -> str:
    return build_owner_status_text(_config)


async def _send_owner_help(
    bot: Bot,
    event: PrivateMessageEvent,
    trace_id: str,
    *,
    is_root: bool,
) -> None:
    try:
        await bot.send(event, _help_text(is_root=is_root))
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="send_owner_help_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=trace_id,
        )


def _help_text(*, is_root: bool) -> str:
    lines = [
        "小黄管理命令帮助",
        "",
        "普通管理命令：",
        "/help - 查看这份命令帮助。用法：/help",
        "/status - 查看运行状态、白名单、数据库计数、最近审计和模型失败。用法：/status",
        "/memory - 查看你自己的长期记忆摘要。用法：/memory",
        "/memory clear - 清空你自己的长期记忆。用法：/memory clear",
        "/audit last - 查看最近 5 条回复审计和系统事件。用法：/audit last",
        "/reload profile - 查看画像重载提示，当前版本需要重启服务后重新加载。用法：/reload profile",
        "/ping model - 测试当前模型配置是否可调用。用法：/ping model",
        "",
        "白名单命令：",
        "/allow private add <qq> - 添加私聊白名单。例：/allow private add 123456",
        "/allow private remove <qq> - 移除私聊白名单。例：/allow private remove 123456",
        "/allow private list - 查看私聊白名单。用法：/allow private list",
        "/allow group add <group_id> - 添加群聊白名单。例：/allow group add 123456",
        "/allow group remove <group_id> - 移除群聊白名单。例：/allow group remove 123456",
        "/allow group list - 查看群聊白名单。用法：/allow group list",
        "",
        "群静默命令：",
        "/mute status - 查看当前静默群。用法：/mute status",
        "/mute clear - 清空全部群静默状态。用法：/mute clear",
        "/mute clear <group_id> - 清除指定群静默状态。例：/mute clear 123456",
    ]
    lines.extend(
        [
            "",
            "语音命令：",
            "/voice status - 查看 /v1/audio/speech 配置状态、模型、音色和端点。用法：/voice status",
            "/voice on - 开启全局语音回复。用法：/voice on",
            "/voice off - 关闭全局语音回复。用法：/voice off",
            "/voice private on|off - 开关私聊回复附加语音。例：/voice private on",
            "/voice group on|off - 开关群聊回复附加语音，命令仍只允许 owner/root 私聊使用。例：/voice group on",
            "模型、音色、Base URL 和 API Key 环境变量由管理员在 speech 配置中设置。",
        ]
    )
    lines.extend(
        [
            "",
            "定时提醒命令：",
            "/remind <提醒内容> - 创建私聊定时提醒。例：/remind 十分钟后提醒我喝水",
            "/remind list - 查看待触发提醒。用法：/remind list",
            "/remind cancel <id> - 取消指定提醒。例：/remind cancel 3",
        ]
    )
    if is_root:
        lines.extend(
            [
                "",
                "Root 专用命令：",
                "/owner add <qq> - 添加 owner。例：/owner add 123456",
                "/owner remove <qq> - 移除 owner，不能移除 root 或最后一个 owner。例：/owner remove 123456",
                "/owner list - 查看 owner 列表。用法：/owner list",
                "/video upload plan <group_id> <cache-relative.mp4> - 生成小视频上传验收计划。",
                "/video upload apply <group_id> <cache-relative.mp4> <plan_hash> --confirm-group <group_id> - 执行一次受控上传。",
            ]
        )
    lines.extend(
        [
            "",
            "权限说明：以上普通管理命令仅 owner/root 可用，非 owner 发送会静默。",
            "/owner ... 仅 root 可用；root 在 qq.rootUserIds 中配置。",
            "root 自动拥有 owner 权限和私聊白名单权限。",
        ]
    )
    return "\n".join(lines)


async def _video_upload_acceptance_text(bot: Bot, text: str, *, trace_id: str) -> str:
    try:
        parts = shlex.split(text)
    except ValueError:
        return "video upload command parse failed"
    usage = (
        "用法：/video upload plan <group_id> <cache-relative.mp4>\n"
        "执行：/video upload apply <group_id> <cache-relative.mp4> "
        "<plan_hash> --confirm-group <group_id>"
    )
    if len(parts) == 5 and parts[:3] == ["/video", "upload", "plan"]:
        try:
            plan = _video_upload_plan_gate.issue(
                _config.video,
                group_id=parts[3],
                relative_file=parts[4],
            )
        except VideoUploadAcceptanceError as exc:
            return f"小视频上传验收计划失败：{exc}"
        await _conversation_service.record_system_event(
            level="INFO",
            event="video_upload_acceptance",
            detail=acceptance_audit_detail(
                plan=plan,
                stage="plan",
                result="planned",
                duration_ms=0,
            ),
            trace_id=trace_id,
        )
        return (
            "小视频上传验收计划\n"
            f"目标群：{plan['group_id']}\n"
            f"缓存文件：{plan['relative_file']}\n"
            f"大小：{plan['size_bytes']} bytes（上限 {plan['maximum_bytes']}）\n"
            f"SHA256：{plan['sha256']}\n"
            f"plan_hash：{plan['plan_hash']}\n"
            "仅在确认目标群后执行 apply；该操作会真实上传一个群文件。"
        )
    if (
        len(parts) == 8
        and parts[:3] == ["/video", "upload", "apply"]
        and parts[6] == "--confirm-group"
    ):
        plan = None
        started = time.perf_counter()
        try:
            plan = _video_upload_plan_gate.consume(
                _config.video,
                group_id=parts[3],
                relative_file=parts[4],
                plan_hash=parts[5],
                confirmed_group_id=parts[7],
            )
            result = await apply_video_upload_plan(
                bot,
                _config.video,
                group_id=parts[3],
                relative_file=parts[4],
                plan_hash=parts[5],
                confirmed_group_id=parts[7],
                expected_plan=plan,
            )
        except VideoUploadAcceptanceError as exc:
            await _conversation_service.record_system_event(
                level="WARNING",
                event="video_upload_acceptance",
                detail=acceptance_audit_detail(
                    plan=plan,
                    stage="apply",
                    result="failure",
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    error_category=exc.category,
                ),
                trace_id=trace_id,
            )
            return f"小视频上传验收失败：{exc}"
        await _conversation_service.record_system_event(
            level="INFO",
            event="video_upload_acceptance",
            detail=acceptance_audit_detail(
                plan=result.plan,
                stage="apply",
                result="success",
                duration_ms=result.duration_ms,
            ),
            trace_id=trace_id,
        )
        return (
            "小视频上传验收完成：upload_group_file 已返回成功，"
            f"耗时 {result.duration_ms} ms"
        )
    return usage


async def _allow_text(text: str) -> str:
    try:
        result = handle_allow_config_command(_config_path, text)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="allow_config_write_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=None,
        )
        return f"allow config failed: {type(exc).__name__}"

    if not result.changed:
        return result.text

    try:
        _reload_runtime_config()
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="allow_config_reload_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=None,
        )
        return f"{result.text}\nconfig written, restart needed"

    return f"{result.text}\nhot reloaded"


async def _owner_text(text: str) -> str:
    try:
        result = handle_owner_config_command(_config_path, text)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="owner_config_write_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=None,
        )
        return f"owner config failed: {type(exc).__name__}"

    if not result.changed:
        return result.text

    try:
        _reload_runtime_config()
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="owner_config_reload_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=None,
        )
        return f"{result.text}\nconfig written, restart needed"

    return f"{result.text}\nhot reloaded"


async def _voice_text(text: str) -> str:
    try:
        result = handle_voice_config_command(_config_path, text)
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="voice_config_write_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=None,
        )
        return f"语音配置写入失败：{type(exc).__name__}"

    if not result.changed:
        return result.text

    try:
        _reload_runtime_config()
    except Exception as exc:
        await _conversation_service.record_system_event(
            level="ERROR",
            event="voice_config_reload_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            trace_id=None,
        )
        return f"{result.text}\n配置已写入，需要重启服务后生效"

    return f"{result.text}\n配置已热加载"


def _reload_runtime_config() -> None:
    global _config
    global _conversation_service
    global _feature_hub
    global _audit_repository
    global _group_mute_repository
    global _memory_repository

    new_config = load_config(_config_path)
    _config = new_config
    _conversation_service = create_conversation_service(new_config)
    _feature_hub = create_runtime_feature_hub(new_config)
    _audit_repository = AuditRepository(new_config.storage.database_path)
    _group_mute_repository = GroupMuteStateRepository(new_config.storage.database_path)
    _memory_repository = MemoryProfileRepository(new_config.storage.database_path)

    _reload_private_chat(new_config)
    _reload_group_chat(new_config)
    _reload_group_reactions(new_config)


def _reload_private_chat(new_config) -> None:
    try:
        import app.plugins.private_chat as private_chat
    except Exception:
        return

    private_chat._config = new_config  # noqa: SLF001
    private_chat._conversation_service = create_conversation_service(new_config)  # noqa: SLF001
    if hasattr(private_chat, "_feature_hub"):
        private_chat._feature_hub = create_runtime_feature_hub(new_config)  # noqa: SLF001
    private_chat._permission_service = PermissionService(new_config.qq)  # noqa: SLF001
    if hasattr(private_chat, "_market_command_service"):
        from app.features.market_providers import create_market_providers
        from app.features.market_service import MarketCommandService
        from app.storage.repositories import StockWatchRepository

        private_chat._stock_watch_repository = StockWatchRepository(  # noqa: SLF001
            new_config.storage.database_path
        )
        private_chat._market_providers = create_market_providers(  # noqa: SLF001
            new_config.markets,
            record_system_event=private_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
        private_chat._market_command_service = MarketCommandService(  # noqa: SLF001
            repository=private_chat._stock_watch_repository,  # noqa: SLF001
            providers=private_chat._market_providers,  # noqa: SLF001
            default_alert_threshold_percent=new_config.markets.alert_threshold_percent,
            command_timeout_seconds=new_config.markets.command_timeout_seconds,
        )
    private_chat._rate_limiter = RateLimiter(  # noqa: SLF001
        new_config.limits.group_cooldown_seconds,
        private_cooldown_seconds=new_config.limits.private_cooldown_seconds,
        max_user_messages_per_minute=new_config.limits.max_user_messages_per_minute,
        max_group_messages_per_minute=new_config.limits.max_group_messages_per_minute,
    )
    if hasattr(private_chat, "_reply_formatter"):
        private_chat._reply_formatter = ReplyFormatter(new_config.reply.max_reply_length)  # noqa: SLF001
    if hasattr(private_chat, "_tts_service"):
        private_chat._tts_service = SpeechService(  # noqa: SLF001
            new_config.speech,
            provider=create_speech_provider(new_config.speech),
            retry_policy=new_config.retry,
            record_system_event=private_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
    if hasattr(private_chat, "_image_service"):
        private_chat._image_service = ImageGenerationService(  # noqa: SLF001
            new_config.image_generation,
            provider=create_image_provider(new_config.image_generation),
            retry_policy=new_config.retry,
            record_system_event=private_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
    if hasattr(private_chat, "_voice_safety_service"):
        from app.safety.safety_service import SafetyService

        private_chat._voice_safety_service = SafetyService(  # noqa: SLF001
            identity_disclosure=new_config.persona.style_profile.identity_disclosure,
            source_user_id=new_config.persona.style_profile.source_user_id,
        )
        if hasattr(private_chat, "_image_safety_service"):
            private_chat._image_safety_service = SafetyService(  # noqa: SLF001
                identity_disclosure=new_config.persona.style_profile.identity_disclosure,
                source_user_id=new_config.persona.style_profile.source_user_id,
            )


def _reload_group_chat(new_config) -> None:
    try:
        import app.plugins.group_chat as group_chat
    except Exception:
        return

    group_chat._config = new_config  # noqa: SLF001
    group_chat._conversation_service = create_conversation_service(new_config)  # noqa: SLF001
    if hasattr(group_chat, "_feature_hub"):
        group_chat._feature_hub = create_runtime_feature_hub(new_config)  # noqa: SLF001
    group_chat._permission_service = PermissionService(new_config.qq)  # noqa: SLF001
    if hasattr(group_chat, "_video_service"):
        from app.features.video_providers import YtDlpVideoExtractor
        from app.features.video_service import GroupVideoService

        group_chat._video_extractor = (  # noqa: SLF001
            YtDlpVideoExtractor(
                new_config.video.host_cache_path,
                http_proxy_env=new_config.video.http_proxy_env,
                socks_proxy_env=new_config.video.socks_proxy_env,
            )
            if new_config.video.enabled
            else None
        )
        group_chat._video_service = GroupVideoService(  # noqa: SLF001
            extractor=group_chat._video_extractor,  # noqa: SLF001
            config=new_config.video,
            retry_policy=new_config.retry,
            record_system_event=group_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
    if hasattr(group_chat, "_news_command_service"):
        from app.features.news_service import NewsCommandService
        from app.features.rss_news_provider import RssNewsProvider
        from app.storage.repositories import GroupNewsSubscriptionRepository

        group_chat._news_subscription_repository = GroupNewsSubscriptionRepository(  # noqa: SLF001
            new_config.storage.database_path
        )
        group_chat._news_provider = (  # noqa: SLF001
            RssNewsProvider(
                feeds=new_config.news.feeds,
                record_system_event=group_chat._conversation_service.record_system_event,  # noqa: SLF001
            )
            if new_config.news.enabled and any(new_config.news.feeds.values())
            else None
        )
        group_chat._news_command_service = NewsCommandService(  # noqa: SLF001
            provider=group_chat._news_provider,  # noqa: SLF001
            repository=group_chat._news_subscription_repository,  # noqa: SLF001
            qq_config=new_config.qq,
            default_time=new_config.news.default_time,
            timezone=new_config.news.timezone,
            model_client=create_model_client(new_config.model),
        )
    if hasattr(group_chat, "_market_command_service"):
        from app.features.market_providers import create_market_providers
        from app.features.market_service import MarketCommandService
        from app.storage.repositories import StockWatchRepository

        group_chat._stock_watch_repository = StockWatchRepository(  # noqa: SLF001
            new_config.storage.database_path
        )
        group_chat._market_providers = create_market_providers(  # noqa: SLF001
            new_config.markets,
            record_system_event=group_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
        group_chat._market_command_service = MarketCommandService(  # noqa: SLF001
            repository=group_chat._stock_watch_repository,  # noqa: SLF001
            providers=group_chat._market_providers,  # noqa: SLF001
            default_alert_threshold_percent=new_config.markets.alert_threshold_percent,
            command_timeout_seconds=new_config.markets.command_timeout_seconds,
        )
    if hasattr(group_chat, "_search_command_service"):
        from app.features.search_providers import create_search_provider
        from app.features.search_service import GroupSearchCommandService
        from app.safety.safety_service import SafetyService as SearchSafetyService
        from app.storage.repositories import (
            ConversationRepository,
            ConversationSessionRepository,
            GroupMemberProfileRepository,
        )

        group_chat._search_command_service = GroupSearchCommandService(  # noqa: SLF001
            search_provider=create_search_provider(
                new_config.search,
                record_system_event=group_chat._conversation_service.record_system_event,  # noqa: SLF001
            ),
            model_client=create_model_client(new_config.model),
            conversation_repository=ConversationRepository(
                new_config.storage.database_path
            ),
            session_repository=ConversationSessionRepository(
                new_config.storage.database_path
            ),
            profile_repository=GroupMemberProfileRepository(
                new_config.storage.database_path
            ),
            safety_service=SearchSafetyService(
                identity_disclosure=new_config.persona.style_profile.identity_disclosure,
                source_user_id=new_config.persona.style_profile.source_user_id,
            ),
        )
    group_chat._group_mute_repository = GroupMuteStateRepository(  # noqa: SLF001
        new_config.storage.database_path
    )
    if hasattr(group_chat, "_bot_sent_repository"):
        from app.storage.repositories import BotSentMessageRepository

        group_chat._bot_sent_repository = BotSentMessageRepository(  # noqa: SLF001
            new_config.storage.database_path
        )
    if hasattr(group_chat, "_pending_question_repository"):
        from app.storage.repositories import GroupPendingQuestionRepository

        group_chat._pending_question_repository = GroupPendingQuestionRepository(  # noqa: SLF001
            new_config.storage.database_path
        )
    if hasattr(group_chat, "_pending_question_service"):
        from app.safety.safety_service import SafetyService
        from app.routing.group_pending import GroupPendingQuestionService

        safety_service = SafetyService(
            identity_disclosure=new_config.persona.style_profile.identity_disclosure,
            source_user_id=new_config.persona.style_profile.source_user_id,
        )
        group_chat._pending_question_service = GroupPendingQuestionService(  # noqa: SLF001
            repository=group_chat._pending_question_repository,  # noqa: SLF001
            safety_service=safety_service,
        )
    group_chat._rate_limiter = RateLimiter(  # noqa: SLF001
        new_config.limits.group_cooldown_seconds,
        private_cooldown_seconds=new_config.limits.private_cooldown_seconds,
        max_user_messages_per_minute=new_config.limits.max_user_messages_per_minute,
        max_group_messages_per_minute=new_config.limits.max_group_messages_per_minute,
    )
    if hasattr(group_chat, "_tts_service"):
        group_chat._tts_service = SpeechService(  # noqa: SLF001
            new_config.speech,
            provider=create_speech_provider(new_config.speech),
            retry_policy=new_config.retry,
            record_system_event=group_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
    if hasattr(group_chat, "_image_service"):
        group_chat._image_service = ImageGenerationService(  # noqa: SLF001
            new_config.image_generation,
            provider=create_image_provider(new_config.image_generation),
            retry_policy=new_config.retry,
            record_system_event=group_chat._conversation_service.record_system_event,  # noqa: SLF001
        )
    if hasattr(group_chat, "_voice_safety_service"):
        group_chat._voice_safety_service = SafetyService(  # noqa: SLF001
            identity_disclosure=new_config.persona.style_profile.identity_disclosure,
            source_user_id=new_config.persona.style_profile.source_user_id,
        )


def _reload_group_reactions(new_config) -> None:
    try:
        import app.plugins.group_reactions as group_reactions
    except Exception:
        return

    group_reactions._config = new_config  # noqa: SLF001
    if hasattr(group_reactions, "_feature_hub"):
        group_reactions._feature_hub = create_runtime_feature_hub(new_config)  # noqa: SLF001
    if hasattr(group_reactions, "_group_mute_repository"):
        group_reactions._group_mute_repository = GroupMuteStateRepository(  # noqa: SLF001
            new_config.storage.database_path
        )


def _memory_text(profile) -> str:
    if profile is None:
        return "还没记什么"
    parts = [
        ("称呼", profile.preferred_name),
        ("摘要", profile.summary),
        ("喜欢", profile.likes),
        ("不喜欢", profile.dislikes),
        ("重要事件", profile.important_events),
        ("安全备注", profile.safety_notes),
        ("更新时间", profile.updated_at),
    ]
    lines = []
    for label, value in parts:
        cleaned = _clean_owner_display_text(value)
        if cleaned:
            lines.append(f"{label}: {cleaned}")
    text = "\n".join(lines)
    return text or "还没记什么"


async def _audit_text() -> str:
    audits = await _audit_repository.get_recent_reply_audits(limit=5)
    events = await _audit_repository.get_recent_system_events(limit=5)
    lines = ["reply_audits:"]
    if audits:
        lines.extend(_format_reply_audit(row) for row in audits)
    else:
        lines.append("none")
    lines.append("system_events:")
    if events:
        lines.extend(_format_system_event(row) for row in events)
    else:
        lines.append("none")
    return "\n".join(lines)


async def _mute_status_text() -> str:
    states = await _group_mute_repository.list_muted(limit=10)
    if not states:
        return "muted_groups=none"
    lines = ["muted_groups:"]
    lines.extend(
        f"{state.group_id} by={state.updated_by} reason={state.reason}"
        for state in states
    )
    return "\n".join(lines)


async def _mute_clear_text(*, updated_by: str, group_id: str | None) -> str:
    cleared = await _group_mute_repository.clear_muted(
        updated_by=updated_by,
        reason="owner_mute_clear",
        group_id=group_id,
    )
    await _conversation_service.record_system_event(
        level="INFO",
        event="group_mute_owner_clear",
        detail=f"group_id={group_id or '*'}; cleared={cleared}; updated_by={updated_by}",
        trace_id=None,
    )
    target = group_id or "all"
    return f"muted_groups_cleared={cleared} target={target}"


async def _remind_list_text(*, user_id: str, include_all: bool) -> str:
    tasks = await _feature_hub.reminders.list_for_user(
        user_id=user_id,
        include_all=include_all,
        limit=10,
    )
    return format_reminder_tasks(tasks)


async def _remind_cancel_text(text: str, *, user_id: str, include_all: bool) -> str:
    task_id = parse_reminder_cancel_id(text)
    if task_id is None:
        return "invalid reminder id"
    cancelled = await _feature_hub.reminders.cancel(
        task_id=task_id,
        user_id=user_id,
        include_all=include_all,
    )
    return f"reminder_cancelled={1 if cancelled else 0} id={task_id}"


async def _ping_model() -> str:
    try:
        client = create_model_client(_config.model)
        reply = await client.generate([{"role": "user", "content": "只回复 ok"}])
    except Exception as exc:
        return f"model failed: {type(exc).__name__}"
    return f"model ok: {reply.text[:30]}"


def _format_reply_audit(row: dict) -> str:
    return (
        f"- {row.get('created_at', '-')}"
        f" action={row.get('action', '-')}"
        f" reason={row.get('reason', '-')}"
        f" scope={row.get('scope_type', '-')}/{row.get('scope_id', '-')}"
        f" user={row.get('user_id', '-')}"
        f" model_called={row.get('model_called', '-')}"
        f" safety_blocked={row.get('safety_blocked', '-')}"
        f" elapsed_ms={row.get('elapsed_ms', '-')}"
    )


def _format_system_event(row: dict) -> str:
    detail = _clean_owner_display_text(row.get("detail", ""))
    text = (
        f"- {row.get('created_at', '-')}"
        f" {row.get('level', '-')}/{row.get('event', '-')}"
        f" trace={row.get('trace_id') or '-'}"
    )
    if detail:
        text += f" detail={detail}"
    return text


def _clean_owner_display_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text.strip():
        return ""
    text = re.sub(r"\[CQ:[^\]]+\]", " ", text)
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"fe_oa_[A-Za-z0-9]+", "fe_oa_[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"sk-[A-Za-z0-9]+", "sk-[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(api[_-]?key|token|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"1[3-9]\d{9}", "[phone]", text)
    text = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", text)
    text = re.sub(r"(?i)\bobject Object\b", " ", text)
    text = re.sub(
        r"(?:图片|表情|动画表情|视频|语音|文件)(?:消息|内容)?",
        " ",
        text,
    )
    text = re.sub(r"\[url\](?:[/?&=A-Za-z0-9_.%-]+)?", "[url]", text)
    text = re.sub(r"\s+", " ", text).strip(" ；;，,。")
    if len(text) > 260:
        return text[:257] + "..."
    return text
