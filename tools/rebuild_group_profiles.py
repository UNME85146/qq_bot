from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.memory.group_member_profile_service import aggregate_group_member_records
from app.safety.safety_service import SafetyService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild one group's low-sensitivity member profiles from a runtime DB window."
    )
    parser.add_argument("--database", required=True, help="SQLite runtime database path.")
    parser.add_argument("--group-id", required=True, help="Target group id.")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days.")
    parser.add_argument(
        "--reference-time",
        help="Aware ISO timestamp used as the window end; defaults to current UTC.",
    )
    parser.add_argument(
        "--bot-user-id",
        help="Optional bot user id to exclude in addition to rows marked is_bot.",
    )
    parser.add_argument("--report-output", help="Optional aggregate-only JSON report path.")
    args = parser.parse_args()
    report = rebuild_group_profiles(
        Path(args.database),
        group_id=str(args.group_id),
        lookback_days=args.days,
        reference_time=_parse_reference_time(args.reference_time),
        bot_user_id=str(args.bot_user_id) if args.bot_user_id else None,
    )
    if args.report_output:
        output = Path(args.report_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def rebuild_group_profiles(
    database_path: str | Path,
    *,
    group_id: str,
    lookback_days: int = 30,
    reference_time: datetime | None = None,
    bot_user_id: str | None = None,
) -> dict[str, Any]:
    if not str(group_id).strip():
        raise ValueError("group_id must not be empty")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    end_at = reference_time or datetime.now(UTC)
    if end_at.tzinfo is None:
        raise ValueError("reference_time must be timezone-aware")
    end_at = end_at.astimezone(UTC)
    start_at = end_at - timedelta(days=lookback_days)
    path = Path(database_path)
    if not path.exists():
        raise FileNotFoundError(path)
    start_text = start_at.strftime("%Y-%m-%d %H:%M:%S")
    end_text = end_at.strftime("%Y-%m-%d %H:%M:%S")

    rows: list[dict[str, object]] = []
    skipped_bot_records = 0
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        source_rows = conn.execute(
            """
            SELECT user_id, user_name, text, media_type, is_bot, created_at
            FROM group_message_index
            WHERE group_id = ?
              AND created_at >= ?
              AND created_at < ?
            ORDER BY datetime(created_at), rowid
            """,
            (str(group_id), start_text, end_text),
        ).fetchall()
        for row in source_rows:
            if bool(row["is_bot"]) or (
                bot_user_id is not None and str(row["user_id"]) == bot_user_id
            ):
                skipped_bot_records += 1
                continue
            rows.append(dict(row))

    aggregates, aggregate_stats = aggregate_group_member_records(
        rows,
        safety_service=SafetyService(),
    )
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for user_id, aggregate in aggregates.items():
            conn.execute(
                """
                INSERT INTO group_member_profiles (
                  group_id, user_id, display_name, summary, metrics_json,
                  preference_notes, message_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  summary = excluded.summary,
                  metrics_json = excluded.metrics_json,
                  preference_notes = excluded.preference_notes,
                  message_count = excluded.message_count,
                  updated_at = datetime('now')
                """,
                (
                    str(group_id),
                    user_id,
                    aggregate["display_name"],
                    aggregate["summary"],
                    json.dumps(aggregate["metrics"], ensure_ascii=False, sort_keys=True),
                    aggregate["preference_notes"],
                    int(aggregate["message_count"]),
                ),
            )
        conn.commit()

    return {
        "groupId": str(group_id),
        "windowStart": start_at.isoformat(),
        "windowEnd": end_at.isoformat(),
        "lookbackDays": lookback_days,
        "sourceRecords": aggregate_stats["source_records"] + skipped_bot_records,
        "eligibleRecords": aggregate_stats["eligible_records"],
        "eligibleUsers": len(aggregates),
        "updatedUsers": len(aggregates),
        "skippedSensitiveRecords": aggregate_stats["skipped_sensitive_records"],
        "skippedBotRecords": skipped_bot_records,
    }


def _parse_reference_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reference-time must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("reference-time must include a timezone")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
