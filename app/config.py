from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.models import (
    AppConfig,
    LimitsConfig,
    LoggingConfig,
    ModelConfig,
    OneBotConfig,
    PersonaConfig,
    QQConfig,
    ReplyConfig,
    StorageConfig,
    StyleProfileConfig,
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
    limits = raw["limits"]
    storage = raw["storage"]
    logging = raw["logging"]

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
    )


def _to_str_set(values: list[Any]) -> set[str]:
    return {str(value) for value in values if str(value).strip()}


def _load_persona_config(raw: dict[str, Any], config_dir: Path) -> PersonaConfig:
    mode = str(raw.get("mode", "legacy_persona"))
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
    )
    return PersonaConfig(
        mode="legacy_persona",
        profile_path="",
        fallback_profile_path="",
        style_profile=profile,
    )


def _load_style_profile(profile_path: Path, fallback_profile_path: Path) -> StyleProfileConfig:
    selected_path = profile_path if profile_path.exists() else fallback_profile_path
    if not selected_path.exists():
        raise FileNotFoundError(
            "Style profile file not found: "
            f"{profile_path} or fallback {fallback_profile_path}"
        )
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
    )


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
