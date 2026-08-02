from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.models import VideoConfig


SCHEMA_VERSION = 1
DEFAULT_ACCEPTANCE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_PLAN_TTL_SECONDS = 300.0


class VideoUploadAcceptanceError(RuntimeError):
    def __init__(self, message: str, *, category: str = "validation_failed") -> None:
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class VideoUploadAcceptanceResult:
    plan: dict[str, Any]
    duration_ms: int


class VideoUploadPlanGate:
    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PLAN_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("plan TTL must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._plans: dict[str, tuple[dict[str, Any], float]] = {}

    def issue(
        self,
        config: VideoConfig,
        *,
        group_id: str,
        relative_file: str,
    ) -> dict[str, Any]:
        plan = build_video_upload_plan(
            config,
            group_id=group_id,
            relative_file=relative_file,
        )
        self._plans[plan["plan_hash"]] = (
            plan,
            self._clock() + self._ttl_seconds,
        )
        return plan

    def consume(
        self,
        config: VideoConfig,
        *,
        group_id: str,
        relative_file: str,
        plan_hash: str,
        confirmed_group_id: str,
    ) -> dict[str, Any]:
        normalized_group = _normalize_group_id(group_id)
        if _normalize_group_id(confirmed_group_id) != normalized_group:
            raise VideoUploadAcceptanceError(
                "confirmed group id does not match the target",
                category="group_confirmation_mismatch",
            )
        stored = self._plans.get(plan_hash)
        if stored is None:
            raise VideoUploadAcceptanceError(
                "plan is missing, expired, or already consumed; rerun plan",
                category="plan_missing_or_consumed",
            )
        expected, expires_at = stored
        if self._clock() > expires_at:
            self._plans.pop(plan_hash, None)
            raise VideoUploadAcceptanceError(
                "plan expired; rerun plan",
                category="plan_expired",
            )
        fresh = build_video_upload_plan(
            config,
            group_id=normalized_group,
            relative_file=relative_file,
        )
        if expected != fresh or fresh["plan_hash"] != plan_hash:
            self._plans.pop(plan_hash, None)
            raise VideoUploadAcceptanceError(
                "plan hash mismatch; rerun plan",
                category="plan_hash_mismatch",
            )
        self._plans.pop(plan_hash, None)
        return fresh


def build_video_upload_plan(
    config: VideoConfig,
    *,
    group_id: str,
    relative_file: str,
) -> dict[str, Any]:
    normalized_group = _normalize_group_id(group_id)
    host_root = Path(config.host_cache_path).resolve()
    candidate = _resolve_cache_file(host_root, relative_file)
    maximum_bytes = min(
        DEFAULT_ACCEPTANCE_MAX_BYTES,
        config.qq_video_max_bytes or DEFAULT_ACCEPTANCE_MAX_BYTES,
    )
    size_bytes = candidate.stat().st_size
    if size_bytes <= 0:
        raise VideoUploadAcceptanceError(
            "acceptance video must not be empty",
            category="file_empty",
        )
    if size_bytes > maximum_bytes:
        raise VideoUploadAcceptanceError(
            "acceptance video exceeds the small-file limit",
            category="file_too_large",
        )
    relative = candidate.relative_to(host_root)
    container_file = str(
        PurePosixPath(config.container_cache_path) / PurePosixPath(relative.as_posix())
    )
    plan = {
        "schema_version": SCHEMA_VERSION,
        "operation": "upload_small_video_acceptance",
        "group_id": normalized_group,
        "relative_file": relative.as_posix(),
        "upload_name": candidate.name,
        "container_file": container_file,
        "size_bytes": size_bytes,
        "maximum_bytes": maximum_bytes,
        "sha256": _file_sha256(candidate),
    }
    plan["plan_hash"] = _plan_hash(plan)
    return plan


async def apply_video_upload_plan(
    bot,
    config: VideoConfig,
    *,
    group_id: str,
    relative_file: str,
    plan_hash: str,
    confirmed_group_id: str,
    expected_plan: dict[str, Any],
) -> VideoUploadAcceptanceResult:
    normalized_group = _normalize_group_id(group_id)
    if _normalize_group_id(confirmed_group_id) != normalized_group:
        raise VideoUploadAcceptanceError(
            "confirmed group id does not match the target",
            category="group_confirmation_mismatch",
        )
    plan = build_video_upload_plan(
        config,
        group_id=normalized_group,
        relative_file=relative_file,
    )
    if not plan_hash or plan_hash != plan["plan_hash"]:
        raise VideoUploadAcceptanceError(
            "plan hash mismatch; rerun plan",
            category="plan_hash_mismatch",
        )
    if expected_plan != plan:
        raise VideoUploadAcceptanceError(
            "consumed plan no longer matches the file snapshot",
            category="plan_hash_mismatch",
        )
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            bot.call_api(
                "upload_group_file",
                group_id=int(normalized_group),
                file=plan["container_file"],
                name=plan["upload_name"],
            ),
            timeout=config.send_timeout_seconds,
        )
    except TimeoutError as exc:
        raise VideoUploadAcceptanceError(
            "upload_group_file timed out",
            category="network_timeout",
        ) from exc
    except Exception as exc:
        raise VideoUploadAcceptanceError(
            f"upload_group_file failed: {type(exc).__name__}",
            category="upload_failed",
        ) from exc
    return VideoUploadAcceptanceResult(
        plan=plan,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )


def acceptance_audit_detail(
    *,
    plan: dict[str, Any] | None,
    stage: str,
    result: str,
    duration_ms: int,
    error_category: str | None = None,
) -> str:
    group_id = str((plan or {}).get("group_id") or "")
    payload = {
        "platform": "napcat",
        "stage": stage,
        "attempts": 1,
        "duration_ms": max(0, int(duration_ms)),
        "result": result,
        "error_category": error_category,
        "group_ref": hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:10]
        if group_id
        else None,
        "size_bytes": (plan or {}).get("size_bytes"),
        "file_sha256_prefix": str((plan or {}).get("sha256") or "")[:12] or None,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _resolve_cache_file(host_root: Path, relative_file: str) -> Path:
    raw = str(relative_file or "").strip()
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise VideoUploadAcceptanceError(
            "acceptance file must be cache-relative",
            category="invalid_cache_path",
        )
    candidate = (host_root / relative).resolve()
    try:
        candidate.relative_to(host_root)
    except ValueError as exc:
        raise VideoUploadAcceptanceError(
            "acceptance file escaped the cache root",
            category="invalid_cache_path",
        ) from exc
    if candidate.suffix.lower() != ".mp4":
        raise VideoUploadAcceptanceError(
            "acceptance file must be an MP4",
            category="invalid_file_type",
        )
    unresolved = host_root / relative
    if unresolved.is_symlink() or not candidate.is_file():
        raise VideoUploadAcceptanceError(
            "acceptance file is missing or unsafe",
            category="file_missing_or_unsafe",
        )
    return candidate


def _normalize_group_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise VideoUploadAcceptanceError(
            "group id must be a positive decimal id",
            category="invalid_group_id",
        )
    return normalized


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plan_hash(plan: dict[str, Any]) -> str:
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
