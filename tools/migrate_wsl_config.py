from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
from typing import Any


WIKIMEDIA_CORE_ZH = "https://api.wikimedia.org/core/v1/wikipedia/zh/search/page"

DEFAULT_NEWS_FEEDS = {
    "politics": ("https://feeds.bbci.co.uk/news/world/rss.xml",),
    "business": (
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    ),
    "technology": (
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    ),
    "finance": ("https://www.cnbc.com/id/10000664/device/rss/rss.html",),
}


def migrate_runtime_config(
    source: dict[str, Any],
    *,
    video_cache_host: str,
    video_cache_container: str,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ValueError("runtime config must be a JSON object")
    migrated = copy.deepcopy(source)
    model = _object(migrated.get("model"))

    migrated.pop("tts", None)
    migrated["persona"] = {
        "mode": "history_derived_character",
        "profilePath": "persona_profile.local.json",
    }
    conversation_sessions = _merge_defaults(
        migrated.get("conversationSessions"),
        {
            "inactivitySeconds": 900,
            "chatDelayMinMs": 0,
            "chatDelayMaxMs": 0,
            "relationTimeoutSeconds": 1.2,
            "pendingRetentionDays": 30,
            "groupModelTimeoutSeconds": 18,
        },
    )
    conversation_sessions.pop("contextualSafetyTimeoutSeconds", None)
    migrated["conversationSessions"] = conversation_sessions
    migrated["retry"] = _merge_defaults(
        migrated.get("retry"),
        {
            "maxAttempts": 3,
            "timeoutMultipliers": [1, 2, 3],
            "backoffSeconds": [2, 5],
        },
    )
    migrated["video"] = _merge_defaults(
        migrated.get("video"),
        {
            "perMessageConcurrency": 3,
            "globalConcurrency": 6,
            "downloadTimeoutSeconds": 300,
            "sendTimeoutSeconds": 90,
            "qqVideoMaxBytes": None,
            "minFreeBytes": 0,
            "httpProxyEnv": "QQ_BOT_VIDEO_HTTP_PROXY",
            "socksProxyEnv": "QQ_BOT_VIDEO_SOCKS_PROXY",
            "cookieFileEnv": "QQ_BOT_VIDEO_COOKIE_FILE",
            "progressThresholdSeconds": 3,
            "domainFailureThreshold": 2,
            "domainRecoverySeconds": 120,
            "canonicalUrlCacheSeconds": 3600,
            "backoffJitterSeconds": 0.5,
        },
    )
    migrated["video"].pop("backoffBaseSeconds", None)
    migrated["video"].update(
        {
            "enabled": True,
            "hostCachePath": video_cache_host,
            "containerCachePath": video_cache_container,
        }
    )

    news = _merge_defaults(
        migrated.get("news"),
        {"defaultTime": "08:00", "timezone": "Asia/Shanghai"},
    )
    news["enabled"] = True
    configured_feeds = news.get("feeds")
    if not isinstance(configured_feeds, dict) or not any(configured_feeds.values()):
        news["feeds"] = {
            category: list(urls) for category, urls in DEFAULT_NEWS_FEEDS.items()
        }
    migrated["news"] = news

    markets = _merge_defaults(
        migrated.get("markets"),
        {
            "alertThresholdPercent": 3,
            "pollIntervalSeconds": 300,
            "commandTimeoutSeconds": 20,
            "providerTimeoutSeconds": 8,
            "circuitFailureThreshold": 3,
            "circuitRecoverySeconds": 60,
            "aShareFallbacks": [
                {
                    "provider": "sina",
                    "baseUrl": "https://hq.sinajs.cn",
                    "apiKeyEnv": "",
                }
            ],
        },
    )
    markets["enabled"] = True
    markets["aShare"] = _provider_with_default(markets.get("aShare"), "akshare")
    markets["usShare"] = _provider_with_default(markets.get("usShare"), "yfinance")
    migrated["markets"] = markets

    migrated["search"] = {
        "enabled": True,
        "provider": "wikipedia",
        "baseUrl": WIKIMEDIA_CORE_ZH,
        "apiKeyEnv": "",
    }

    allowed_speech_fields = {
        "baseUrl",
        "apiKeyEnv",
        "model",
        "voice",
        "format",
        "timeoutSeconds",
        "sendTimeoutSeconds",
        "cacheDir",
        "maxChars",
        "privateEnabled",
        "groupEnabled",
        "privateCooldownSeconds",
        "groupCooldownSeconds",
    }
    current_speech = _object(migrated.get("speech"))
    speech = {
        key: copy.deepcopy(value)
        for key, value in current_speech.items()
        if key in allowed_speech_fields
    }
    speech = _merge_defaults(
        speech,
        {
            "baseUrl": "",
            "apiKeyEnv": "",
            "model": "",
            "voice": "",
            "format": "mp3",
            "timeoutSeconds": 60,
            "sendTimeoutSeconds": 60,
            "cacheDir": "runtime_artifacts/speech",
            "maxChars": 4096,
            "privateEnabled": True,
            "groupEnabled": True,
            "privateCooldownSeconds": 30,
            "groupCooldownSeconds": 60,
        },
    )
    speech["enabled"] = False
    migrated["speech"] = speech

    image_enabled = bool(model.get("baseUrl") and model.get("apiKeyEnv"))
    migrated["imageGeneration"] = {
        "enabled": image_enabled,
        "baseUrl": str(model.get("baseUrl") or ""),
        "apiKeyEnv": str(model.get("apiKeyEnv") or ""),
        "generationEndpoint": "/images/generations",
        "editEndpoint": "/images/edits",
        "model": "gpt-image-2",
        "timeoutSeconds": 120,
        "sendTimeoutSeconds": 60,
        "cacheDir": "runtime_artifacts/image-generation",
        "editWindowSeconds": 180,
    }
    return migrated


def validate_history_profile(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("persona profile must be a JSON object")
    if not str(payload.get("sourceUserId") or "").strip():
        raise ValueError("persona profile sourceUserId must not be empty")
    if not str(payload.get("updatedAt") or "").strip():
        raise ValueError("persona profile updatedAt must not be empty")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError("persona profile metrics must be a non-empty object")
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"persona profile metric must be numeric: {key}")
        if not math.isfinite(float(value)):
            raise ValueError(f"persona profile metric must be finite: {key}")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a migrated WSL private config.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--video-cache-host", required=True)
    parser.add_argument("--video-cache-container", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    validate_history_profile(profile)
    migrated = migrate_runtime_config(
        source,
        video_cache_host=args.video_cache_host,
        video_cache_container=args.video_cache_container,
    )
    write_json_atomic(Path(args.output), migrated)
    return 0


def _merge_defaults(value: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    merged = _object(copy.deepcopy(value))
    for key, default in defaults.items():
        merged.setdefault(key, copy.deepcopy(default))
    return merged


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _provider_with_default(value: Any, provider: str) -> dict[str, Any]:
    result = _merge_defaults(value, {"baseUrl": "", "apiKeyEnv": ""})
    if not str(result.get("provider") or "").strip():
        result["provider"] = provider
    return result


if __name__ == "__main__":
    raise SystemExit(main())
