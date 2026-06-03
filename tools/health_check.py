from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from tools.runtime_common import (
    last_model_failure,
    sanitize_text,
    tcp_established_on_local_port,
    tcp_listening,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a read-only QQ bot health check.")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--db", default="")
    parser.add_argument("--log-dir", default="")
    parser.add_argument("--recent-event-limit", type=int, default=50)
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = Path(args.db or config.storage.database_path)
    log_dir = Path(args.log_dir or config.logging.log_dir)
    data = build_health_report(
        config=config,
        db_path=db_path,
        log_dir=log_dir,
        recent_event_limit=args.recent_event_limit,
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def build_health_report(
    *,
    config: Any,
    db_path: Path,
    log_dir: Path,
    recent_event_limit: int = 50,
) -> dict[str, Any]:
    qq_bot_listening = tcp_listening(config.onebot.host, config.onebot.port)
    napcat_webui_listening = tcp_listening("127.0.0.1", 6099)
    napcat_to_bot_established = tcp_established_on_local_port(
        config.onebot.host,
        config.onebot.port,
    )
    report: dict[str, Any] = {
        "checkedAt": datetime.now(UTC).isoformat(),
        "status": "ok",
        "warnings": [],
        "qqBotListening": qq_bot_listening,
        "napcatWebuiListening": napcat_webui_listening,
        "napcatToBotEstablished": napcat_to_bot_established,
        "selfId": config.qq.self_id,
        "allowedGroups": sorted(config.qq.allowed_group_ids),
        "persona": {
            "mode": config.persona.mode,
            "sourceUserId": config.persona.style_profile.source_user_id,
        },
        "db": _db_health(db_path, recent_event_limit),
        "logs": _log_health(log_dir),
    }
    if not qq_bot_listening:
        report["warnings"].append("qq_bot_not_listening")
    if not napcat_webui_listening:
        report["warnings"].append("napcat_webui_not_listening")
    if not napcat_to_bot_established:
        report["warnings"].append("napcat_to_bot_not_established")
    if report["db"].get("lastModelFailure") is not None:
        report["warnings"].append("model_failure_seen")
    if report["logs"].get("lastBotOfflineAt"):
        report["warnings"].append("bot_offline_seen_in_logs")
    if report["logs"].get("lastKickedOfflineAt"):
        report["warnings"].append("napcat_kicked_offline_seen_in_logs")
    if report["warnings"]:
        report["status"] = "warn"
    if not qq_bot_listening:
        report["status"] = "down"
    return report


def _db_health(db_path: Path, recent_event_limit: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "sizeBytes": db_path.stat().st_size if db_path.exists() else 0,
        "counts": {},
        "lastConversationAt": None,
        "lastReplyAuditAt": None,
        "recentErrorEventCount": 0,
        "lastModelFailure": None,
    }
    if not db_path.exists():
        return data
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            for table in (
                "conversations",
                "reply_audits",
                "system_events",
                "memory_profiles",
                "group_contexts",
                "group_mute_states",
                "bot_sent_messages",
                "group_pending_questions",
            ):
                data["counts"][table] = _safe_count(conn, table)
            data["lastConversationAt"] = _max_created_at(conn, "conversations")
            data["lastReplyAuditAt"] = _max_created_at(conn, "reply_audits")
            data["recentErrorEventCount"] = _recent_error_event_count(
                conn,
                recent_event_limit,
            )
        data["lastModelFailure"] = last_model_failure(db_path)
    except sqlite3.Error as exc:
        data["error"] = sanitize_text(f"{type(exc).__name__}: {exc}")
    return data


def _log_health(log_dir: Path) -> dict[str, Any]:
    files = list(log_dir.glob("*.log")) if log_dir.exists() else []
    data: dict[str, Any] = {
        "path": str(log_dir),
        "exists": log_dir.exists(),
        "sizeBytes": sum(path.stat().st_size for path in files),
        "lastBotConnectedAt": None,
        "lastBotOfflineAt": None,
        "lastKickedOfflineAt": None,
    }
    out_log = log_dir / "bot-systemd.out.log"
    if out_log.exists():
        connected_at, offline_at = _scan_bot_log(out_log)
        data["lastBotConnectedAt"] = connected_at
        data["lastBotOfflineAt"] = offline_at
    napcat_log = log_dir / "napcat.log"
    if napcat_log.exists():
        data["lastKickedOfflineAt"] = _scan_kicked_log(napcat_log)
    return data


def _safe_count(conn: sqlite3.Connection, table: str) -> int | str:
    try:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cursor.fetchone()[0])
    except sqlite3.Error:
        return "not_ready"


def _max_created_at(conn: sqlite3.Connection, table: str) -> str | None:
    try:
        cursor = conn.execute(f"SELECT MAX(created_at) FROM {table}")
        value = cursor.fetchone()[0]
    except sqlite3.Error:
        return None
    return str(value) if value else None


def _recent_error_event_count(conn: sqlite3.Connection, limit: int) -> int:
    try:
        cursor = conn.execute(
            """
            SELECT level FROM system_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return sum(1 for row in cursor.fetchall() if str(row["level"]).upper() == "ERROR")
    except sqlite3.Error:
        return 0


def _scan_bot_log(path: Path) -> tuple[str | None, str | None]:
    connected_at: str | None = None
    offline_at: str | None = None
    for line in _tail_lines(path, max_lines=2000):
        timestamp = _extract_log_timestamp(line)
        lowered = line.lower()
        if "bot " in lowered and " connected" in lowered:
            connected_at = timestamp or connected_at
        if any(marker in lowered for marker in ("bot_offline", "kickedoffline")):
            offline_at = timestamp or offline_at
    return connected_at, offline_at


def _scan_kicked_log(path: Path) -> str | None:
    kicked_at: str | None = None
    for line in _tail_lines(path, max_lines=2000):
        timestamp = _extract_log_timestamp(line)
        if "kickedoffline" in line.lower():
            kicked_at = timestamp or kicked_at
    return kicked_at


def _tail_lines(path: Path, *, max_lines: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return []


def _extract_log_timestamp(line: str) -> str | None:
    match = re.match(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line)
    if match:
        return match.group(0)
    match = re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", line)
    return match.group(0) if match else None


if __name__ == "__main__":
    raise SystemExit(main())
