from __future__ import annotations

from pathlib import Path

from tools.runtime_common import (
    last_model_failure,
    muted_group_states,
    recent_rows,
    sanitize_text,
    table_count,
    tcp_established_on_local_port,
    tcp_listening,
)


def build_owner_status_text(config) -> str:
    bot_listening = tcp_listening(config.onebot.host, config.onebot.port)
    napcat_webui = tcp_listening("127.0.0.1", 6099)
    napcat_ws = tcp_established_on_local_port(config.onebot.host, config.onebot.port)
    db_path = Path(config.storage.database_path)
    lines = [
        f"selfId={config.qq.self_id}",
        f"bot={'on' if bot_listening else 'off'} napcat={'on' if napcat_webui else 'off'} ws={'on' if napcat_ws else 'off'}",
        f"model={config.model.provider}/{config.model.name}",
        f"persona={config.persona.mode}/{config.persona.style_profile.source_user_id}",
        (
            f"tts={'on' if config.tts.enabled else 'off'} "
            f"private={'on' if config.tts.private_enabled else 'off'} "
            f"group={'on' if config.tts.group_enabled else 'off'} "
            f"provider={config.tts.provider}/{config.tts.backend}/"
            f"{config.tts.execution_provider} "
            f"profile={config.tts.default_voice_profile_id}"
        ),
        f"private={_short_join(config.qq.allowed_private_user_ids)}",
        f"groups={_short_join(config.qq.allowed_group_ids)}",
    ]
    if not db_path.exists():
        lines.append("db not ready")
        return "\n".join(lines)

    try:
        counts = {
            table: table_count(db_path, table)
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
            )
        }
        lines.append(
            "db "
            + " ".join(f"{key}={value}" for key, value in counts.items())
        )
        failure = last_model_failure(db_path)
        if failure is None:
            lines.append("last_model_failure=none")
        else:
            lines.append(
                "last_model_failure="
                + sanitize_text(
                    f"{failure.get('created_at', '')} {failure.get('detail', '')}"
                )
            )
        muted_groups = muted_group_states(db_path, limit=3)
        if muted_groups:
            lines.append(
                "muted_groups="
                + ",".join(str(row.get("group_id", "")) for row in muted_groups)
            )
        queue_text = _group_queue_text()
        if queue_text:
            lines.append(queue_text)
        audits = recent_rows(db_path, "reply_audits", limit=3)
    except Exception as exc:
        lines.append(f"db not ready: {type(exc).__name__}")
        return "\n".join(lines)

    if audits:
        lines.append("recent_audits:")
        lines.extend(
            f"{row.get('action')}/{row.get('reason')}/{row.get('scope_type')}/{row.get('user_id')}"
            for row in audits
        )
    else:
        lines.append("recent_audits=none")
    return "\n".join(lines)


def _short_join(values: set[str], *, limit: int = 6) -> str:
    sorted_values = sorted(values)
    if len(sorted_values) <= limit:
        return ",".join(sorted_values) or "-"
    visible = ",".join(sorted_values[:limit])
    return f"{visible},+{len(sorted_values) - limit}"


def _group_queue_text() -> str:
    try:
        from app.plugins import group_chat

        status = group_chat.group_queue_status()
    except Exception:
        return ""
    queues = status.get("queues", {})
    if not isinstance(queues, dict) or not queues:
        return "group_queues=empty"
    parts = []
    for group_id, value in queues.items():
        if isinstance(value, dict):
            parts.append(
                f"{group_id}:{value.get('size', 0)}/"
                f"{value.get('oldestWaitSeconds', 0)}s"
            )
    return "group_queues=" + (",".join(parts) if parts else "empty")
