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

from tools.build_style_profile import _discover_history_files
from tools.build_style_profile import _read_runtime_db_sources


SQLITE_MAGIC = b"SQLite format 3\x00"
QQNT_SQLITE_HEADER = b"SQLite header 3\x00"
QQNT_MARKER = b"QQ_NT DB"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect readable QQ history sources without modifying them.",
    )
    parser.add_argument("--export-root", action="append", default=[])
    parser.add_argument("--export-dir", action="append", default=[])
    parser.add_argument("--history-file", action="append", default=[])
    parser.add_argument("--runtime-db", action="append", default=[])
    parser.add_argument("--sqlite-path", action="append", default=[])
    parser.add_argument(
        "--qqnt-root",
        action="append",
        default=[],
        help="QQNT account/root directory to scan for nt_qq/nt_db/*.db candidates.",
    )
    args = parser.parse_args()

    data = inspect_sources(
        export_roots=[Path(path) for path in args.export_root],
        export_dirs=[Path(path) for path in args.export_dir],
        history_files=[Path(path) for path in args.history_file],
        runtime_dbs=[Path(path) for path in args.runtime_db],
        sqlite_paths=[Path(path) for path in args.sqlite_path],
        qqnt_roots=[Path(path) for path in args.qqnt_root],
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data["readableExportMessages"] > 0 or data["sqlite"] else 1


def inspect_sources(
    *,
    export_roots: list[Path],
    export_dirs: list[Path],
    history_files: list[Path] | None = None,
    runtime_dbs: list[Path] | None = None,
    sqlite_paths: list[Path],
    qqnt_roots: list[Path] | None = None,
) -> dict[str, Any]:
    exports: list[dict[str, Any]] = []
    for root in export_roots:
        files, sources = _discover_history_files(input_dirs=[], input_root=root)
        exports.append(
            {
                "kind": "export_root",
                "path": str(root),
                "historyFiles": len(files),
                "jsonlFiles": sum(1 for file_path in files if file_path.suffix.lower() == ".jsonl"),
                "textFiles": sum(1 for file_path in files if file_path.suffix.lower() == ".txt"),
                "htmlFiles": sum(1 for file_path in files if file_path.suffix.lower() in {".html", ".htm"}),
                "sources": sources,
                "manifestTotalMessages": _manifest_total(sources),
                "readableRecords": _readable_records(sources),
                "readableHistoryMessages": _readable_messages(sources),
            }
        )
    if export_dirs:
        files, sources = _discover_history_files(input_dirs=export_dirs, input_root=None)
        exports.append(
            {
                "kind": "export_dirs",
                "paths": [str(path) for path in export_dirs],
                "historyFiles": len(files),
                "jsonlFiles": sum(1 for file_path in files if file_path.suffix.lower() == ".jsonl"),
                "textFiles": sum(1 for file_path in files if file_path.suffix.lower() == ".txt"),
                "htmlFiles": sum(1 for file_path in files if file_path.suffix.lower() in {".html", ".htm"}),
                "sources": sources,
                "manifestTotalMessages": _manifest_total(sources),
                "readableRecords": _readable_records(sources),
                "readableHistoryMessages": _readable_messages(sources),
            }
        )
    if history_files:
        files, sources = _discover_history_files(
            input_dirs=[],
            input_root=None,
            input_files=history_files,
        )
        exports.append(
            {
                "kind": "history_files",
                "paths": [str(path) for path in history_files],
                "historyFiles": len(files),
                "jsonlFiles": sum(1 for file_path in files if file_path.suffix.lower() == ".jsonl"),
                "textFiles": sum(1 for file_path in files if file_path.suffix.lower() == ".txt"),
                "htmlFiles": sum(1 for file_path in files if file_path.suffix.lower() in {".html", ".htm"}),
                "sources": sources,
                "manifestTotalMessages": _manifest_total(sources),
                "readableRecords": _readable_records(sources),
                "readableHistoryMessages": _readable_messages(sources),
            }
        )
    if runtime_dbs:
        sources = _read_runtime_db_sources(runtime_dbs)
        exports.append(
            {
                "kind": "runtime_dbs",
                "paths": [str(path) for path in runtime_dbs],
                "historyFiles": len(runtime_dbs),
                "jsonlFiles": 0,
                "textFiles": 0,
                "htmlFiles": 0,
                "sources": sources,
                "manifestTotalMessages": 0,
                "readableRecords": _readable_records(sources),
                "readableHistoryMessages": _readable_records(sources),
            }
        )
    discovered_qqnt = _discover_qqnt_sqlite_candidates(qqnt_roots or [])
    sqlite_candidates = _dedupe_paths([*sqlite_paths, *discovered_qqnt])
    sqlite_results = [_inspect_sqlite_candidate(path) for path in sqlite_candidates]
    return {
        "exports": exports,
        "readableExportMessages": sum(int(item.get("readableHistoryMessages") or 0) for item in exports),
        "sqlite": sqlite_results,
        "qqntDiscovery": {
            "roots": [str(path) for path in (qqnt_roots or [])],
            "candidates": [str(path) for path in discovered_qqnt],
        },
    }


def _manifest_total(sources: list[dict[str, Any]]) -> int:
    total = 0
    for source in sources:
        manifest = source.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("totalMessages"), int):
            total += int(manifest["totalMessages"])
    return total


def _readable_records(sources: list[dict[str, Any]]) -> int:
    return sum(
        int(source.get("readableRecords") or 0)
        for source in sources
        if isinstance(source, dict)
    )


def _readable_messages(sources: list[dict[str, Any]]) -> int:
    return _manifest_total(sources) + _readable_records(sources)


def _discover_qqnt_sqlite_candidates(roots: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    preferred_names = {
        "nt_msg.db",
        "group_msg_fts.db",
        "group_info.db",
        "profile_info.db",
    }
    for root in roots:
        if root.is_file():
            candidates.append(root)
            continue
        if not root.exists():
            continue
        nt_db_dirs = [path for path in root.rglob("nt_db") if path.is_dir()]
        for nt_db_dir in nt_db_dirs:
            has_qqnt_parent = any(parent.name == "nt_qq" for parent in nt_db_dir.parents)
            if not has_qqnt_parent:
                continue
            named = [nt_db_dir / name for name in preferred_names]
            candidates.extend(path for path in named if path.exists())
            candidates.extend(
                path
                for path in sorted(nt_db_dir.glob("*.db"))
                if path.name not in preferred_names
            )
    return _dedupe_paths(candidates)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.resolve(strict=False)).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _inspect_sqlite_candidate(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "sizeBytes": path.stat().st_size if path.exists() else 0,
    }
    if not path.exists():
        return result

    header = path.read_bytes()[:100]
    result["headerHex"] = header[:16].hex()
    result["sqliteMagic"] = header[:16] == SQLITE_MAGIC
    result["qqNtHeader"] = header[:16] == QQNT_SQLITE_HEADER
    result["qqNtMarker"] = QQNT_MARKER in header[:64]
    if result["qqNtHeader"] or result["qqNtMarker"]:
        result["formatHint"] = "qqnt_encrypted_or_custom_sqlite"
    if len(header) >= 48:
        result["pageSize"] = int.from_bytes(header[16:18], "big")
        result["writeVersion"] = header[18]
        result["readVersion"] = header[19]
        result["reservedBytes"] = header[20]
        result["pageCount"] = int.from_bytes(header[28:32], "big")
        result["schemaFormat"] = int.from_bytes(header[44:48], "big")
        result["normalSqliteHeader"] = (
            result["sqliteMagic"]
            and result["readVersion"] in {1, 2}
            and result["writeVersion"] in {1, 2}
        )

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT COUNT(*) AS count FROM sqlite_master").fetchone()
            tables = conn.execute(
                """
                SELECT name, type
                FROM sqlite_master
                WHERE type IN ('table', 'view')
                ORDER BY name
                LIMIT 20
                """
            ).fetchall()
        result["readableByStdSqlite"] = True
        result["schemaObjectCount"] = int(row["count"]) if row is not None else 0
        result["sampleObjects"] = [dict(table) for table in tables]
    except sqlite3.Error as exc:
        result["readableByStdSqlite"] = False
        result["sqliteError"] = str(exc)
        if result.get("formatHint") == "qqnt_encrypted_or_custom_sqlite":
            result["nextStep"] = (
                "Use a QQNT-aware export/decryption path before counting this "
                "file as readable chat history."
            )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
