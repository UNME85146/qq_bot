from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.runtime_common import sanitize_text


BATTLE_REASONS = {
    "context_sticker_battle_sent",
    "context_sticker_missing_text_sent",
    "context_sticker_missing",
}
POKE_EVENTS = {
    "poke_notice_replied",
    "poke_notice_skipped",
    "poke_notice_reply_failed",
}
ORDINARY_ACTION_WORDS = ("斗图", "接一下", "表情包", "表情", "图")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check recent QQ human-action acceptance evidence.",
    )
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--since", default=None, help="SQLite datetime lower bound.")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"status": "error", "error": "db_not_found", "db": str(db_path)}))
        return 2

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        data = check_acceptance(
            conn,
            since=args.since,
            hours=args.hours,
            limit=args.limit,
        )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data["status"] in {"pass", "pending"} else 1


def check_acceptance(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    hours: float,
    limit: int,
) -> dict[str, Any]:
    where_sql, params = _time_filter(since=since, hours=hours)
    battle_rows = _reply_audits(conn, where_sql, params, BATTLE_REASONS, limit=limit)
    poke_rows = _system_events(conn, where_sql, params, POKE_EVENTS, limit=limit)
    ordinary_mentions = (
        _ordinary_action_mentions(conn, where_sql, params, limit=limit)
        if _table_exists(conn, "group_message_index")
        else []
    )
    fake_action_rows = (
        _fake_action_rows(conn, where_sql, params, limit=limit)
        if _table_exists(conn, "conversations")
        else []
    )
    battle_prompt_seen = bool(battle_rows)
    battle_message_keys = {
        _message_key(row)
        for row in ordinary_mentions
        if row.get("audit_reason") in BATTLE_REASONS
    }
    unsafe_ordinary_replies = [
        row
        for row in ordinary_mentions
        if _message_key(row) not in battle_message_keys
        and row.get("audit_reason") not in {None, "group_not_triggered"}
    ]

    checks = {
        "battleActionEvidence": _check_state(bool(battle_rows), battle_prompt_seen),
        "pokeActionEvidence": _check_state(bool(poke_rows), False),
        "ordinaryMentionNoUnexpectedReply": "pass" if not unsafe_ordinary_replies else "fail",
        "noFakeMediaActionText": "pass" if not fake_action_rows else "fail",
    }
    status = _overall_status(checks)
    return {
        "status": status,
        "window": {"since": since, "hours": hours},
        "checks": checks,
        "battleAudits": battle_rows,
        "pokeEvents": poke_rows,
        "ordinaryActionMentions": ordinary_mentions,
        "fakeActionRows": fake_action_rows,
        "nextManualChecks": _next_manual_checks(checks),
    }


def _time_filter(*, since: str | None, hours: float) -> tuple[str, tuple[Any, ...]]:
    if since:
        return "created_at >= ?", (since,)
    return "created_at >= datetime('now', ?)", (f"-{hours:g} hours",)


def _reply_audits(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
    reasons: set[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in reasons)
    rows = conn.execute(
        f"""
        SELECT id, trace_id, scope_type, scope_id, user_id, action, reason, model_called,
               safety_blocked, created_at
        FROM reply_audits
        WHERE {where_sql}
          AND reason IN ({placeholders})
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, *sorted(reasons), limit),
    )
    return [_row_dict(row) for row in rows]


def _system_events(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
    events: set[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in events)
    rows = conn.execute(
        f"""
        SELECT id, level, event, detail, trace_id, created_at
        FROM system_events
        WHERE {where_sql}
          AND event IN ({placeholders})
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, *sorted(events), limit),
    )
    return [_row_dict(row) for row in rows]


def _ordinary_action_mentions(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT g.group_id, g.message_id, g.user_id, g.user_name, g.text, g.created_at,
               r.action AS audit_action, r.reason AS audit_reason, r.model_called
        FROM group_message_index AS g
        LEFT JOIN reply_audits AS r
          ON r.scope_type = 'group'
         AND r.scope_id = g.group_id
         AND r.user_id = g.user_id
         AND r.created_at >= g.created_at
         AND r.created_at <= datetime(g.created_at, '+30 seconds')
        WHERE g.{where_sql}
          AND g.is_bot = 0
          AND g.text != ''
          AND (
            g.text LIKE '%斗图%'
            OR g.text LIKE '%接一下%'
            OR g.text LIKE '%表情包%'
            OR g.text LIKE '%表情%'
          )
        ORDER BY g.rowid DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    result = []
    seen_keys: set[tuple[Any, ...]] = set()
    for row in rows:
        item = _row_dict(row)
        key = (
            item.get("group_id"),
            item.get("message_id"),
            item.get("audit_reason"),
            item.get("model_called"),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        item["text"] = sanitize_text(str(item.get("text") or ""))
        result.append(item)
    return result


def _fake_action_rows(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT id, scope_type, scope_id, role, content, created_at
        FROM conversations
        WHERE {where_sql}
          AND role = 'assistant'
          AND (
            content LIKE '%发送一个表情包%'
            OR content LIKE '%发一个表情包%'
            OR content LIKE '%正在语音回复中%'
            OR content LIKE '%念给你听%'
            OR content LIKE '%读完了%'
            OR content LIKE '%我戳回%'
          )
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    result = []
    for row in rows:
        item = _row_dict(row)
        item["content"] = sanitize_text(str(item.get("content") or ""))
        result.append(item)
    return result


def _check_state(has_evidence: bool, prompt_seen: bool) -> str:
    if has_evidence:
        return "pass"
    if prompt_seen:
        return "fail"
    return "pending"


def _overall_status(checks: dict[str, str]) -> str:
    if any(value == "fail" for value in checks.values()):
        return "fail"
    if any(value == "pending" for value in checks.values()):
        return "pending"
    return "pass"


def _next_manual_checks(checks: dict[str, str]) -> list[str]:
    prompts = []
    if checks["battleActionEvidence"] == "pending":
        prompts.append("在白名单群发送：@机器人 斗图 / @机器人 接一下 / @机器人 用表情包回")
    if checks["pokeActionEvidence"] == "pending":
        prompts.append("在白名单群对机器人执行戳一戳")
    if checks["ordinaryMentionNoUnexpectedReply"] != "pass":
        prompts.append("观察普通未 @ 群聊提到表情包/斗图时是否仍保持沉默")
    if checks["noFakeMediaActionText"] != "pass":
        prompts.append("检查 conversations 中的假媒体动作文本并继续收敛 ReplyFormatter")
    return prompts


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _message_key(row: dict[str, Any]) -> tuple[Any, Any]:
    return row.get("group_id"), row.get("message_id")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


if __name__ == "__main__":
    raise SystemExit(main())
