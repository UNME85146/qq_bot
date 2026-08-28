from __future__ import annotations

import hashlib
import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def analyze_group(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    since: str,
    until: str,
    limit: int = 20,
) -> dict[str, Any]:
    if not group_id.strip():
        raise ValueError("group_id is required")
    if until <= since:
        raise ValueError("until must be after since")
    params = (group_id, since, until)
    messages = (
        _one(
            conn,
            """
            SELECT COUNT(*) AS indexed,
                   COALESCE(SUM(CASE WHEN is_bot = 0 THEN 1 ELSE 0 END), 0) AS human,
                   COALESCE(SUM(CASE WHEN is_bot = 1 THEN 1 ELSE 0 END), 0) AS bot
            FROM group_message_index
            WHERE group_id = ? AND created_at >= ? AND created_at < ?
            """,
            params,
        )
        if _has_table(conn, "group_message_index")
        else {"indexed": 0, "human": 0, "bot": 0}
    )
    conversations = _group_counts(
        conn,
        """
        SELECT role, COUNT(*) AS count
        FROM conversations
        WHERE scope_type = 'group' AND scope_id = ?
          AND created_at >= ? AND created_at < ?
        GROUP BY role
        """,
        params,
    )
    audits = _group_counts(
        conn,
        """
        SELECT reason || ':' || action AS name, COUNT(*) AS count
        FROM reply_audits
        WHERE scope_type = 'group' AND scope_id = ?
          AND created_at >= ? AND created_at < ?
        GROUP BY reason, action
        ORDER BY count DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    delivery = _delivery_summary(conn, params)
    sent = (
        _one(
            conn,
            """
            SELECT COUNT(*) AS messages, COUNT(DISTINCT trace_id) AS traces,
                   COUNT(DISTINCT original_message_id) AS originals
            FROM bot_sent_messages
            WHERE group_id = ? AND created_at >= ? AND created_at < ?
            """,
            params,
        )
        if _has_table(conn, "bot_sent_messages")
        else {"messages": 0, "traces": 0, "originals": 0}
    )
    reply_shape = _reply_shape(conn, params, limit=limit)
    content_categories = _content_categories(conn, params) if _has_table(conn, "group_message_index") else {}
    mute_state = _mute_state(conn, group_id) if _has_table(conn, "group_mute_states") else None
    latency = _latency_summary(conn, params)
    return {
        "window": {
            "groupId": group_id,
            "since": since,
            "until": until,
        },
        "messages": {
            "indexed": int(messages["indexed"] or 0),
            "human": int(messages["human"] or 0),
            "bot": int(messages["bot"] or 0),
        },
        "conversations": {
            "user": int(conversations.get("user", 0)),
            "assistant": int(conversations.get("assistant", 0)),
        },
        "auditReasons": audits,
        "delivery": delivery,
        "botSent": {
            "messages": int(sent["messages"] or 0),
            "traces": int(sent["traces"] or 0),
            "originals": int(sent["originals"] or 0),
        },
        "replyShape": reply_shape,
        "contentCategories": content_categories,
        "latencyMs": latency,
        "muteState": mute_state,
    }


def _delivery_summary(conn: sqlite3.Connection, params: tuple[str, str, str]) -> dict[str, int]:
    has_response_text = _has_column(conn, "reply_audits", "response_text")
    has_delivery_status = _has_column(conn, "reply_audits", "delivery_status")
    response_expr = "response_text" if has_response_text else "NULL"
    status_expr = "delivery_status" if has_delivery_status else "NULL"
    row = _one(
        conn,
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN {status_expr} = 'sent' THEN 1 ELSE 0 END), 0) AS sent,
          COALESCE(SUM(CASE WHEN {status_expr} = 'partial' THEN 1 ELSE 0 END), 0) AS partial,
          COALESCE(SUM(CASE WHEN {status_expr} = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
          COALESCE(SUM(CASE WHEN {response_expr} IS NOT NULL THEN 1 ELSE 0 END), 0) AS coverage
        FROM reply_audits
        WHERE scope_type = 'group' AND scope_id = ?
          AND created_at >= ? AND created_at < ?
        """,
        params,
    )
    return {key: int(row[key] or 0) for key in ("sent", "partial", "failed", "coverage")}


def _reply_shape(
    conn: sqlite3.Connection,
    params: tuple[str, str, str],
    *,
    limit: int,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT content
        FROM conversations
        WHERE scope_type = 'group' AND scope_id = ? AND role = 'assistant'
          AND created_at >= ? AND created_at < ?
        ORDER BY id
        """,
        params,
    ).fetchall()
    texts = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]
    normalized = [" ".join(text.split()) for text in texts]
    counts = Counter(normalized)
    lengths = sorted(len(text) for text in normalized)
    return {
        "assistant": len(texts),
        "averageChars": round(sum(lengths) / len(lengths), 2) if lengths else 0,
        "medianChars": _percentile(lengths, 50),
        "maxChars": max(lengths, default=0),
        "duplicateTexts": sum(count - 1 for count in counts.values() if count > 1),
        "textExamples": [],
        "textHashes": [
            {"hash": _text_hash(text), "count": count}
            for text, count in counts.most_common(limit)
        ],
    }


def _content_categories(
    conn: sqlite3.Connection,
    params: tuple[str, str, str],
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT text, media_type
        FROM group_message_index
        WHERE group_id = ? AND is_bot = 0
          AND created_at >= ? AND created_at < ?
        """,
        params,
    ).fetchall()
    counts: Counter[str] = Counter()
    for row in rows:
        text = str(row[0] or "")
        media_type = str(row[1] or "").lower()
        compact = "".join(text.split()).lower()
        if "?" in text or "？" in text:
            counts["question"] += 1
        if any(marker in compact for marker in ("c++", "python", "api", "ai", "模型", "代码", "数据库", "bug", "openai")):
            counts["tech_ai"] += 1
        if any(marker in compact for marker in ("股票", "行情", "a股", "美股", "大盘", "基金", "涨", "跌")):
            counts["market"] += 1
        if media_type or any(marker in compact for marker in ("图片", "视频", "抖音", "b站", "bilibili")):
            counts["media"] += 1
        if media_type in {"sticker", "face"} or any(marker in compact for marker in ("表情", "斗图", "贴纸")):
            counts["sticker"] += 1
        if any(marker in compact for marker in ("哈哈", "笑", "晚安", "早睡", "喜欢", "卧槽", "牛逼", "生气")):
            counts["emotion_social"] += 1
        if any(marker in compact for marker in ("语音", "朗读", "念", "读给")):
            counts["voice"] += 1
        if any(marker in compact for marker in ("http://", "https://", "链接")):
            counts["link"] += 1
    return dict(sorted(counts.items()))


def _latency_summary(conn: sqlite3.Connection, params: tuple[str, str, str]) -> dict[str, int | float]:
    rows = conn.execute(
        """
        SELECT elapsed_ms
        FROM reply_audits
        WHERE scope_type = 'group' AND scope_id = ? AND model_called = 1
          AND elapsed_ms IS NOT NULL AND created_at >= ? AND created_at < ?
        """,
        params,
    ).fetchall()
    values = sorted(int(row[0]) for row in rows)
    return {
        "count": len(values),
        "p50": _percentile(values, 50),
        "p90": _percentile(values, 90),
        "p95": _percentile(values, 95),
        "max": max(values, default=0),
        "over18000": sum(value > 18_000 for value in values),
    }


def _mute_state(conn: sqlite3.Connection, group_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT mode, muted, updated_at, reason
        FROM group_mute_states
        WHERE group_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (group_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "mode": str(row[0] or "normal"),
        "muted": bool(row[1]),
        "updatedAt": row[2],
        "reason": row[3],
    }


def _group_counts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in conn.execute(sql, params)}


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return {}
    return row


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * percentile / 100) - 1))
    return values[index]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze one QQ group without printing message text.")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    until = args.until or datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    until_dt = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
    since = args.since or (until_dt - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"database not found: {db_path}")
    db_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        print(json.dumps(analyze_group(conn, group_id=args.group_id, since=since, until=until, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
