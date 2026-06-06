from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.runtime_common import sanitize_text


MECHANICAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "fake_media_action",
        re.compile(
            r"[（(][^）)]*(?:发送|发|附上|贴|来|整|给你发)"
            r"[^）)]*(?:表情包|表情|图片|图|语音|音频|record|image)[^）)]*[）)]",
            re.IGNORECASE,
        ),
    ),
    (
        "voice_status_text",
        re.compile(
            r"正在语音回复中|语音回复中|念给你听|读给你听|读完了|念完了|"
            r"(?:好|好的)?[，,\s。.!！?？]*现在(?:来|发|回|整).{0,8}语音|"
            r"给你发.{0,6}语音|马上(?:发|回|来).{0,6}语音",
            re.IGNORECASE,
        ),
    ),
    (
        "voice_capability_excuse",
        re.compile(
            r"没有语音功能|没语音功能|不能发语音|发不出语音|发不了语音|脑补|文字代替",
            re.IGNORECASE,
        ),
    ),
    ("ai_disclaimer", re.compile(r"作为\s*AI|语言模型|我是(?:一个)?机器人")),
    ("template_apology", re.compile(r"抱歉|不好意思|很遗憾|无法满足")),
    ("meta_prompt_talk", re.compile(r"QQ风格|系统会|前置逻辑|模型|prompt|TTS|record")),
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only naturalness analysis for recent QQ bot runtime chats.",
    )
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument(
        "--show-assistant-text",
        action="store_true",
        help="Include sanitized assistant reply text for repeated/flagged bot messages.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"error": f"database not found: {db_path}"}, ensure_ascii=False))
        return 1

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        data = analyze(conn, days=args.days, limit=args.limit)
    if not args.show_assistant_text:
        _strip_text_examples(data)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def analyze(conn: sqlite3.Connection, *, days: int, limit: int) -> dict[str, Any]:
    conversations = _recent_conversations(conn, days)
    audits = _recent_audits(conn, days)
    events = _recent_events(conn, days)
    group_index_count = _recent_group_message_index_count(conn, days)

    assistant_by_scope: dict[str, list[sqlite3.Row]] = defaultdict(list)
    user_by_scope: Counter[str] = Counter()
    for row in conversations:
        scope = str(row["scope_type"])
        if row["role"] == "assistant":
            assistant_by_scope[scope].append(row)
        elif row["role"] == "user":
            user_by_scope[scope] += 1

    return {
        "windowDays": days,
        "sample": {
            "conversations": len(conversations),
            "replyAudits": len(audits),
            "systemEvents": len(events),
            "groupMessageIndex": group_index_count,
            "assistantReplies": {
                scope: len(rows) for scope, rows in sorted(assistant_by_scope.items())
            },
            "userRows": dict(sorted(user_by_scope.items())),
        },
        "assistantTextShape": {
            scope: _assistant_text_shape(rows, limit=limit)
            for scope, rows in sorted(assistant_by_scope.items())
        },
        "mechanicalMarkers": _mechanical_marker_summary(
            [row for rows in assistant_by_scope.values() for row in rows],
            limit=limit,
        ),
        "replyAuditReasons": _count_rows(
            audits,
            ["scope_type", "action", "reason", "model_called"],
            limit=limit,
        ),
        "systemEventKinds": _count_rows(events, ["level", "event"], limit=limit),
        "systemEventDetails": _event_detail_summary(events, limit=limit),
        "ttsEvents": _tts_event_summary(events, limit=limit),
        "modelFailures": _model_failure_summary(events, limit=limit),
    }


def _recent_conversations(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM conversations
            WHERE created_at >= datetime('now', ?)
            ORDER BY id ASC
            """,
            (f"-{days} days",),
        )
    )


def _recent_audits(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM reply_audits
            WHERE created_at >= datetime('now', ?)
            ORDER BY id ASC
            """,
            (f"-{days} days",),
        )
    )


def _recent_events(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM system_events
            WHERE created_at >= datetime('now', ?)
            ORDER BY id ASC
            """,
            (f"-{days} days",),
        )
    )


def _recent_group_message_index_count(conn: sqlite3.Connection, days: int) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM group_message_index
            WHERE created_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row is not None else 0


def _assistant_text_shape(rows: list[sqlite3.Row], *, limit: int) -> dict[str, Any]:
    lengths = [_compact_len(str(row["content"] or "")) for row in rows]
    exact_counts = Counter(str(row["content"] or "").strip() for row in rows if row["content"])
    prefix_counts = Counter(
        _prefix_key(str(row["content"] or "")) for row in rows if row["content"]
    )
    return {
        "count": len(lengths),
        "avgChars": round(statistics.fmean(lengths), 2) if lengths else 0,
        "medianChars": round(statistics.median(lengths), 2) if lengths else 0,
        "maxChars": max(lengths) if lengths else 0,
        "oneToTwoChars": sum(1 for length in lengths if 1 <= length <= 2),
        "threeToTwelveChars": sum(1 for length in lengths if 3 <= length <= 12),
        "over80Chars": sum(1 for length in lengths if length > 80),
        "questionEnding": sum(
            1 for row in rows if str(row["content"] or "").strip().endswith(("?", "？"))
        ),
        "topExactReplies": _top_text_counts(exact_counts, limit=limit),
        "topPrefixes": _top_text_counts(prefix_counts, limit=limit),
    }


def _mechanical_marker_summary(
    rows: list[sqlite3.Row],
    *,
    limit: int,
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        text = str(row["content"] or "")
        for name, pattern in MECHANICAL_PATTERNS:
            if not pattern.search(text):
                continue
            counts[name] += 1
            if len(examples[name]) < limit:
                examples[name].append(
                    {
                        "scope": row["scope_type"],
                        "createdAt": row["created_at"],
                        "text": _safe_example(text),
                    }
                )
    return {
        "counts": dict(counts.most_common()),
        "examples": dict(examples),
    }


def _count_rows(
    rows: list[sqlite3.Row],
    keys: list[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    counter: Counter[tuple[Any, ...]] = Counter(
        tuple(row[key] for key in keys) for row in rows
    )
    output = []
    for values, count in counter.most_common(limit):
        item = {key: value for key, value in zip(keys, values, strict=True)}
        item["count"] = count
        output.append(item)
    return output


def _tts_event_summary(rows: list[sqlite3.Row], *, limit: int) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        event = str(row["event"] or "")
        if not event.startswith("tts_"):
            continue
        detail = str(row["detail"] or "")
        scope = _detail_value(detail, "scope")
        reason = _detail_value(detail, "reason")
        counter[(event, scope, reason)] += 1
    return [
        {"event": event, "scope": scope, "reason": reason, "count": count}
        for (event, scope, reason), count in counter.most_common(limit)
    ]


def _event_detail_summary(rows: list[sqlite3.Row], *, limit: int) -> dict[str, list[dict[str, Any]]]:
    selected_events = {
        "sticker_analysis_failed",
        "vision_generate_failed",
        "tts_generate_failed",
        "tts_fallback_text_sent",
        "model_generate_failed",
    }
    counters: dict[str, Counter[str]] = {event: Counter() for event in selected_events}
    for row in rows:
        event = str(row["event"] or "")
        if event not in counters:
            continue
        counters[event][_detail_fingerprint(str(row["detail"] or ""))] += 1
    return {
        event: [{"count": count, "detail": detail} for detail, count in counter.most_common(limit)]
        for event, counter in sorted(counters.items())
        if counter
    }


def _model_failure_summary(rows: list[sqlite3.Row], *, limit: int) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    for row in rows:
        if row["event"] != "model_generate_failed":
            continue
        detail = str(row["detail"] or "")
        counter[(_detail_value(detail, "category"), _detail_value(detail, "status"))] += 1
    return [
        {"category": category, "status": status, "count": count}
        for (category, status), count in counter.most_common(limit)
    ]


def _top_text_counts(counter: Counter[str], *, limit: int) -> list[dict[str, Any]]:
    return [
        {"count": count, "text": _safe_example(text)}
        for text, count in counter.most_common(limit)
        if text
    ]


def _strip_text_examples(data: dict[str, Any]) -> None:
    for scope_data in data.get("assistantTextShape", {}).values():
        for key in ("topExactReplies", "topPrefixes"):
            for item in scope_data.get(key, []):
                item["textHash"] = _text_hash(str(item.pop("text", "")))
    for examples in data.get("mechanicalMarkers", {}).get("examples", {}).values():
        for item in examples:
            item["textHash"] = _text_hash(str(item.pop("text", "")))


def _safe_example(text: str) -> str:
    cleaned = sanitize_text(text)
    cleaned = re.sub(r"\b\d{5,}\b", "[number]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] + ("..." if len(cleaned) > 120 else "")


def _compact_len(text: str) -> int:
    return len("".join(str(text or "").split()))


def _prefix_key(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    return cleaned[:16]


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _detail_value(detail: str, key: str) -> str:
    match = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+)", detail)
    return match.group(1).strip() if match else ""


def _detail_fingerprint(detail: str) -> str:
    cleaned = sanitize_text(str(detail or ""))
    cleaned = re.sub(r"asset_id=[^;\s]+", "asset_id=[hash]", cleaned)
    cleaned = re.sub(r"trace[-_a-zA-Z0-9]*", "trace=[hash]", cleaned)
    cleaned = re.sub(r"\b[0-9a-f]{8,}\b", "[hash]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d{5,}\b", "[number]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:180] + ("..." if len(cleaned) > 180 else "")


if __name__ == "__main__":
    raise SystemExit(main())
