from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.features.speech_provider import speech_endpoint_url
from tools.runtime_common import (
    last_model_failure,
    muted_group_states,
    print_json,
    recent_rows,
    table_count,
    tcp_established_on_local_port,
    tcp_listening,
)
from tools.manage_tts_retirement import (
    TtsRetirementManager,
    TtsRetirementSpec,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect local QQ bot runtime status.")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only connection readiness fields; omits ids and runtime records.",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit non-zero unless the bot listener and OneBot connection are ready.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = Path(args.db)
    data = {
        "qq_bot_listening": tcp_listening(config.onebot.host, config.onebot.port),
        "napcat_webui_listening": tcp_listening("127.0.0.1", 6099),
        "napcat_to_bot_established": tcp_established_on_local_port(
            config.onebot.host,
            config.onebot.port,
        ),
        "selfId": config.qq.self_id,
        "allowedPrivateUsers": sorted(config.qq.allowed_private_user_ids),
        "allowedGroups": sorted(config.qq.allowed_group_ids),
        "persona": {
            "mode": config.persona.mode,
            "updatedAt": config.persona.style_profile.updated_at,
            "hasCharacterSummary": (
                config.persona.mode == "history_derived_character"
                and bool(config.persona.style_profile.character_summary.strip())
            ),
        },
        "speech": {
            "enabled": config.speech.enabled,
            "privateEnabled": config.speech.private_enabled,
            "groupEnabled": config.speech.group_enabled,
            "apiMode": config.speech.api_mode,
            "randomReplyEnabled": config.speech.random_reply_enabled,
            "maxAudioBytes": config.speech.max_audio_bytes,
            "endpoint": _speech_endpoint(config.speech),
            "model": config.speech.model,
            "voice": config.speech.voice,
            "format": config.speech.format,
        },
        "counts": {},
        "mutedGroups": [],
        "recentReplyAudits": [],
        "recentSystemEvents": [],
        "recentQueueAudits": [],
        "recentVisionEvents": [],
        "lastModelFailure": None,
        **historical_tts_status(),
    }
    if db_path.exists():
        for table in (
            "conversations",
            "reply_audits",
            "system_events",
            "memory_profiles",
            "group_contexts",
            "group_mute_states",
            "bot_sent_messages",
            "group_pending_questions",
            "scheduled_tasks",
            "sticker_assets",
            "sticker_asset_analysis",
            "group_message_index",
            "message_repeat_states",
            "group_semantic_terms",
        ):
            try:
                data["counts"][table] = table_count(db_path, table)
            except Exception:
                data["counts"][table] = "not_ready"
        try:
            data["mutedGroups"] = muted_group_states(db_path, limit=args.limit)
        except Exception:
            data["mutedGroups"] = []
        data["recentReplyAudits"] = recent_rows(db_path, "reply_audits", limit=args.limit)
        data["recentSystemEvents"] = recent_rows(db_path, "system_events", limit=args.limit)
        data["recentQueueAudits"] = [
            row
            for row in recent_rows(db_path, "reply_audits", limit=max(args.limit * 5, 10))
            if str(row.get("reason", "")).startswith("group_reply_")
            or str(row.get("reason", "")).startswith("group_thread_")
            or str(row.get("reason", "")).startswith("group_queue_")
        ][: args.limit]
        data["recentVisionEvents"] = [
            row
            for row in recent_rows(db_path, "system_events", limit=max(args.limit * 5, 10))
            if "vision" in str(row.get("event", ""))
        ][: args.limit]
        data["lastModelFailure"] = last_model_failure(db_path)
    print_json(runtime_summary(data) if args.summary else data)
    return 1 if args.require_ready and not runtime_ready(data) else 0


def runtime_ready(data: dict[str, object]) -> bool:
    return bool(data.get("qq_bot_listening")) and bool(
        data.get("napcat_to_bot_established")
    )


def runtime_summary(data: dict[str, object]) -> dict[str, object]:
    return {
        "qq_bot_listening": bool(data.get("qq_bot_listening")),
        "napcat_webui_listening": bool(data.get("napcat_webui_listening")),
        "napcat_to_bot_established": bool(data.get("napcat_to_bot_established")),
        "ready": runtime_ready(data),
        "historical_tts": data.get("historical_tts", "inspection_unavailable"),
        "tts_rollback_packages": data.get("tts_rollback_packages", []),
    }


def historical_tts_status() -> dict[str, object]:
    if not _historical_tts_supported_platform():
        return {
            "historical_tts": "not_applicable",
            "tts_rollback_packages": [],
        }
    try:
        status = TtsRetirementManager(TtsRetirementSpec.production()).status(
            verify_hashes=False
        )
    except Exception:
        return {
            "historical_tts": "inspection_failed",
            "tts_rollback_packages": [],
        }
    return {
        "historical_tts": status["historical_tts"],
        "tts_rollback_packages": status["rollback_packages"],
    }


def _historical_tts_supported_platform() -> bool:
    return os.name == "posix"


def _speech_endpoint(config) -> str:
    return speech_endpoint_url(config) if config.base_url else "unconfigured"


if __name__ == "__main__":
    raise SystemExit(main())
