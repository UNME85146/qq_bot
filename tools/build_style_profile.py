from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.safety.safety_service import SafetyService
from app.persona.history_character import (
    HistoryCharacterMetrics,
    build_behavior_profile,
    build_character_summary,
    build_reply_rules,
    build_style_summary,
    build_tone_rules,
)


ATTACHMENT_ONLY_PATTERN = re.compile(
    r"^(?:\[(?:图片|表情\d*|视频|转发消息|语音|文件|动画表情|JSON消息)\]\s*)+$"
)
CQ_ONLY_PATTERN = re.compile(r"^(?:\[CQ:(?:image|face|record|video|file)[^\]]*\]\s*)+$")
LONG_NUMBER_PATTERN = re.compile(r"\d{7,}")
URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
MEDIA_MARKER_PATTERN = re.compile(r"\[(?:图片|表情\d*|视频|语音|动画表情|文件)\]")
QUESTION_PATTERN = re.compile(r"[?？]|(吗|呢|咋|怎么|为什么|为啥|啥|哪|谁)$")
PUNCTUATION_PATTERN = re.compile(r"[，。！？、,.!?；;：:]")
STICKER_INTENT_PATTERN = re.compile(r"(表情包|斗图|接一下|接个图|回个表情|回张图|来个表情|发个表情)")
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
SUPPORTED_HISTORY_SUFFIXES = {".jsonl", ".json", ".csv", ".txt", ".html", ".htm"}
MEDIA_ACTION_TYPES = {
    "image",
    "face",
    "sticker",
    "mface",
    "marketface",
    "video",
    "record",
    "file",
    "json",
    "forward",
}
GENERIC_RECORD_CONTAINER_KEYS = (
    "messages",
    "records",
    "items",
    "rows",
    "data",
    "messageList",
    "msgList",
)
SENDER_ID_KEYS = (
    "uin",
    "uid",
    "user_id",
    "userId",
    "sender_id",
    "senderId",
    "sender_uin",
    "senderUin",
    "from_uin",
    "fromUin",
    "qq",
    "qq_id",
    "qqId",
    "account",
    "account_id",
    "账号",
    "QQ",
    "QQ号",
    "发送人QQ",
    "发送者QQ",
    "发送者账号",
)
TEXT_KEYS = (
    "message_text",
    "messageText",
    "text",
    "message",
    "raw_message",
    "rawMessage",
    "msg",
    "body",
    "content",
    "message_content",
    "messageContent",
    "消息",
    "消息内容",
    "内容",
    "正文",
)
TIME_KEYS = (
    "time",
    "timestamp",
    "created_at",
    "createdAt",
    "datetime",
    "date",
    "send_time",
    "sendTime",
    "msg_time",
    "msgTime",
    "消息时间",
    "发送时间",
    "时间",
)
PLAIN_MESSAGE_HEADER_PATTERN = re.compile(
    r"^(?P<time>(?:\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?|\d{1,2}[-/.月]\d{1,2}(?:日)?)[ T日周星期]*"
    r"\d{1,2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<sender>.+?)\s*$"
)
HTML_BREAK_PATTERN = re.compile(r"(?i)<\s*br\s*/?\s*>")
HTML_BLOCK_END_PATTERN = re.compile(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a bot-owned conversation character from readable QQ history exports."
    )
    parser.add_argument(
        "--input-dir",
        action="append",
        default=[],
        help=(
            "Directory containing QQ Chat Exporter chunks or generic JSONL/JSON/CSV history files. "
            "Can be repeated. If it has a chunks/ child, that child is used for QQ Chat Exporter files."
        ),
    )
    parser.add_argument(
        "--input-root",
        help="Root directory containing one or more QQ Chat Exporter or generic readable exports.",
    )
    parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        help="Explicit generic .jsonl, .json, or .csv history file. Can be repeated.",
    )
    parser.add_argument(
        "--runtime-db",
        action="append",
        default=[],
        help="Readonly bot runtime SQLite DB containing group_message_index stream rows. Can be repeated.",
    )
    parser.add_argument("--source-user-id", required=True, help="QQ user id to extract style from.")
    parser.add_argument("--output", required=True, help="Output persona_profile.local.json path.")
    parser.add_argument("--report-output", help="Optional JSON report path for source coverage and behavior stats.")
    parser.add_argument("--days", type=int, help="Only include records from the most recent number of days.")
    args = parser.parse_args()

    result = build_style_profile(
        input_dirs=[Path(path) for path in args.input_dir],
        input_root=Path(args.input_root) if args.input_root else None,
        input_files=[Path(path) for path in args.input_file],
        runtime_dbs=[Path(path) for path in args.runtime_db],
        source_user_id=str(args.source_user_id),
        output_path=Path(args.output),
        report_output_path=Path(args.report_output) if args.report_output else None,
        days=args.days,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_style_profile(
    *,
    input_dir: Path | None = None,
    input_dirs: list[Path] | None = None,
    input_root: Path | None = None,
    input_files: list[Path] | None = None,
    runtime_dbs: list[Path] | None = None,
    source_user_id: str,
    output_path: Path,
    report_output_path: Path | None = None,
    days: int | None = None,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    if days is not None and days <= 0:
        raise ValueError("days must be positive")
    current_time = reference_time or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("reference_time must be timezone-aware")
    current_time = current_time.astimezone(UTC)
    cutoff_at = current_time - timedelta(days=days) if days is not None else None
    safety_service = SafetyService(source_user_id=source_user_id)
    resolved_inputs = list(input_dirs or [])
    resolved_files = list(input_files or [])
    resolved_runtime_dbs = list(runtime_dbs or [])
    if input_dir is not None:
        resolved_inputs.append(input_dir)
    files, export_sources = _discover_history_files(
        input_dirs=resolved_inputs,
        input_root=input_root,
        input_files=resolved_files,
    )
    stats: dict[str, Any] = {
        "inputDirs": [str(path) for path in resolved_inputs],
        "inputRoot": str(input_root) if input_root else None,
        "inputFiles": [str(path) for path in resolved_files],
        "runtimeDbs": [str(path) for path in resolved_runtime_dbs],
        "sourceUserId": source_user_id,
        "exports": len(export_sources),
        "files": len(files),
        "totalRecords": 0,
        "targetRecords": 0,
        "nonEmptyTargetTexts": 0,
        "validLowSensitiveTexts": 0,
        "skippedAttachments": 0,
        "skippedSensitive": 0,
        "skippedSystemOrRecalled": 0,
        "output": str(output_path),
        "reportOutput": str(report_output_path) if report_output_path else None,
        "lookbackDays": days,
        "windowStart": cutoff_at.isoformat() if cutoff_at is not None else None,
    }
    valid_texts: list[str] = []
    behavior_stats: dict[str, Any] = {
        "validLengths": [],
        "questionTexts": 0,
        "punctuationMarks": Counter(),
        "textsWithPunctuation": 0,
        "atMentions": 0,
        "replyMarkers": 0,
        "mediaRecords": 0,
        "stickerRecords": 0,
        "mediaTypes": Counter(),
        "runtimeMediaTypes": Counter(),
        "runtimeGroups": Counter(),
        "stickerIntentTexts": Counter(),
        "shortTexts": Counter(),
        "activeHours": Counter(),
        "activeWeekdays": Counter(),
        "replyLatencies": [],
        "threadBursts": 0,
        "continuationReplies": 0,
        "sourceRecordsWithTime": 0,
    }

    if resolved_runtime_dbs:
        export_sources.extend(_read_runtime_db_sources(resolved_runtime_dbs))
    stats["exports"] = len(export_sources)

    if not files and not resolved_runtime_dbs:
        raise FileNotFoundError(
            "No readable history files found. Provide --input-dir, --input-root, --input-file, or --runtime-db with "
            "QQ Chat Exporter chunked-jsonl, generic JSONL, JSON, CSV, TXT, HTML exports, or bot runtime DB streams."
        )

    for file_path in files:
        _consume_records(
            _iter_history_records(file_path),
            source_user_id=source_user_id,
            safety_service=safety_service,
            stats=stats,
            behavior_stats=behavior_stats,
            valid_texts=valid_texts,
            cutoff_at=cutoff_at,
        )

    for runtime_db in resolved_runtime_dbs:
        _consume_records(
            _iter_runtime_db_records(runtime_db),
            source_user_id=source_user_id,
            safety_service=safety_service,
            stats=stats,
            behavior_stats=behavior_stats,
            valid_texts=valid_texts,
            cutoff_at=cutoff_at,
        )

    stats["validLowSensitiveTexts"] = len(valid_texts)
    stats["sourceCoverage"] = _make_source_coverage(export_sources)
    stats["behaviorStats"] = _make_behavior_stats_summary(behavior_stats)
    if not valid_texts:
        raise ValueError(
            "No valid low-sensitive text found for the requested source user; "
            "the existing profile was not changed."
        )
    metrics = _make_character_metrics(behavior_stats)
    profile = _make_profile(
        source_user_id=source_user_id,
        metrics=metrics,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report_output_path is not None:
        report_output_path.parent.mkdir(parents=True, exist_ok=True)
        report_output_path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return stats


def _make_profile(
    *,
    source_user_id: str,
    metrics: HistoryCharacterMetrics,
) -> dict[str, Any]:
    return {
        "sourceUserId": source_user_id,
        "identityDisclosure": "我是小黄，一个从过往聊天习惯中形成自己说话方式的 QQ 聊天机器人。",
        "metrics": metrics.to_payload(),
        "characterSummary": build_character_summary(metrics),
        "styleSummary": build_style_summary(metrics),
        "toneRules": build_tone_rules(metrics),
        "topicBiases": [],
        "lexicon": [],
        "replyRules": build_reply_rules(metrics),
        "avoidRules": [
            "不要冒充历史记录中的任何人",
            "不要编造真实学校、公司、住址、手机号、财务和身份信息",
            "不要复述完整聊天记录",
            "不要过度攻击或辱骂",
            "不要客服腔",
        ],
        "fewShotExamples": [],
        "behaviorProfile": build_behavior_profile(metrics),
        "updatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _make_character_metrics(stats: dict[str, Any]) -> HistoryCharacterMetrics:
    lengths: list[int] = stats["validLengths"]
    media_records = int(stats["mediaRecords"])
    metrics = HistoryCharacterMetrics(
        valid_text_count=len(lengths),
        average_text_length=round(sum(lengths) / len(lengths), 1) if lengths else 0,
        short_text_ratio=_ratio(sum(1 for length in lengths if length <= 12), len(lengths)),
        medium_text_ratio=_ratio(
            sum(1 for length in lengths if 13 <= length <= 40),
            len(lengths),
        ),
        question_ratio=_ratio(stats["questionTexts"], len(lengths)),
        punctuation_ratio=_ratio(stats["textsWithPunctuation"], len(lengths)),
        media_ratio=_ratio(media_records, media_records + len(lengths)),
        sticker_ratio=_ratio(int(stats["stickerRecords"]), media_records or 1),
        continuation_replies=int(stats["continuationReplies"]),
        thread_bursts=int(stats["threadBursts"]),
        at_mentions=int(stats["atMentions"]),
        reply_markers=int(stats["replyMarkers"]),
        sticker_intent_count=sum(
            count
            for text, count in stats["stickerIntentTexts"].items()
            if _is_sticker_intent_phrase(text)
        ),
        repeated_short_expression_count=sum(
            1 for _text, count in stats["shortTexts"].items() if count >= 2
        ),
    )
    metrics.validate()
    return metrics


def _consume_records(
    records,
    *,
    source_user_id: str,
    safety_service: SafetyService,
    stats: dict[str, Any],
    behavior_stats: dict[str, Any],
    valid_texts: list[str],
    cutoff_at: datetime | None = None,
) -> None:
    previous_record: dict[str, Any] | None = None
    for record in records:
        if cutoff_at is not None:
            record_at = _record_datetime(record)
            if record_at is None or record_at < cutoff_at:
                previous_record = None
                continue
        stats["totalRecords"] += 1

        if str(_sender_uin(record)) != source_user_id:
            previous_record = record
            continue
        stats["targetRecords"] += 1

        if _is_system_or_recalled(record):
            stats["skippedSystemOrRecalled"] += 1
            previous_record = record
            continue

        raw_text = _normalize_text(_extract_text(record))
        _observe_behavior_record(record, raw_text, behavior_stats, previous_record)
        text = raw_text
        if not text:
            previous_record = record
            continue
        stats["nonEmptyTargetTexts"] += 1

        if _is_pure_attachment(text):
            stats["skippedAttachments"] += 1
            previous_record = record
            continue
        if not _is_low_sensitive_style_text(text, safety_service):
            stats["skippedSensitive"] += 1
            previous_record = record
            continue
        valid_texts.append(text)
        _observe_valid_style_text(text, behavior_stats)
        previous_record = record


def _discover_jsonl_files(
    *,
    input_dirs: list[Path],
    input_root: Path | None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    return _discover_history_files(
        input_dirs=input_dirs,
        input_root=input_root,
        input_files=[],
        suffixes={".jsonl"},
    )


def _discover_history_files(
    *,
    input_dirs: list[Path],
    input_root: Path | None,
    input_files: list[Path] | None = None,
    suffixes: set[str] | None = None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    allowed_suffixes = suffixes or SUPPORTED_HISTORY_SUFFIXES
    chunk_dirs: list[Path] = []
    generic_roots: list[Path] = []
    for input_dir in input_dirs:
        resolved_dir = _resolve_chunk_dir(input_dir)
        chunk_dirs.append(resolved_dir)
        generic_roots.append(resolved_dir)

    if input_root is not None:
        if not input_root.exists():
            raise FileNotFoundError(f"Input root does not exist: {input_root}")
        generic_roots.append(input_root)
        for manifest_path in sorted(input_root.rglob("manifest.json")):
            export_dir = manifest_path.parent
            if export_dir == input_root or input_root in export_dir.parents:
                chunk_dirs.append(_resolve_chunk_dir(export_dir))

    unique_chunk_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    for chunk_dir in chunk_dirs:
        key = chunk_dir.resolve()
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        unique_chunk_dirs.append(chunk_dir)

    unique_generic_roots: list[Path] = []
    seen_roots: set[Path] = set()
    for root in generic_roots:
        if not root.exists():
            raise FileNotFoundError(f"Input directory does not exist: {root}")
        key = root.resolve()
        if key in seen_roots:
            continue
        seen_roots.add(key)
        unique_generic_roots.append(root)

    files: list[Path] = []
    sources: list[dict[str, Any]] = []
    seen_files: set[Path] = set()
    for chunk_dir in unique_chunk_dirs:
        if not chunk_dir.exists():
            raise FileNotFoundError(f"Input directory does not exist: {chunk_dir}")
        jsonl_files = [
            file_path
            for file_path in sorted(chunk_dir.glob("*.jsonl"))
            if file_path.suffix.lower() in allowed_suffixes
        ]
        if not jsonl_files:
            continue
        files.extend(_append_unique_files(jsonl_files, seen_files))
        sources.append(_read_export_source(chunk_dir, jsonl_files))
    for root in unique_generic_roots:
        generic_files = _find_generic_history_files(root, allowed_suffixes)
        generic_files = [
            file_path
            for file_path in generic_files
            if not _is_qq_exporter_chunk_file(file_path)
        ]
        appended = _append_unique_files(generic_files, seen_files)
        if not appended:
            continue
        sources.append(_read_generic_source(root, appended))
        files.extend(appended)
    for file_path in input_files or []:
        if not file_path.exists():
            raise FileNotFoundError(f"Input file does not exist: {file_path}")
        if file_path.suffix.lower() not in allowed_suffixes:
            raise ValueError(f"Unsupported history file type: {file_path}")
        appended = _append_unique_files([file_path], seen_files)
        if not appended:
            continue
        sources.append(_read_generic_source(file_path.parent, appended, explicit_files=True))
        files.extend(appended)
    return files, sources


def _append_unique_files(files: list[Path], seen_files: set[Path]) -> list[Path]:
    appended: list[Path] = []
    for file_path in files:
        key = file_path.resolve()
        if key in seen_files:
            continue
        seen_files.add(key)
        appended.append(file_path)
    return appended


def _find_generic_history_files(root: Path, suffixes: set[str]) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in suffixes else []
    return [
        file_path
        for file_path in sorted(root.rglob("*"))
        if file_path.is_file()
        and file_path.suffix.lower() in suffixes
        and file_path.name.lower() != "manifest.json"
    ]


def _is_qq_exporter_chunk_file(file_path: Path) -> bool:
    return (
        file_path.suffix.lower() == ".jsonl"
        and file_path.parent.name == "chunks"
        and (file_path.parent.parent / "manifest.json").exists()
    )


def _resolve_chunk_dir(path: Path) -> Path:
    if path.name == "chunks":
        return path
    chunks = path / "chunks"
    if chunks.exists():
        return chunks
    return path


def _read_export_source(chunk_dir: Path, files: list[Path]) -> dict[str, Any]:
    export_dir = chunk_dir.parent if chunk_dir.name == "chunks" else chunk_dir
    manifest_path = export_dir / "manifest.json"
    source: dict[str, Any] = {
        "kind": "qq_chat_exporter",
        "exportDir": str(export_dir),
        "chunkDir": str(chunk_dir),
        "files": len(files),
        "manifest": None,
    }
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {"error": "invalid_json"}
        source["manifest"] = _summarize_manifest(manifest)
    if not isinstance(source.get("manifest"), dict) or not isinstance(
        source["manifest"].get("totalMessages"),
        int,
    ):
        source["readableRecords"] = sum(1 for file_path in files for _record in _iter_history_records(file_path))
    return source


def _read_generic_source(
    root: Path,
    files: list[Path],
    *,
    explicit_files: bool = False,
) -> dict[str, Any]:
    readable_records = 0
    errors: list[dict[str, Any]] = []
    file_summaries: list[dict[str, Any]] = []
    for file_path in files:
        file_summary = {
            "path": str(file_path),
            "suffix": file_path.suffix.lower(),
            "readableRecords": 0,
        }
        try:
            count = sum(1 for _record in _iter_history_records(file_path))
        except (ValueError, OSError, UnicodeError) as exc:
            errors.append({"path": str(file_path), "error": str(exc)})
        else:
            readable_records += count
            file_summary["readableRecords"] = count
        file_summaries.append(file_summary)
    return {
        "kind": "generic_files" if explicit_files else "generic_dir",
        "root": str(root),
        "files": len(files),
        "readableRecords": readable_records,
        "errors": errors,
        "fileSummaries": file_summaries[:20],
    }


def _read_runtime_db_sources(paths: list[Path]) -> list[dict[str, Any]]:
    return [_read_runtime_db_source(path) for path in paths]


def _read_runtime_db_source(path: Path) -> dict[str, Any]:
    source: dict[str, Any] = {
        "kind": "runtime_db",
        "path": str(path),
        "exists": path.exists(),
        "readableRecords": 0,
        "table": "group_message_index",
    }
    if not path.exists():
        source["error"] = "db_not_found"
        return source
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT COUNT(*) AS count FROM group_message_index").fetchone()
            time_row = conn.execute(
                "SELECT MIN(created_at) AS start, MAX(created_at) AS end FROM group_message_index"
            ).fetchone()
            group_rows = conn.execute(
                """
                SELECT group_id, COUNT(*) AS count
                FROM group_message_index
                GROUP BY group_id
                ORDER BY count DESC
                LIMIT 10
                """
            ).fetchall()
        source["readableRecords"] = int(row["count"] if row is not None else 0)
        source["timeRange"] = dict(time_row) if time_row is not None else {}
        source["groups"] = [dict(group_row) for group_row in group_rows]
    except sqlite3.Error as exc:
        source["error"] = str(exc)
    return source


def _summarize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    chat_info = manifest.get("chatInfo") if isinstance(manifest.get("chatInfo"), dict) else {}
    statistics = manifest.get("statistics") if isinstance(manifest.get("statistics"), dict) else {}
    time_range = statistics.get("timeRange") if isinstance(statistics.get("timeRange"), dict) else {}
    return {
        "format": metadata.get("format"),
        "exportTime": metadata.get("exportTime"),
        "chatName": chat_info.get("name"),
        "chatType": chat_info.get("type"),
        "totalMessages": statistics.get("totalMessages"),
        "timeRange": {
            "start": time_range.get("start"),
            "end": time_range.get("end"),
            "durationDays": time_range.get("durationDays"),
        },
    }


def _make_source_coverage(export_sources: list[dict[str, Any]]) -> dict[str, Any]:
    manifest_totals = [
        source.get("manifest", {}).get("totalMessages")
        for source in export_sources
        if isinstance(source.get("manifest"), dict)
    ]
    readable_totals = [value for value in manifest_totals if isinstance(value, int)]
    source_readable_records = [
        source.get("readableRecords")
        for source in export_sources
        if isinstance(source.get("readableRecords"), int)
    ]
    readable_records = sum(source_readable_records)
    return {
        "exports": export_sources,
        "manifestTotalMessages": sum(readable_totals),
        "readableRecords": readable_records,
        "readableHistoryMessages": sum(readable_totals) + readable_records,
        "hasManifestTotals": bool(readable_totals),
    }


def _iter_history_records(file_path: Path):
    suffix = file_path.suffix.lower()
    if suffix == ".jsonl":
        yield from _iter_jsonl_records(file_path)
        return
    if suffix == ".json":
        yield from _iter_json_records(file_path)
        return
    if suffix == ".csv":
        yield from _iter_csv_records(file_path)
        return
    if suffix == ".txt":
        yield from _iter_plain_text_records(file_path)
        return
    if suffix in {".html", ".htm"}:
        yield from _iter_html_records(file_path)
        return
    raise ValueError(f"Unsupported history file type: {file_path}")


def _iter_jsonl_records(file_path: Path):
    with file_path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {file_path}:{line_number}") from exc
            if isinstance(value, list):
                for item in value:
                    record = _coerce_history_record(item)
                    if record is not None:
                        yield record
            else:
                record = _coerce_history_record(value)
                if record is not None:
                    yield record


def _iter_json_records(file_path: Path):
    try:
        value = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON at {file_path}") from exc
    yield from _iter_json_value_records(value)


def _iter_json_value_records(value: Any):
    if isinstance(value, list):
        for item in value:
            record = _coerce_history_record(item)
            if record is not None:
                yield record
        return
    if isinstance(value, dict):
        container = _find_record_container(value)
        if container is not None:
            yield from _iter_json_value_records(container)
            return
        record = _coerce_history_record(value)
        if record is not None:
            yield record


def _find_record_container(value: dict[str, Any]) -> Any | None:
    for key in GENERIC_RECORD_CONTAINER_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict):
            nested = _find_record_container(candidate)
            if nested is not None:
                return nested
    return None


def _iter_csv_records(file_path: Path):
    with file_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            return
        for row_number, row in enumerate(reader, start=2):
            record = _coerce_history_record(row)
            if record is None:
                continue
            record.setdefault("id", str(row_number))
            yield record


def _iter_plain_text_records(file_path: Path):
    yield from _iter_plain_lines_records(_read_history_text(file_path).splitlines())


def _iter_html_records(file_path: Path):
    text = _html_to_plain_text(_read_history_text(file_path))
    yield from _iter_plain_lines_records(text.splitlines())


def _iter_runtime_db_records(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Runtime DB does not exist: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                """
                SELECT group_id, message_id, user_id, user_name, text, media_type,
                       sticker_asset_id, is_bot, created_at
                FROM group_message_index
                ORDER BY datetime(created_at), rowid
                """
            )
            for row in rows:
                yield _runtime_row_to_record(dict(row))
    except sqlite3.Error as exc:
        raise ValueError(f"Runtime DB is not readable as group_message_index: {path}") from exc


def _runtime_row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    media_type = str(row.get("media_type") or "").lower()
    text = str(row.get("text") or "").strip()
    if not text and media_type:
        text = _media_type_placeholder(media_type)
    elements: list[dict[str, Any]] = []
    if media_type:
        elements.append({"type": _runtime_media_element_type(media_type), "data": {"id": row.get("sticker_asset_id")}})
    if text:
        elements.append({"type": "text", "data": {"text": text}})
    return {
        "id": row.get("message_id"),
        "type": "message",
        "sender": {
            "uin": str(row.get("user_id") or ""),
            "nickname": row.get("user_name") or "",
        },
        "content": {
            "text": text,
            "elements": elements,
        },
        "timestamp": None,
        "time": row.get("created_at"),
        "group_id": row.get("group_id"),
        "is_bot": bool(row.get("is_bot")),
        "media_type": media_type,
        "runtime_source": "group_message_index",
    }


def _media_type_placeholder(media_type: str) -> str:
    if media_type in {"face", "sticker", "mface", "marketface"}:
        return "[表情]"
    if media_type == "image":
        return "[图片]"
    if media_type == "video":
        return "[视频]"
    if media_type == "record":
        return "[语音]"
    if media_type == "file":
        return "[文件]"
    return f"[{media_type}]"


def _runtime_media_element_type(media_type: str) -> str:
    if media_type in {"face", "sticker", "mface", "marketface"}:
        return "face"
    if media_type == "record":
        return "record"
    return media_type or "text"


def _iter_plain_lines_records(lines: list[str]):
    current_header: re.Match[str] | None = None
    current_body: list[str] = []
    record_index = 0

    for line in lines:
        stripped = line.strip()
        header_match = PLAIN_MESSAGE_HEADER_PATTERN.match(stripped)
        if header_match is not None:
            if current_header is not None:
                record = _make_plain_record(current_header, current_body, record_index)
                if record is not None:
                    yield record
                record_index += 1
            current_header = header_match
            current_body = []
            continue
        if current_header is None:
            continue
        current_body.append(line.rstrip())

    if current_header is not None:
        record = _make_plain_record(current_header, current_body, record_index)
        if record is not None:
            yield record


def _make_plain_record(
    header_match: re.Match[str],
    body_lines: list[str],
    record_index: int,
) -> dict[str, Any] | None:
    sender_text = header_match.group("sender").strip()
    text = _normalize_text("\n".join(line for line in body_lines if line.strip()))
    if not sender_text and not text:
        return None
    sender_uin = _extract_sender_identifier(sender_text)
    record: dict[str, Any] = {
        "id": str(record_index + 1),
        "sender": {"uin": sender_uin, "name": sender_text},
        "content": {"text": text},
        "time": _normalize_plain_time(header_match.group("time")),
    }
    return _coerce_history_record(record)


def _extract_sender_identifier(sender_text: str) -> str:
    for pattern in (
        r"\((\d{5,12})\)",
        r"（(\d{5,12})）",
        r"<(\d{5,12})>",
        r"《(\d{5,12})》",
        r"\[(\d{5,12})\]",
        r"【(\d{5,12})】",
        r"\b(\d{5,12})\b",
    ):
        match = re.search(pattern, sender_text)
        if match:
            return match.group(1)
    return sender_text.strip()


def _normalize_plain_time(raw_time: str) -> str:
    text = raw_time.strip()
    text = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .replace(".", "-")
    )
    text = re.sub(r"\s+", " ", text)
    return text


def _html_to_plain_text(text: str) -> str:
    text = HTML_BREAK_PATTERN.sub("\n", text)
    text = HTML_BLOCK_END_PATTERN.sub("\n", text)
    text = HTML_TAG_PATTERN.sub("", text)
    return html.unescape(text)


def _read_history_text(file_path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def _coerce_history_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    record = dict(value)
    sender_uin = _sender_uin(record)
    if not sender_uin:
        sender_uin = _first_non_empty(record, SENDER_ID_KEYS)
        if not sender_uin:
            sender_value = record.get("sender")
            if isinstance(sender_value, (int, float)):
                sender_uin = str(sender_value)
            elif isinstance(sender_value, str) and sender_value.strip():
                sender_uin = sender_value.strip()
        sender = record.get("sender") if isinstance(record.get("sender"), dict) else {}
        if sender_uin:
            record["sender"] = {**sender, "uin": sender_uin}
    if not _extract_text(record):
        text = _first_non_empty(record, TEXT_KEYS)
        if text:
            record["content"] = {"text": text}
    if _record_datetime(record) is None:
        time_value = _first_non_empty(record, TIME_KEYS)
        if time_value:
            record["time"] = time_value
    if not _is_message_like_record(record):
        return None
    return record


def _is_message_like_record(record: dict[str, Any]) -> bool:
    if _sender_uin(record) or _extract_text(record):
        return True
    if _record_datetime(record) is None:
        return False
    return any(
        key in record
        for key in (
            "id",
            "message_id",
            "messageId",
            "msg_id",
            "msgId",
            "type",
            "system",
            "recalled",
        )
    )


def _first_non_empty(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_short_style_phrase(text: str) -> bool:
    if not 1 <= len(text) <= 18:
        return False
    if text.startswith("[") and text.endswith("]"):
        return False
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return False
    if URL_PATTERN.search(text) or LONG_NUMBER_PATTERN.search(text):
        return False
    if any(marker in text for marker in ("@", "http", "身份证", "手机号", "住址", "银行卡")):
        return False
    return True


def _is_sticker_intent_phrase(text: str) -> bool:
    if not 1 <= len(text) <= 12:
        return False
    if not STICKER_INTENT_PATTERN.search(text):
        return False
    if any(marker in text for marker in ("，", "。", "；", ";", "所以", "因为", "现在只是", "可以没有")):
        return False
    return _is_short_style_phrase(text)


def _observe_behavior_record(
    record: dict[str, Any],
    text: str,
    stats: dict[str, Any],
    previous_record: dict[str, Any] | None = None,
) -> None:
    rendered = json.dumps(record, ensure_ascii=False)
    media_type = _record_media_type(record)
    if media_type in MEDIA_ACTION_TYPES:
        stats["mediaTypes"][media_type] += 1
        if record.get("runtime_source") == "group_message_index":
            stats["runtimeMediaTypes"][media_type] += 1
    group_id = record.get("group_id")
    if record.get("runtime_source") == "group_message_index" and group_id:
        stats["runtimeGroups"][str(group_id)] += 1
    if STICKER_INTENT_PATTERN.search(text):
        stats["stickerIntentTexts"][text] += 1
    if _has_media_marker(text, rendered):
        stats["mediaRecords"] += 1
    if _has_sticker_marker(text, rendered):
        stats["stickerRecords"] += 1
    if _has_at_marker(text, rendered):
        stats["atMentions"] += 1
    if _has_reply_marker(text, rendered):
        stats["replyMarkers"] += 1
    created_at = _record_datetime(record)
    if created_at is not None:
        stats["sourceRecordsWithTime"] += 1
        stats["activeHours"][created_at.hour] += 1
        stats["activeWeekdays"][created_at.weekday()] += 1
        if previous_record is not None:
            previous_at = _record_datetime(previous_record)
            if previous_at is not None:
                delta_seconds = (created_at - previous_at).total_seconds()
                if 0 <= delta_seconds <= 15 * 60:
                    if _sender_uin(previous_record) == _sender_uin(record):
                        stats["threadBursts"] += 1
                    else:
                        stats["continuationReplies"] += 1
                    stats["replyLatencies"].append(int(delta_seconds))


def _observe_valid_style_text(text: str, stats: dict[str, Any]) -> None:
    stats["validLengths"].append(len(text))
    if QUESTION_PATTERN.search(text):
        stats["questionTexts"] += 1
    punctuation = PUNCTUATION_PATTERN.findall(text)
    if punctuation:
        stats["textsWithPunctuation"] += 1
        stats["punctuationMarks"].update(punctuation)
    if _is_short_style_phrase(text):
        stats["shortTexts"][text] += 1


def _has_media_marker(text: str, rendered: str) -> bool:
    haystack = f"{text}\n{rendered}"
    return bool(
        MEDIA_MARKER_PATTERN.search(haystack)
        or "[CQ:image" in haystack
        or '"type": "image"' in haystack
        or '"type":"image"' in haystack
        or '"type": "face"' in haystack
        or '"type":"face"' in haystack
    )


def _record_media_type(record: dict[str, Any]) -> str:
    raw_media_type = record.get("media_type")
    if isinstance(raw_media_type, str) and raw_media_type.strip():
        return raw_media_type.strip().lower()
    content = record.get("content")
    if not isinstance(content, dict):
        return ""
    for element in content.get("elements", []):
        if not isinstance(element, dict):
            continue
        raw_type = element.get("type")
        if not isinstance(raw_type, str):
            continue
        normalized_type = raw_type.strip().lower()
        if normalized_type in MEDIA_ACTION_TYPES:
            return normalized_type
    return ""


def _has_sticker_marker(text: str, rendered: str) -> bool:
    haystack = f"{text}\n{rendered}".lower()
    return any(
        marker in haystack
        for marker in (
            "表情",
            "动画表情",
            "sticker",
            "marketface",
            "mface",
            '"type": "face"',
            '"type":"face"',
        )
    )


def _has_at_marker(text: str, rendered: str) -> bool:
    haystack = f"{text}\n{rendered}"
    return (
        "[CQ:at," in haystack
        or "[at:qq=" in haystack
        or '"type": "at"' in haystack
        or '"type":"at"' in haystack
        or "@" in text
    )


def _has_reply_marker(text: str, rendered: str) -> bool:
    haystack = f"{text}\n{rendered}".lower()
    return (
        "[cq:reply," in haystack
        or "[reply:" in haystack
        or '"type": "reply"' in haystack
        or '"type":"reply"' in haystack
        or "reply" in haystack
        or "引用" in haystack
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _make_behavior_stats_summary(stats: dict[str, Any]) -> dict[str, Any]:
    latencies = stats["replyLatencies"]
    return {
        "sourceRecordsWithTime": int(stats["sourceRecordsWithTime"]),
        "topActiveHours": _counter_summary(stats["activeHours"], limit=8),
        "topActiveWeekdays": [
            {"weekday": WEEKDAY_NAMES[int(day)], "count": count}
            for day, count in stats["activeWeekdays"].most_common(7)
            if 0 <= int(day) < len(WEEKDAY_NAMES)
        ],
        "replyLatencySeconds": {
            "count": len(latencies),
            "median": _percentile(latencies, 0.5),
            "p75": _percentile(latencies, 0.75),
        },
        "threadBursts": int(stats["threadBursts"]),
        "continuationReplies": int(stats["continuationReplies"]),
        "topShortTexts": stats["shortTexts"].most_common(12),
        "mediaTypes": stats["mediaTypes"].most_common(12),
        "runtimeMediaTypes": stats["runtimeMediaTypes"].most_common(12),
        "runtimeGroups": stats["runtimeGroups"].most_common(8),
        "stickerIntentTexts": stats["stickerIntentTexts"].most_common(12),
    }


def _counter_summary(counter: Counter[int], *, limit: int) -> list[dict[str, int]]:
    return [{"value": int(value), "count": count} for value, count in counter.most_common(limit)]


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return int(ordered[index])


def _sender_uin(record: dict[str, Any]) -> str:
    sender = record.get("sender") or {}
    if isinstance(sender, dict):
        return str(sender.get("uin") or sender.get("uid") or sender.get("user_id") or "")
    return ""


def _record_datetime(record: dict[str, Any]) -> datetime | None:
    timestamp = record.get("timestamp")
    if isinstance(timestamp, (int, float)):
        value = float(timestamp)
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    raw_time = record.get("time") or record.get("created_at") or record.get("createdAt")
    if isinstance(raw_time, str) and raw_time.strip():
        text = raw_time.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _is_system_or_recalled(record: dict[str, Any]) -> bool:
    if bool(record.get("system")) or bool(record.get("recalled")):
        return True
    record_type = str(record.get("type", "")).lower()
    return record_type in {"system", "recalled", "recall"}


def _extract_text(record: dict[str, Any]) -> str:
    content = record.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text
        element_texts = []
        for element in content.get("elements", []):
            if not isinstance(element, dict):
                continue
            data = element.get("data")
            if isinstance(data, dict):
                value = data.get("text") or data.get("content")
                if isinstance(value, str):
                    element_texts.append(value)
            elif isinstance(data, str):
                element_texts.append(data)
        if element_texts:
            return " ".join(element_texts)
    elif isinstance(content, str):
        return content

    for key in ("message_text", "text", "message", "raw_message"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_pure_attachment(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(ATTACHMENT_ONLY_PATTERN.fullmatch(compact) or CQ_ONLY_PATTERN.fullmatch(compact))


def _is_low_sensitive_style_text(text: str, safety_service: SafetyService) -> bool:
    if safety_service.contains_high_sensitivity(text):
        return False
    if not safety_service.can_store_long_term_memory(text):
        return False
    if URL_PATTERN.search(text):
        return False
    if LONG_NUMBER_PATTERN.search(text):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
