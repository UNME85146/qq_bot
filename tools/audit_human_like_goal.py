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

from tools.check_human_actions_acceptance import check_acceptance


REQUIRED_BEHAVIOR_KEYS = {
    "replyCadence",
    "punctuationProfile",
    "interactionHabits",
    "chatActionRules",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit evidence for the human-like QQ bot goal.",
    )
    parser.add_argument("--profile", default="config/persona_profile.local.json")
    parser.add_argument(
        "--style-report",
        default="data/backups/persona/style_profile_build_report.latest.json",
    )
    parser.add_argument(
        "--history-report",
        default="data/backups/persona/history_sources_inspection.latest.json",
    )
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--min-readable-messages",
        type=int,
        default=100_000,
        help="Minimum readable historical messages required to satisfy the full-history part of the goal.",
    )
    parser.add_argument(
        "--fail-on-incomplete",
        action="store_true",
        help="Exit non-zero when the overall audit is not complete.",
    )
    args = parser.parse_args()

    data = audit_goal(
        profile_path=Path(args.profile),
        style_report_path=Path(args.style_report),
        history_report_path=Path(args.history_report),
        db_path=Path(args.db),
        hours=args.hours,
        limit=args.limit,
        min_readable_messages=args.min_readable_messages,
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.fail_on_incomplete and data["status"] != "complete":
        return 1
    return 0


def audit_goal(
    *,
    profile_path: Path,
    style_report_path: Path,
    history_report_path: Path,
    db_path: Path,
    hours: float,
    limit: int,
    min_readable_messages: int,
) -> dict[str, Any]:
    requirements = {
        "readableFullHistory": _audit_readable_full_history(
            history_report_path,
            min_readable_messages=min_readable_messages,
        ),
        "behaviorProfileBuilt": _audit_behavior_profile(
            profile_path,
            style_report_path,
        ),
        "humanActionsRuntimeVerified": _audit_human_actions(
            db_path,
            hours=hours,
            limit=limit,
        ),
    }
    status = "complete" if all(
        requirement["status"] == "pass" for requirement in requirements.values()
    ) else "incomplete"
    return {
        "status": status,
        "requirements": requirements,
        "nextSteps": _next_steps(requirements),
    }


def _audit_readable_full_history(
    report_path: Path,
    *,
    min_readable_messages: int,
) -> dict[str, Any]:
    if not report_path.exists():
        return {
            "status": "missing",
            "evidence": {"report": str(report_path), "exists": False},
            "reason": "history_source_report_missing",
        }
    report = _load_json(report_path)
    readable_messages = int(report.get("readableExportMessages") or 0)
    sqlite_candidates = report.get("sqlite") if isinstance(report.get("sqlite"), list) else []
    unreadable_sqlite = [
        {
            "path": item.get("path"),
            "sizeBytes": item.get("sizeBytes"),
            "normalSqliteHeader": item.get("normalSqliteHeader"),
            "qqNtHeader": item.get("qqNtHeader"),
            "qqNtMarker": item.get("qqNtMarker"),
            "formatHint": item.get("formatHint"),
            "readableByStdSqlite": item.get("readableByStdSqlite"),
            "sqliteError": item.get("sqliteError"),
            "nextStep": item.get("nextStep"),
        }
        for item in sqlite_candidates
        if not item.get("readableByStdSqlite")
    ]
    return {
        "status": "pass" if readable_messages >= min_readable_messages else "fail",
        "evidence": {
            "report": str(report_path),
            "readableExportMessages": readable_messages,
            "minReadableMessages": min_readable_messages,
            "exports": _export_summary(report),
            "unreadableSqliteCandidates": unreadable_sqlite,
        },
        "reason": (
            "enough_readable_history"
            if readable_messages >= min_readable_messages
            else "readable_history_below_goal_scope"
        ),
    }


def _audit_behavior_profile(
    profile_path: Path,
    style_report_path: Path,
) -> dict[str, Any]:
    if not profile_path.exists():
        return {
            "status": "missing",
            "evidence": {"profile": str(profile_path), "exists": False},
            "reason": "profile_missing",
        }
    profile = _load_json(profile_path)
    behavior = profile.get("behaviorProfile")
    behavior_keys = set(behavior) if isinstance(behavior, dict) else set()
    missing_behavior = sorted(REQUIRED_BEHAVIOR_KEYS - behavior_keys)
    empty_behavior = sorted(
        key for key in REQUIRED_BEHAVIOR_KEYS
        if isinstance(behavior, dict) and not behavior.get(key)
    )
    style_report: dict[str, Any] = {}
    if style_report_path.exists():
        style_report = _load_json(style_report_path)
    behavior_stats = (
        style_report.get("behaviorStats")
        if isinstance(style_report.get("behaviorStats"), dict)
        else {}
    )
    source_coverage = (
        style_report.get("sourceCoverage")
        if isinstance(style_report.get("sourceCoverage"), dict)
        else {}
    )
    stats_have_timing = (
        int(behavior_stats.get("sourceRecordsWithTime") or 0) > 0
        and int((behavior_stats.get("replyLatencySeconds") or {}).get("count") or 0) > 0
    )
    readable_history_messages = int(
        source_coverage.get("readableHistoryMessages")
        or source_coverage.get("manifestTotalMessages")
        or 0
    )
    status = "pass" if (
        not missing_behavior
        and not empty_behavior
        and readable_history_messages > 0
        and stats_have_timing
    ) else "fail"
    return {
        "status": status,
        "evidence": {
            "profile": str(profile_path),
            "styleReport": str(style_report_path),
            "behaviorKeys": sorted(behavior_keys),
            "missingBehaviorKeys": missing_behavior,
            "emptyBehaviorKeys": empty_behavior,
            "manifestTotalMessages": int(source_coverage.get("manifestTotalMessages") or 0),
            "readableRecords": int(source_coverage.get("readableRecords") or 0),
            "readableHistoryMessages": readable_history_messages,
            "sourceRecordsWithTime": int(behavior_stats.get("sourceRecordsWithTime") or 0),
            "replyLatencySeconds": behavior_stats.get("replyLatencySeconds") or {},
            "threadBursts": int(behavior_stats.get("threadBursts") or 0),
            "continuationReplies": int(behavior_stats.get("continuationReplies") or 0),
        },
        "reason": "behavior_profile_has_required_stats" if status == "pass" else "behavior_profile_incomplete",
    }


def _audit_human_actions(
    db_path: Path,
    *,
    hours: float,
    limit: int,
) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "status": "missing",
            "evidence": {"db": str(db_path), "exists": False},
            "reason": "runtime_db_missing",
        }
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            acceptance = check_acceptance(conn, since=None, hours=hours, limit=limit)
    except sqlite3.Error as exc:
        return {
            "status": "fail",
            "evidence": {
                "db": str(db_path),
                "exists": True,
                "sqliteError": str(exc),
            },
            "reason": "runtime_db_unreadable_or_missing_tables",
        }
    return {
        "status": "pass" if acceptance.get("status") == "pass" else "fail",
        "evidence": {
            "db": str(db_path),
            "acceptanceStatus": acceptance.get("status"),
            "checks": acceptance.get("checks"),
            "battleAudits": acceptance.get("battleAudits", [])[:3],
            "pokeEvents": acceptance.get("pokeEvents", [])[:3],
            "fakeActionRows": acceptance.get("fakeActionRows", [])[:3],
        },
        "reason": (
            "human_actions_runtime_verified"
            if acceptance.get("status") == "pass"
            else "human_actions_runtime_not_fully_verified"
        ),
    }


def _next_steps(requirements: dict[str, dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    if requirements["readableFullHistory"]["status"] != "pass":
        steps.append(
            "导出或提供可读的 QQ Chat Exporter chunked-jsonl/JSONL/CSV 完整历史，使 readableExportMessages 达到目标阈值后重建画像。"
        )
    if requirements["behaviorProfileBuilt"]["status"] != "pass":
        steps.append(
            "重新运行 tools/build_style_profile.py --input-root ... --report-output ... 并通过 inspect_style_profile.py。"
        )
    if requirements["humanActionsRuntimeVerified"]["status"] != "pass":
        steps.append(
            "在 WSL 真实库上运行 tools/check_human_actions_acceptance.py，并补齐斗图、戳一戳、普通提及沉默和假媒体文本检查。"
        )
    return steps


def _export_summary(report: dict[str, Any]) -> list[dict[str, Any]]:
    exports = report.get("exports")
    if not isinstance(exports, list):
        return []
    summary = []
    for export in exports:
        summary.append(
            {
                "kind": export.get("kind"),
                "path": export.get("path") or export.get("paths"),
                "historyFiles": export.get("historyFiles"),
                "jsonlFiles": export.get("jsonlFiles"),
                "manifestTotalMessages": export.get("manifestTotalMessages"),
                "readableRecords": export.get("readableRecords"),
                "readableHistoryMessages": export.get("readableHistoryMessages"),
            }
        )
    return summary


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    raise SystemExit(main())
