from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from app.persona.history_character import (
    HistoryCharacterMetrics,
    build_behavior_profile,
    build_character_summary,
    build_reply_rules,
    build_style_summary,
    build_tone_rules,
)
from app.models import (
    AppConfig,
    BehaviorProfileConfig,
    ConversationSessionsConfig,
    ImageGenerationConfig,
    LimitsConfig,
    LoggingConfig,
    MarketProviderConfig,
    MarketsConfig,
    ModelConfig,
    NewsConfig,
    OneBotConfig,
    PersonaConfig,
    PresenceConfig,
    QQConfig,
    ReplyConfig,
    RetryConfig,
    SearchConfig,
    SpeechConfig,
    StorageConfig,
    StyleProfileConfig,
    VideoConfig,
)


def load_config(path: str | Path = "config/config.json") -> AppConfig:
    load_dotenv()
    config_path = Path(path)
    if not config_path.exists():
        config_path = config_path.with_name("config.example.json")

    with config_path.open("r", encoding="utf-8") as file:
        raw: dict[str, Any] = json.load(file)

    qq = raw["qq"]
    onebot = raw["onebot"]
    model = raw["model"]
    persona = raw["persona"]
    reply = raw["reply"]
    presence = raw.get("presence", {})
    limits = raw["limits"]
    storage = raw["storage"]
    logging = raw["logging"]
    conversation_sessions = _config_block(raw, "conversationSessions")
    retry = _config_block(raw, "retry")
    video = _config_block(raw, "video")
    news = _config_block(raw, "news")
    markets = _config_block(raw, "markets")
    search = _config_block(raw, "search")
    speech = _config_block(raw, "speech")
    image_generation = _config_block(raw, "imageGeneration")

    api_key_env = str(model["apiKeyEnv"])
    api_key = os.getenv(api_key_env) or None

    persona_config = _load_persona_config(persona, config_path.parent)

    return AppConfig(
        qq=QQConfig(
            self_id=str(qq["selfId"]),
            root_user_ids=_to_str_set(qq.get("rootUserIds", qq.get("ownerUserIds", []))),
            owner_user_ids=_to_str_set(qq.get("ownerUserIds", [])),
            allowed_private_user_ids=_to_str_set(qq.get("allowedPrivateUserIds", [])),
            allowed_group_ids=_to_str_set(qq.get("allowedGroupIds", [])),
            memory_allowed_user_ids=_to_str_set(qq.get("memoryAllowedUserIds", [])),
            nicknames=_to_str_set(qq.get("nicknames", [])),
            group_mute_controller_user_ids=_to_str_set(
                qq.get("groupMuteControllerUserIds", qq.get("ownerUserIds", []))
            ),
        ),
        onebot=OneBotConfig(
            mode=str(onebot["mode"]),
            access_token_env=str(onebot["accessTokenEnv"]),
            host=str(onebot["host"]),
            port=int(onebot["port"]),
            api_root=str(onebot["apiRoot"]),
        ),
        model=ModelConfig(
            provider=str(model["provider"]),
            api_key_env=api_key_env,
            base_url=str(model["baseUrl"]).rstrip("/"),
            name=str(model["name"]),
            timeout_seconds=float(model["timeoutSeconds"]),
            temperature=float(model["temperature"]),
            max_tokens=int(model["maxTokens"]),
            api_key=api_key,
            use_mock=api_key is None,
            reasoning_effort=(
                str(model.get("reasoningEffort") or "").strip().lower() or None
            ),
            base_url_candidates=_load_model_base_url_candidates(model),
            endpoint_probe_interval_seconds=_load_endpoint_probe_interval(model),
        ),
        persona=persona_config,
        reply=ReplyConfig(
            private_always_reply=bool(reply["privateAlwaysReply"]),
            group_mention_reply=bool(reply["groupMentionReply"]),
            nickname_reply_probability=float(reply["nicknameReplyProbability"]),
            active_window_seconds=int(reply["activeWindowSeconds"]),
            min_delay_ms=int(reply["minDelayMs"]),
            max_delay_ms=int(reply["maxDelayMs"]),
            max_reply_length=int(reply["maxReplyLength"]),
            long_text_max_length=int(reply.get("longTextMaxLength", 2200)),
            long_text_max_bubbles=int(reply.get("longTextMaxBubbles", 8)),
        ),
        presence=PresenceConfig(
            focus_window_seconds=int(presence.get("focusWindowSeconds", 180)),
            base_online_probability=float(presence.get("baseOnlineProbability", 0.08)),
            focused_repeat_probability=float(
                presence.get("focusedRepeatProbability", 0.35)
            ),
            unfocused_repeat_probability=float(
                presence.get("unfocusedRepeatProbability", 0.05)
            ),
            plus_one_repeat_probability=float(
                presence.get("plusOneRepeatProbability", 0.45)
            ),
            sticker_repeat_probability=float(
                presence.get("stickerRepeatProbability", 0.25)
            ),
            text_repeat_probability=float(presence.get("textRepeatProbability", 0.08)),
        ),
        limits=LimitsConfig(
            private_cooldown_seconds=float(limits["privateCooldownSeconds"]),
            group_cooldown_seconds=float(limits["groupCooldownSeconds"]),
            max_user_messages_per_minute=int(limits["maxUserMessagesPerMinute"]),
            max_group_messages_per_minute=int(limits["maxGroupMessagesPerMinute"]),
            model_failure_break_count=int(limits["modelFailureBreakCount"]),
            model_failure_break_seconds=int(limits["modelFailureBreakSeconds"]),
        ),
        storage=StorageConfig(
            database_path=str(storage["databasePath"]),
            backup_dir=str(storage["backupDir"]),
            save_raw_model_text=bool(storage["saveRawModelText"]),
            save_raw_user_message=bool(storage["saveRawUserMessage"]),
        ),
        logging=LoggingConfig(
            level=str(logging["level"]),
            log_dir=str(logging["logDir"]),
            sanitize_message_content=bool(logging["sanitizeMessageContent"]),
        ),
        conversation_sessions=_load_conversation_sessions_config(conversation_sessions),
        retry=_load_retry_config(retry),
        video=_load_video_config(video),
        news=_load_news_config(news),
        markets=_load_markets_config(markets),
        search=_load_search_config(search),
        speech=_load_speech_config(speech),
        image_generation=_load_image_generation_config(image_generation),
    )


def _to_str_set(values: list[Any]) -> set[str]:
    return {str(value) for value in values if str(value).strip()}


def _config_block(raw: dict[str, Any], name: str) -> dict[str, Any]:
    block = raw.get(name, {})
    if not isinstance(block, dict):
        raise ValueError(f"{name} must be an object")
    return block


def _load_model_base_url_candidates(model: dict[str, Any]) -> tuple[str, ...]:
    raw_candidates = model.get("baseUrlCandidates")
    if raw_candidates is None:
        return ()
    if not isinstance(raw_candidates, list):
        raise ValueError("model.baseUrlCandidates must be an array")
    if not raw_candidates:
        return ()

    candidates: list[str] = []
    for value in raw_candidates:
        if not isinstance(value, str):
            raise ValueError("model.baseUrlCandidates must contain strings")
        normalized = _normalize_model_base_url(value)
        if normalized not in candidates:
            candidates.append(normalized)
    if len(candidates) < 2:
        raise ValueError("model.baseUrlCandidates must contain at least two distinct URLs")
    return tuple(candidates)


def _normalize_model_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("model.baseUrlCandidates entries must be HTTPS base URLs")
    return raw


def _load_endpoint_probe_interval(model: dict[str, Any]) -> float:
    value = float(model.get("endpointProbeIntervalSeconds", 60.0))
    if value < 10.0 or value > 3600.0:
        raise ValueError("model.endpointProbeIntervalSeconds must be between 10 and 3600")
    return value


def _load_conversation_sessions_config(raw: dict[str, Any]) -> ConversationSessionsConfig:
    config = ConversationSessionsConfig(
        inactivity_seconds=int(raw.get("inactivitySeconds", 900)),
        chat_delay_min_ms=int(raw.get("chatDelayMinMs", 0)),
        chat_delay_max_ms=int(raw.get("chatDelayMaxMs", 0)),
        relation_timeout_seconds=float(raw.get("relationTimeoutSeconds", 1.2)),
        pending_retention_days=int(raw.get("pendingRetentionDays", 30)),
        group_model_timeout_seconds=float(raw.get("groupModelTimeoutSeconds", 18.0)),
    )
    if config.inactivity_seconds <= 0:
        raise ValueError("conversationSessions.inactivitySeconds must be positive")
    if config.chat_delay_min_ms < 0 or config.chat_delay_max_ms < 0:
        raise ValueError("conversationSessions chat delays must be non-negative")
    if config.chat_delay_min_ms > config.chat_delay_max_ms:
        raise ValueError(
            "conversationSessions.chatDelayMinMs must not exceed chatDelayMaxMs"
        )
    if config.relation_timeout_seconds <= 0:
        raise ValueError("conversationSessions.relationTimeoutSeconds must be positive")
    if config.pending_retention_days <= 0:
        raise ValueError("conversationSessions.pendingRetentionDays must be positive")
    if config.group_model_timeout_seconds <= 0:
        raise ValueError("conversationSessions.groupModelTimeoutSeconds must be positive")
    return config


def _load_retry_config(raw: dict[str, Any]) -> RetryConfig:
    config = RetryConfig(
        max_attempts=int(raw.get("maxAttempts", 3)),
        timeout_multipliers=tuple(
            float(value) for value in raw.get("timeoutMultipliers", [1, 2, 3])
        ),
        backoff_seconds=tuple(
            float(value) for value in raw.get("backoffSeconds", [2, 5])
        ),
    )
    if config.max_attempts <= 0:
        raise ValueError("retry.maxAttempts must be positive")
    if len(config.timeout_multipliers) != config.max_attempts:
        raise ValueError("retry.maxAttempts must equal timeoutMultipliers length")
    if len(config.backoff_seconds) != config.max_attempts - 1:
        raise ValueError("retry.backoffSeconds length must be maxAttempts minus one")
    if any(value <= 0 for value in config.timeout_multipliers):
        raise ValueError("retry.timeoutMultipliers must be positive")
    if any(value < 0 for value in config.backoff_seconds):
        raise ValueError("retry.backoffSeconds must be non-negative")
    return config


def _load_video_config(raw: dict[str, Any]) -> VideoConfig:
    max_bytes = raw.get("qqVideoMaxBytes")
    config = VideoConfig(
        enabled=bool(raw.get("enabled", False)),
        host_cache_path=str(raw.get("hostCachePath", "runtime_artifacts/video-cache")),
        container_cache_path=str(
            raw.get("containerCachePath", "/path/in/container/qq-bot-media")
        ),
        per_message_concurrency=int(raw.get("perMessageConcurrency", 3)),
        global_concurrency=int(raw.get("globalConcurrency", 6)),
        download_timeout_seconds=float(raw.get("downloadTimeoutSeconds", 300.0)),
        send_timeout_seconds=float(raw.get("sendTimeoutSeconds", 90.0)),
        qq_video_max_bytes=int(max_bytes) if max_bytes is not None else None,
        min_free_bytes=int(raw.get("minFreeBytes", 0)),
        http_proxy_env=str(
            raw.get("httpProxyEnv", "QQ_BOT_VIDEO_HTTP_PROXY")
        ).strip(),
        socks_proxy_env=str(
            raw.get("socksProxyEnv", "QQ_BOT_VIDEO_SOCKS_PROXY")
        ).strip(),
        cookie_file_env=str(
            raw.get("cookieFileEnv", "QQ_BOT_VIDEO_COOKIE_FILE")
        ).strip(),
        progress_threshold_seconds=float(raw.get("progressThresholdSeconds", 1.0)),
        domain_failure_threshold=int(raw.get("domainFailureThreshold", 2)),
        domain_recovery_seconds=float(raw.get("domainRecoverySeconds", 120.0)),
        canonical_url_cache_seconds=float(raw.get("canonicalUrlCacheSeconds", 3600.0)),
        backoff_jitter_seconds=float(raw.get("backoffJitterSeconds", 0.5)),
    )
    if not config.host_cache_path.strip() or not config.container_cache_path.strip():
        raise ValueError("video cache paths must not be empty")
    if config.per_message_concurrency <= 0 or config.global_concurrency <= 0:
        raise ValueError("video concurrency must be positive")
    if config.global_concurrency < config.per_message_concurrency:
        raise ValueError("video.globalConcurrency must be at least perMessageConcurrency")
    if config.download_timeout_seconds <= 0 or config.send_timeout_seconds <= 0:
        raise ValueError("video timeouts must be positive")
    if config.qq_video_max_bytes is not None and config.qq_video_max_bytes <= 0:
        raise ValueError("video.qqVideoMaxBytes must be positive when configured")
    if config.min_free_bytes < 0:
        raise ValueError("video.minFreeBytes must be non-negative")
    if not all(
        (config.http_proxy_env, config.socks_proxy_env, config.cookie_file_env)
    ):
        raise ValueError("video environment variable names must not be empty")
    if config.progress_threshold_seconds < 0:
        raise ValueError("video.progressThresholdSeconds must be non-negative")
    if config.domain_failure_threshold <= 0:
        raise ValueError("video.domainFailureThreshold must be positive")
    if config.domain_recovery_seconds <= 0:
        raise ValueError("video.domainRecoverySeconds must be positive")
    if config.canonical_url_cache_seconds <= 0:
        raise ValueError("video.canonicalUrlCacheSeconds must be positive")
    if config.backoff_jitter_seconds < 0:
        raise ValueError("video.backoffJitterSeconds must be non-negative")
    return config


def _load_news_config(raw: dict[str, Any]) -> NewsConfig:
    raw_feeds = raw.get("feeds", {})
    if not isinstance(raw_feeds, dict):
        raise ValueError("news.feeds must be an object")
    feeds = {
        str(category): tuple(str(url) for url in urls)
        for category, urls in raw_feeds.items()
    }
    if not feeds:
        feeds = NewsConfig().feeds
    config = NewsConfig(
        enabled=bool(raw.get("enabled", False)),
        default_time=str(raw.get("defaultTime", "08:00")),
        timezone=str(raw.get("timezone", "Asia/Shanghai")),
        feeds=feeds,
    )
    if not _is_hhmm(config.default_time):
        raise ValueError("news.defaultTime must use HH:MM")
    supported_categories = {"politics", "business", "technology", "finance"}
    unsupported_categories = set(config.feeds) - supported_categories
    if unsupported_categories:
        raise ValueError(
            "news.feeds contains unsupported category: "
            + ", ".join(sorted(unsupported_categories))
        )
    return config


def _load_market_provider_config(raw: dict[str, Any]) -> MarketProviderConfig:
    return MarketProviderConfig(
        provider=str(raw.get("provider", "")).strip(),
        base_url=str(raw.get("baseUrl", "")).rstrip("/"),
        api_key_env=str(raw.get("apiKeyEnv", "")).strip(),
    )


def _load_markets_config(raw: dict[str, Any]) -> MarketsConfig:
    raw_fallbacks = raw.get("aShareFallbacks", [{"provider": "sina"}])
    if not isinstance(raw_fallbacks, list):
        raise ValueError("markets.aShareFallbacks must be an array")
    config = MarketsConfig(
        enabled=bool(raw.get("enabled", False)),
        alert_threshold_percent=float(raw.get("alertThresholdPercent", 3.0)),
        poll_interval_seconds=int(raw.get("pollIntervalSeconds", 300)),
        command_timeout_seconds=float(raw.get("commandTimeoutSeconds", 20.0)),
        provider_timeout_seconds=float(raw.get("providerTimeoutSeconds", 8.0)),
        circuit_failure_threshold=int(raw.get("circuitFailureThreshold", 3)),
        circuit_recovery_seconds=float(raw.get("circuitRecoverySeconds", 60.0)),
        a_share=_load_market_provider_config(raw.get("aShare", {})),
        a_share_fallbacks=tuple(
            _load_market_provider_config(item)
            for item in raw_fallbacks
            if isinstance(item, dict)
        ),
        us_share=_load_market_provider_config(raw.get("usShare", {})),
    )
    if config.alert_threshold_percent < 0:
        raise ValueError("markets.alertThresholdPercent must be non-negative")
    if config.poll_interval_seconds <= 0:
        raise ValueError("markets.pollIntervalSeconds must be positive")
    if config.command_timeout_seconds <= 0:
        raise ValueError("markets.commandTimeoutSeconds must be positive")
    if config.provider_timeout_seconds <= 0:
        raise ValueError("markets.providerTimeoutSeconds must be positive")
    if config.provider_timeout_seconds >= config.command_timeout_seconds:
        raise ValueError(
            "markets.providerTimeoutSeconds must be less than commandTimeoutSeconds"
        )
    if config.circuit_failure_threshold <= 0:
        raise ValueError("markets.circuitFailureThreshold must be positive")
    if config.circuit_recovery_seconds <= 0:
        raise ValueError("markets.circuitRecoverySeconds must be positive")
    return config


def _load_search_config(raw: dict[str, Any]) -> SearchConfig:
    config = SearchConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=str(raw.get("provider", "")).strip().lower(),
        base_url=str(raw.get("baseUrl", "")).rstrip("/"),
        api_key_env=str(raw.get("apiKeyEnv", "")).strip(),
    )
    if config.provider not in {"", "searxng", "brave", "wikipedia"}:
        raise ValueError("search.provider must be empty, searxng, brave, or wikipedia")
    return config


def _load_speech_config(raw: dict[str, Any]) -> SpeechConfig:
    moss_fields = {
        "provider",
        "backend",
        "executionProvider",
        "endpoint",
        "voiceProfiles",
        "promptAudioPath",
    }
    unexpected = moss_fields & set(raw)
    if unexpected:
        raise ValueError(
            "speech must use generic OpenAI-compatible fields, not: "
            + ", ".join(sorted(unexpected))
        )
    config = SpeechConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("baseUrl", "")).rstrip("/"),
        api_key_env=str(raw.get("apiKeyEnv", "")).strip(),
        model=str(raw.get("model", "")).strip(),
        voice=str(raw.get("voice", "")).strip(),
        format=str(raw.get("format", "mp3")).strip().lower(),
        timeout_seconds=float(raw.get("timeoutSeconds", 60.0)),
        send_timeout_seconds=float(raw.get("sendTimeoutSeconds", 60.0)),
        cache_dir=str(raw.get("cacheDir", "runtime_artifacts/speech")).strip(),
        max_chars=int(raw.get("maxChars", 4096)),
        private_enabled=bool(raw.get("privateEnabled", True)),
        group_enabled=bool(raw.get("groupEnabled", True)),
        private_cooldown_seconds=float(raw.get("privateCooldownSeconds", 30.0)),
        group_cooldown_seconds=float(raw.get("groupCooldownSeconds", 60.0)),
    )
    if config.timeout_seconds <= 0:
        raise ValueError("speech.timeoutSeconds must be positive")
    if config.send_timeout_seconds <= 0:
        raise ValueError("speech.sendTimeoutSeconds must be positive")
    if config.max_chars <= 0:
        raise ValueError("speech.maxChars must be positive")
    if not config.cache_dir:
        raise ValueError("speech.cacheDir must not be empty")
    if config.format not in {"mp3", "opus", "aac", "flac", "wav", "pcm"}:
        raise ValueError("speech.format is unsupported")
    return config


def _load_image_generation_config(raw: dict[str, Any]) -> ImageGenerationConfig:
    config = ImageGenerationConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("baseUrl", "")).rstrip("/"),
        api_key_env=str(raw.get("apiKeyEnv", "")).strip(),
        generation_endpoint=str(
            raw.get("generationEndpoint", "/images/generations")
        ).strip(),
        edit_endpoint=str(raw.get("editEndpoint", "/images/edits")).strip(),
        model=str(raw.get("model", "")).strip(),
        timeout_seconds=float(raw.get("timeoutSeconds", 120.0)),
        send_timeout_seconds=float(raw.get("sendTimeoutSeconds", 60.0)),
        cache_dir=str(raw.get("cacheDir", "runtime_artifacts/image-generation")).strip(),
        edit_window_seconds=int(raw.get("editWindowSeconds", 180)),
    )
    if config.timeout_seconds <= 0:
        raise ValueError("imageGeneration.timeoutSeconds must be positive")
    if config.send_timeout_seconds <= 0:
        raise ValueError("imageGeneration.sendTimeoutSeconds must be positive")
    if config.edit_window_seconds < 0:
        raise ValueError("imageGeneration.editWindowSeconds must be non-negative")
    return config


def _is_hhmm(value: str) -> bool:
    if re.fullmatch(r"\d{2}:\d{2}", value) is None:
        return False
    hours, minutes = (int(part) for part in value.split(":"))
    return 0 <= hours <= 23 and 0 <= minutes <= 59


def _load_persona_config(raw: dict[str, Any], config_dir: Path) -> PersonaConfig:
    mode = str(raw.get("mode", "legacy_persona"))
    if mode == "history_derived_character":
        profile_path = str(raw.get("profilePath", "")).strip()
        if not profile_path:
            raise ValueError(
                "persona history_derived_character missing required field: profilePath"
            )
        profile = _load_history_character_profile(
            _resolve_config_path(profile_path, config_dir)
        )
        return PersonaConfig(
            mode=mode,
            profile_path=profile_path,
            fallback_profile_path="",
            style_profile=profile,
        )

    if mode == "fixed_style_profile":
        try:
            profile_path = str(raw["profilePath"])
            fallback_profile_path = str(raw["fallbackProfilePath"])
        except KeyError as exc:
            raise ValueError(
                f"persona fixed_style_profile missing required field: {exc.args[0]}"
            ) from exc
        profile = _load_style_profile(
            _resolve_config_path(profile_path, config_dir),
            _resolve_config_path(fallback_profile_path, config_dir),
        )
        return PersonaConfig(
            mode=mode,
            profile_path=profile_path,
            fallback_profile_path=fallback_profile_path,
            style_profile=profile,
        )

    profile = StyleProfileConfig(
        source_user_id="legacy",
        identity_disclosure=str(raw.get("disclosure", "我是测试号，不是真实自然人。")),
        style_summary="旧版 persona 配置兼容模式。",
        tone_rules=[
            str(raw.get("personality", "")),
            str(raw.get("lifeStyle", "")),
            str(raw.get("speechStyle", "")),
        ],
        topic_biases=[],
        lexicon=[],
        reply_rules=["回复要像 QQ 好友即时聊天，大多数回复 1-3 句，短一点，自然一点。"],
        avoid_rules=["不要编造真实学校、住址、手机号、身份证等身份信息。"],
        few_shot_examples=[],
        updated_at=None,
        character_summary="旧版 persona 配置兼容模式。",
        behavior_profile=BehaviorProfileConfig(),
    )
    return PersonaConfig(
        mode="legacy_persona",
        profile_path="",
        fallback_profile_path="",
        style_profile=profile,
    )


def _load_style_profile(
    profile_path: Path,
    fallback_profile_path: Path | None = None,
) -> StyleProfileConfig:
    selected_path = profile_path
    if not selected_path.exists() and fallback_profile_path is not None:
        selected_path = fallback_profile_path
    if not selected_path.exists():
        fallback_detail = (
            f" or fallback {fallback_profile_path}" if fallback_profile_path is not None else ""
        )
        raise FileNotFoundError(f"Style profile file not found: {profile_path}{fallback_detail}")
    with selected_path.open("r", encoding="utf-8") as file:
        raw: dict[str, Any] = json.load(file)
    required_fields = {
        "sourceUserId",
        "identityDisclosure",
        "styleSummary",
        "toneRules",
        "topicBiases",
        "lexicon",
        "replyRules",
        "avoidRules",
        "fewShotExamples",
        "updatedAt",
    }
    missing_fields = sorted(required_fields - set(raw))
    if missing_fields:
        raise ValueError(
            "Style profile missing required field(s): " + ", ".join(missing_fields)
        )
    return StyleProfileConfig(
        source_user_id=str(raw["sourceUserId"]),
        identity_disclosure=str(raw["identityDisclosure"]),
        style_summary=str(raw["styleSummary"]),
        tone_rules=_to_str_list(raw["toneRules"]),
        topic_biases=_to_str_list(raw["topicBiases"]),
        lexicon=_to_str_list(raw["lexicon"]),
        reply_rules=_to_str_list(raw["replyRules"]),
        avoid_rules=_to_str_list(raw["avoidRules"]),
        few_shot_examples=_to_str_list(raw["fewShotExamples"]),
        updated_at=str(raw["updatedAt"]) if raw.get("updatedAt") else None,
        character_summary=str(raw.get("characterSummary") or raw["styleSummary"]),
        behavior_profile=_load_behavior_profile(raw.get("behaviorProfile", {})),
    )


def _load_history_character_profile(profile_path: Path) -> StyleProfileConfig:
    if not profile_path.exists():
        raise FileNotFoundError(f"History-derived character profile not found: {profile_path}")
    with profile_path.open("r", encoding="utf-8") as file:
        raw: dict[str, Any] = json.load(file)
    source_user_id = str(raw.get("sourceUserId") or "").strip()
    if not source_user_id:
        raise ValueError("History-derived character sourceUserId must not be empty")
    metrics = HistoryCharacterMetrics.from_payload(raw.get("metrics"))
    updated_at = _load_history_character_updated_at(raw.get("updatedAt"))
    behavior = build_behavior_profile(metrics)
    return StyleProfileConfig(
        source_user_id=source_user_id,
        identity_disclosure="我是小黄，一个从过往聊天习惯中形成自己说话方式的 QQ 聊天机器人。",
        character_summary=build_character_summary(metrics),
        style_summary=build_style_summary(metrics),
        tone_rules=build_tone_rules(metrics),
        topic_biases=[],
        lexicon=[],
        reply_rules=build_reply_rules(metrics),
        avoid_rules=[
            "不要冒充历史记录中的任何人",
            "不要编造真实学校、公司、住址、手机号、财务和身份信息",
            "不要复述完整聊天记录",
            "不要过度攻击或辱骂",
            "不要客服腔",
        ],
        few_shot_examples=[],
        updated_at=updated_at,
        behavior_profile=_load_behavior_profile(behavior),
    )


def _load_history_character_updated_at(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("History-derived character updatedAt must be an ISO timestamp")
    value = raw.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "History-derived character updatedAt must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError("History-derived character updatedAt must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _resolve_config_path(path: str, config_dir: Path) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    cwd_path = Path.cwd() / resolved
    if cwd_path.exists() or len(resolved.parts) > 1:
        return cwd_path
    return config_dir / resolved


def _to_str_list(values: list[Any]) -> list[str]:
    return [str(value) for value in values if str(value).strip()]


def _load_behavior_profile(raw: Any) -> BehaviorProfileConfig:
    if not isinstance(raw, dict):
        return BehaviorProfileConfig()
    return BehaviorProfileConfig(
        reply_cadence=_to_str_list(raw.get("replyCadence") or []),
        punctuation_profile=_to_str_list(raw.get("punctuationProfile") or []),
        interaction_habits=_to_str_list(raw.get("interactionHabits") or []),
        chat_action_rules=_to_str_list(raw.get("chatActionRules") or []),
    )
