from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.inspect_history_sources import inspect_sources


JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check QQ Chat Exporter status and local readable history coverage.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=40653)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--qce-home",
        default=str(Path.home() / ".qq-chat-exporter"),
        help="QQ Chat Exporter local state directory. security.json is never read.",
    )
    parser.add_argument("--export-root", action="append", default=[])
    parser.add_argument("--runtime-db", action="append", default=[])
    parser.add_argument("--qqnt-root", action="append", default=[])
    parser.add_argument("--min-readable-messages", type=int, default=100_000)
    parser.add_argument(
        "--fail-on-incomplete",
        action="store_true",
        help="Exit non-zero when readable history coverage is below the target threshold.",
    )
    args = parser.parse_args()

    export_roots = [Path(path) for path in args.export_root]
    if not export_roots:
        export_roots = [Path(args.qce_home) / "exports"]

    data = check_qce_status(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        qce_home=Path(args.qce_home),
        export_roots=export_roots,
        runtime_dbs=[Path(path) for path in args.runtime_db],
        qqnt_roots=[Path(path) for path in args.qqnt_root],
        min_readable_messages=args.min_readable_messages,
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.fail_on_incomplete and data["coverage"]["status"] != "pass":
        return 1
    return 0


def check_qce_status(
    *,
    host: str,
    port: int,
    timeout: float,
    qce_home: Path,
    export_roots: list[Path],
    runtime_dbs: list[Path],
    qqnt_roots: list[Path],
    min_readable_messages: int,
) -> dict[str, Any]:
    base_url = f"http://{host}:{port}"
    checks = {
        "root": _http_probe(f"{base_url}/", timeout=timeout),
        "tasks": _http_probe(f"{base_url}/api/tasks", timeout=timeout),
        "tool": _http_probe(f"{base_url}/qce-v4-tool", timeout=timeout),
    }
    listening = any(check.get("ok") for check in checks.values())
    history = inspect_sources(
        export_roots=export_roots,
        export_dirs=[],
        history_files=[],
        runtime_dbs=runtime_dbs,
        sqlite_paths=[],
        qqnt_roots=qqnt_roots,
    )
    readable = int(history.get("readableExportMessages") or 0)
    coverage_status = "pass" if readable >= min_readable_messages else "fail"
    return {
        "qce": {
            "baseUrl": base_url,
            "listening": listening,
            "checks": checks,
            "apiTasks": _summarize_api_tasks(checks["tasks"].get("json")),
        },
        "localState": {
            "qceHome": str(qce_home),
            "tasksJsonl": _summarize_tasks_jsonl(qce_home / "tasks.jsonl"),
        },
        "coverage": {
            "status": coverage_status,
            "readableExportMessages": readable,
            "minReadableMessages": min_readable_messages,
            "exports": _coverage_exports(history),
            "qqntRoots": [str(path) for path in qqnt_roots],
            "qqntCandidateCount": len((history.get("qqntDiscovery") or {}).get("candidates") or []),
            "unreadableSqliteCandidateCount": sum(
                1
                for item in history.get("sqlite", [])
                if isinstance(item, dict) and not item.get("readableByStdSqlite")
            ),
        },
        "nextSteps": _next_steps(
            qce_listening=listening,
            readable_messages=readable,
            min_readable_messages=min_readable_messages,
        ),
    }


def _http_probe(url: str, *, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url, "ok": False}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "qq-bot-qce-status/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(64 * 1024)
            text = body.decode("utf-8", errors="replace")
            result.update(
                {
                    "ok": 200 <= response.status < 400,
                    "status": response.status,
                    "contentType": response.headers.get("Content-Type", ""),
                    "bodyLength": len(body),
                    "bodyShape": _body_shape(text),
                }
            )
            if result["bodyShape"] == "json_like":
                try:
                    result["json"] = json.loads(text)
                except json.JSONDecodeError:
                    result["jsonError"] = "invalid_json"
    except urllib.error.HTTPError as exc:
        result.update({"status": exc.code, "error": "http_error"})
    except urllib.error.URLError as exc:
        result.update({"error": "connection_error", "detail": str(exc.reason)})
    except TimeoutError:
        result.update({"error": "timeout"})
    except OSError as exc:
        result.update({"error": "connection_error", "detail": str(exc)})
    return result


def _body_shape(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "empty"
    if JWT_RE.match(stripped):
        return "jwt_like"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json_like"
    if stripped.startswith("<"):
        return "html_like"
    return "text"


def _summarize_api_tasks(value: Any) -> dict[str, Any]:
    if value is None:
        return {"available": False}
    tasks = _extract_task_list(value)
    return {
        "available": True,
        "taskCount": len(tasks),
        "latest": _safe_task(tasks[-1]) if tasks else None,
    }


def _extract_task_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "tasks", "items", "rows"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
            if isinstance(nested, dict):
                extracted = _extract_task_list(nested)
                if extracted:
                    return extracted
    return []


def _summarize_tasks_jsonl(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "taskLines": 0,
        "latest": None,
    }
    if not path.exists():
        return summary
    latest: dict[str, Any] | None = None
    status_counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        summary["taskLines"] += 1
        latest = row
        state = _decode_json_field(row.get("state"))
        status = state.get("status") if isinstance(state, dict) else None
        if isinstance(status, str):
            status_counts[status] = status_counts.get(status, 0) + 1
    summary["statusCounts"] = status_counts
    summary["latest"] = _safe_task(latest) if latest else None
    return summary


def _safe_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict):
        return {}
    config = _decode_json_field(task.get("config"))
    state = _decode_json_field(task.get("state"))
    if not config:
        config = task
    if not state:
        state = task
    return {
        "taskId": config.get("taskId") or state.get("taskId") or task.get("taskId"),
        "taskName": config.get("taskName") or task.get("taskName"),
        "chatType": config.get("chatType") or task.get("chatType"),
        "formats": config.get("formats") or task.get("formats"),
        "outputDir": config.get("outputDir") or task.get("outputDir"),
        "batchSize": config.get("batchSize") or task.get("batchSize"),
        "status": state.get("status") or task.get("status"),
        "processedMessages": state.get("processedMessages") or task.get("processedMessages"),
        "successCount": state.get("successCount") or task.get("successCount"),
        "failureCount": state.get("failureCount") or task.get("failureCount"),
        "fileName": state.get("fileName") or task.get("fileName"),
        "updatedAt": task.get("updatedAt") or state.get("updatedAt") or config.get("updatedAt"),
    }


def _decode_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _coverage_exports(history: dict[str, Any]) -> list[dict[str, Any]]:
    exports = history.get("exports")
    if not isinstance(exports, list):
        return []
    result = []
    for export in exports:
        if not isinstance(export, dict):
            continue
        result.append(
            {
                "kind": export.get("kind"),
                "path": export.get("path") or export.get("paths"),
                "historyFiles": export.get("historyFiles"),
                "jsonlFiles": export.get("jsonlFiles"),
                "readableHistoryMessages": export.get("readableHistoryMessages"),
            }
        )
    return result


def _next_steps(
    *,
    qce_listening: bool,
    readable_messages: int,
    min_readable_messages: int,
) -> list[str]:
    steps: list[str] = []
    if not qce_listening:
        steps.append(
            "Start NapCat with the qq-chat-exporter plugin until http://127.0.0.1:40653/qce-v4-tool is reachable."
        )
    if readable_messages < min_readable_messages:
        steps.append(
            "Export the target group as QQ Chat Exporter STREAMING_JSONL/chunked-jsonl, then rerun inspect_history_sources.py, build_style_profile.py, and audit_human_like_goal.py."
        )
    if not steps:
        steps.append("Readable history coverage is sufficient; rebuild the persona profile and rerun the goal audit.")
    return steps


if __name__ == "__main__":
    raise SystemExit(main())
