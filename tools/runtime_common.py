from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


def tcp_listening(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tcp_established_on_local_port(host: str, port: int) -> bool:
    """Return whether a TCP connection is established to the local bot port."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        else:
            result = subprocess.run(
                ["ss", "-tan"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return parse_tcp_established_output(result.stdout, host=host, port=port)


def parse_tcp_established_output(output: str, *, host: str, port: int) -> bool:
    local_hosts = {host, "0.0.0.0", "::", "::1", "[::1]", "localhost"}
    for line in output.splitlines():
        compact = " ".join(line.split())
        if not compact:
            continue
        parts = compact.split()
        upper_parts = {part.upper() for part in parts}
        if "ESTABLISHED" not in upper_parts and "ESTAB" not in upper_parts:
            continue
        endpoint_parts = [
            part for part in parts if f":{port}" in part or f".{port}" in part
        ]
        if not endpoint_parts:
            continue
        for endpoint in endpoint_parts[:1]:
            endpoint_host, endpoint_port = _split_endpoint(endpoint)
            if endpoint_port == port and endpoint_host in local_hosts:
                return True
    return False


def table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cursor.fetchone()[0])


def recent_rows(db_path: Path, table: str, *, limit: int) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
        return [_sanitize_row(dict(row)) for row in cursor.fetchall()]


def last_model_failure(db_path: Path) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT * FROM system_events
            WHERE event = 'model_generate_failed'
            ORDER BY id DESC LIMIT 1
            """
        )
        row = cursor.fetchone()
    return _sanitize_row(dict(row)) if row else None


def muted_group_states(db_path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT
              group_id,
              updated_by,
              reason,
              updated_at
            FROM group_mute_states
            WHERE muted = 1
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [_sanitize_row(dict(row)) for row in cursor.fetchall()]


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(row)
    for key in ("content", "detail", "raw_message"):
        if key in sanitized and sanitized[key] is not None:
            sanitized[key] = sanitize_text(str(sanitized[key]))
    return sanitized


def sanitize_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"fe_oa_[A-Za-z0-9]+", "fe_oa_[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"1[3-9]\d{9}", "[phone]", text)
    if len(text) > 160:
        return text[:157] + "..."
    return text


def _split_endpoint(endpoint: str) -> tuple[str, int | None]:
    cleaned = endpoint.strip()
    if cleaned.startswith("["):
        host, _, rest = cleaned[1:].partition("]:")
        return f"[{host}]", _parse_port(rest)
    if ":" in cleaned:
        host, _, port_text = cleaned.rpartition(":")
        return host, _parse_port(port_text)
    if "." in cleaned:
        host, _, port_text = cleaned.rpartition(".")
        return host, _parse_port(port_text)
    return cleaned, None


def _parse_port(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
