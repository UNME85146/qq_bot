from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from tools.runtime_common import (
    last_model_failure,
    muted_group_states,
    print_json,
    recent_rows,
    table_count,
    tcp_established_on_local_port,
    tcp_listening,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect local QQ bot runtime status.")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--limit", type=int, default=5)
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
            "sourceUserId": config.persona.style_profile.source_user_id,
        },
        "tts": {
            "enabled": config.tts.enabled,
            "privateEnabled": config.tts.private_enabled,
            "groupEnabled": config.tts.group_enabled,
            "provider": config.tts.provider,
            "backend": config.tts.backend,
            "executionProvider": config.tts.execution_provider,
            "endpoint": config.tts.endpoint,
            "defaultVoiceProfileId": config.tts.default_voice_profile_id,
        },
        "counts": {},
        "mutedGroups": [],
        "recentReplyAudits": [],
        "recentSystemEvents": [],
        "recentQueueAudits": [],
        "recentVisionEvents": [],
        "lastModelFailure": None,
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
    print_json(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
