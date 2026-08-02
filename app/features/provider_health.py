from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


SystemEventRecorder = Callable[..., Awaitable[None]]
DEFAULT_PROVIDER_STATUS_PATH = "run/provider-status.json"


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold <= 0 or recovery_seconds <= 0:
            raise ValueError("circuit breaker settings must be positive")
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                assert self._opened_at is not None
                if self._clock() - self._opened_at < self._recovery_seconds:
                    return False
                self._state = "half_open"
            if self._half_open_in_flight:
                return False
            self._half_open_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._half_open_in_flight = False
            self._consecutive_failures += 1
            if (
                self._state == "half_open"
                or self._consecutive_failures >= self._failure_threshold
            ):
                self._state = "open"
                self._opened_at = self._clock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state


class ProviderHealthRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(
            path
            or os.getenv(
                "QQ_BOT_PROVIDER_STATUS_PATH",
                DEFAULT_PROVIDER_STATUS_PATH,
            )
        )
        self._lock = threading.Lock()
        self._providers = self._load_existing()

    async def record_attempt(
        self,
        *,
        kind: str,
        provider: str,
        target: str,
        stage: str,
        success: bool,
        attempts: int,
        duration_ms: int,
        error_category: str | None = None,
        circuit_state: str = "closed",
        record_system_event: SystemEventRecorder | None = None,
        trace_id: str | None = None,
    ) -> None:
        safe_target = sanitize_provider_target(target)
        key = f"{kind}:{provider}:{safe_target}"
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            with _status_file_lock(self._path):
                self._providers = self._load_existing()
                previous = dict(self._providers.get(key, {}))
                success_count = int(previous.get("success_count", 0)) + int(success)
                failure_count = int(previous.get("failure_count", 0)) + int(not success)
                error_counts = dict(previous.get("error_counts", {}))
                safe_error = None if success else _safe_label(error_category or "unknown")
                if safe_error is not None:
                    error_counts[safe_error] = int(error_counts.get(safe_error, 0)) + 1
                self._providers[key] = {
                    "kind": _safe_label(kind),
                    "provider": _safe_label(provider),
                    "target": safe_target,
                    "state": "healthy" if success else "degraded",
                    "circuit_state": _safe_label(circuit_state),
                    "stage": _safe_label(stage),
                    "attempts": max(1, int(attempts)),
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "error_category": safe_error,
                    "last_error_category": (
                        safe_error or previous.get("last_error_category")
                    ),
                    "error_counts": error_counts,
                    "duration_ms": max(0, int(duration_ms)),
                    "updated_at": now,
                }
                self._persist_locked(
                    now,
                    mode="runtime_telemetry",
                    healthy=all(
                        item.get("state") == "healthy"
                        for item in self._providers.values()
                    ),
                )

        if record_system_event is not None:
            detail = json.dumps(
                {
                    "provider": _safe_label(provider),
                    "target": safe_target,
                    "stage": _safe_label(stage),
                    "attempts": max(1, int(attempts)),
                    "duration_ms": max(0, int(duration_ms)),
                    "result": "success" if success else "failure",
                    "error_category": (
                        None if success else _safe_label(error_category or "unknown")
                    ),
                    "circuit_state": _safe_label(circuit_state),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
            await record_system_event(
                level="INFO" if success else "WARNING",
                event=f"{_safe_label(kind)}_provider_attempt",
                detail=detail,
                trace_id=trace_id,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._providers = self._load_existing()
            providers = {key: dict(value) for key, value in sorted(self._providers.items())}
        return {
            "schema_version": 1,
            "updated_at": max(
                (str(value.get("updated_at") or "") for value in providers.values()),
                default=None,
            ),
            "providers": providers,
        }

    def replace_snapshot(
        self,
        providers: dict[str, dict[str, Any]],
        *,
        mode: str,
        healthy: bool,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            with _status_file_lock(self._path):
                self._providers = {
                    str(key): dict(value)
                    for key, value in providers.items()
                    if isinstance(value, dict)
                }
                self._persist_locked(now, mode=mode, healthy=healthy)
        return {
            "schema_version": 1,
            "mode": mode,
            "updated_at": now,
            "healthy": healthy,
            "providers": {
                key: dict(self._providers[key]) for key in sorted(self._providers)
            },
        }

    def _load_existing(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return {}
        providers = payload.get("providers")
        if not isinstance(providers, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in providers.items()
            if isinstance(value, dict)
        }

    def _persist_locked(
        self,
        updated_at: str,
        *,
        mode: str,
        healthy: bool,
    ) -> None:
        payload = {
            "schema_version": 1,
            "mode": mode,
            "updated_at": updated_at,
            "healthy": healthy,
            "providers": {key: self._providers[key] for key in sorted(self._providers)},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for attempt in range(5):
            try:
                os.replace(temporary, self._path)
                break
            except PermissionError:
                if attempt >= 4:
                    temporary.unlink(missing_ok=True)
                    raise
                time.sleep(0.01 * (attempt + 1))


@contextmanager
def _status_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            for attempt in range(500):
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if attempt >= 499:
                        raise TimeoutError("provider status lock timed out")
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def classify_provider_error(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "network_timeout"
    explicit_category = getattr(exc, "category", None)
    if isinstance(explicit_category, str) and explicit_category.strip():
        return _safe_label(explicit_category)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        if 400 <= status < 500:
            return "http_4xx"
        if status >= 500:
            return "http_5xx"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, OSError)):
        return "network_error"
    if isinstance(exc, (ValueError, KeyError, json.JSONDecodeError)):
        return "invalid_response"
    return "provider_error"


def sanitize_provider_target(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.hostname:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.hostname.lower()}{port}#{digest}"
    return _safe_label(raw or "default")


def _safe_label(value: str) -> str:
    cleaned = "".join(
        char for char in str(value or "").strip().lower() if char.isalnum() or char in "_-.:#"
    )
    return cleaned[:80] or "unknown"


_DEFAULT_REGISTRY: ProviderHealthRegistry | None = None


def default_provider_health_registry() -> ProviderHealthRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ProviderHealthRegistry()
    return _DEFAULT_REGISTRY
