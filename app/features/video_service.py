from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from app.features.contracts import VideoAsset, VideoExtractor
from app.features.provider_health import (
    CircuitBreaker,
    ProviderHealthRegistry,
    SystemEventRecorder,
    default_provider_health_registry,
)
from app.features.video_providers import VideoExtractorError
from app.models import NormalizedMessage, RetryConfig, VideoConfig
from app.retry import RetryClassification, RetryExhaustedError, run_with_retry


ProgressSender = Callable[[str], Awaitable[None]]
VIDEO_FINAL_MESSAGE_MAX_CHARS = 1000
VIDEO_CANONICAL_URL_CACHE_MAX_ENTRIES = 1024
_VIDEO_FINAL_TRUNCATION = "内容已截断；下一页：请分批发送剩余视频链接"


@dataclass(frozen=True)
class VideoCommandResult:
    handled: bool
    text: str
    reason: str
    sent_count: int = 0
    failure_count: int = 0


@dataclass
class _SourceLockEntry:
    lock: asyncio.Lock
    users: int = 0


@dataclass(frozen=True)
class _DownloadOutcome:
    source_url: str
    canonical_url: str
    platform: str
    asset: VideoAsset | None = None
    error: RetryExhaustedError | None = None
    source_lock: _SourceLockEntry | None = None


class GroupVideoService:
    def __init__(
        self,
        *,
        extractor: VideoExtractor | None,
        config: VideoConfig,
        retry_policy: RetryConfig | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        health_registry: ProviderHealthRegistry | None = None,
        record_system_event: SystemEventRecorder | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._extractor = extractor
        self._config = config
        base_policy = retry_policy or RetryConfig()
        self._retry_policy = RetryConfig(
            max_attempts=base_policy.max_attempts,
            timeout_multipliers=base_policy.timeout_multipliers,
            backoff_seconds=base_policy.backoff_seconds,
        )
        self._retry_sleep = retry_sleep
        self._health = health_registry or default_provider_health_registry()
        self._record_system_event = record_system_event
        self._clock = clock
        self._jitter = jitter
        self._global_downloads = asyncio.Semaphore(config.global_concurrency)
        self._group_upload_locks: dict[str, asyncio.Lock] = {}
        self._source_locks: dict[str, _SourceLockEntry] = {}
        self._source_locks_guard = asyncio.Lock()
        self._domain_breakers: dict[str, CircuitBreaker] = {}
        self._canonical_urls: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._upload_supported: bool | None = None

    async def handle(
        self,
        bot,
        message: NormalizedMessage,
        *,
        on_progress: ProgressSender | None = None,
    ) -> VideoCommandResult | None:
        urls = extract_supported_video_urls(message.text)
        if not urls or message.scope_type != "group" or not message.group_id:
            return None
        if not self._config.enabled or self._extractor is None:
            return VideoCommandResult(True, "视频下载功能未配置", "video_unconfigured")

        cache_dir = Path(self._config.host_cache_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        if (
            self._config.min_free_bytes
            and shutil.disk_usage(cache_dir).free < self._config.min_free_bytes
        ):
            return VideoCommandResult(
                True,
                "视频下载失败：本地缓存空间不足",
                "video_disk_space_low",
                failure_count=len(urls),
            )

        batch_started = time.perf_counter()
        progress_task = (
            asyncio.create_task(self._send_progress_after_threshold(on_progress))
            if on_progress is not None
            else None
        )
        try:
            probe_error = await self._probe_upload_group_file(
                bot,
                group_id=message.group_id,
                trace_id=message.trace_id,
            )
        except BaseException:
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            raise
        if probe_error is not None:
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            elapsed_ms = round((time.perf_counter() - batch_started) * 1000)
            await self._record_batch(
                trace_id=message.trace_id,
                result="video_upload_probe_failed",
                success_count=0,
                failure_count=len(urls),
                duration_ms=elapsed_ms,
            )
            return VideoCommandResult(
                True,
                _format_probe_failure(probe_error),
                "video_upload_probe_failed",
                failure_count=len(urls),
            )

        per_message = asyncio.Semaphore(self._config.per_message_concurrency)
        outcomes: list[_DownloadOutcome] = []
        failures: list[str] = []
        sent_count = 0
        try:
            outcomes = list(
                await asyncio.gather(
                    *(
                        self._download(url, per_message, trace_id=message.trace_id)
                        for url in urls
                    )
                )
            )
            upload_lock = self._group_upload_locks.setdefault(
                message.group_id,
                asyncio.Lock(),
            )
            async with upload_lock:
                for outcome in outcomes:
                    if outcome.error is not None:
                        failures.append(
                            _format_failure(
                                outcome.platform,
                                None,
                                "下载",
                                outcome.error,
                            )
                        )
                        continue
                    asset = outcome.asset
                    if asset is None:
                        continue
                    try:
                        validation_started = time.perf_counter()
                        validation_error = self._validate_asset_size(asset)
                        await self._record_stage(
                            platform=outcome.platform,
                            target=asset.source_url,
                            stage="validate",
                            success=validation_error is None,
                            attempts=1,
                            started=validation_started,
                            error_category=(
                                validation_error.category
                                if validation_error is not None
                                else None
                            ),
                            trace_id=message.trace_id,
                        )
                        if validation_error is not None:
                            failures.append(
                                _format_failure(
                                    asset.provider
                                    or video_platform_label(asset.source_url),
                                    asset.title,
                                    "发送",
                                    validation_error,
                                )
                            )
                            continue
                        started = time.perf_counter()
                        try:
                            retry_result = await run_with_retry(
                                lambda timeout: self._upload_group_file(
                                    bot,
                                    group_id=message.group_id or "",
                                    asset=asset,
                                ),
                                stage="video_send",
                                base_timeout_seconds=self._config.send_timeout_seconds,
                                policy=self._retry_policy,
                                classify=_classify_send_error,
                                sleep=self._sleep_with_jitter,
                            )
                        except RetryExhaustedError as exc:
                            await self._record_stage(
                                platform=outcome.platform,
                                target="onebot://upload_group_file",
                                stage="upload",
                                success=False,
                                attempts=exc.attempts,
                                started=started,
                                error_category=exc.category,
                                trace_id=message.trace_id,
                            )
                            failures.append(
                                _format_failure(
                                    outcome.platform,
                                    asset.title,
                                    "发送",
                                    exc,
                                )
                            )
                        else:
                            await self._record_stage(
                                platform=outcome.platform,
                                target="onebot://upload_group_file",
                                stage="upload",
                                success=True,
                                attempts=retry_result.attempts,
                                started=started,
                                trace_id=message.trace_id,
                            )
                            sent_count += 1
                    finally:
                        await self._cleanup_asset(asset)
        finally:
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            for outcome in outcomes:
                if outcome.asset is not None:
                    await self._cleanup_asset(outcome.asset)
            await asyncio.gather(
                *(
                    self._cleanup_source(outcome.canonical_url or outcome.source_url)
                    for outcome in outcomes
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if outcome.source_lock is not None:
                    await self._release_source_lock(
                        outcome.source_url,
                        outcome.source_lock,
                    )

        failure_count = len(failures)
        reason = "video_files_sent" if not failures else "video_batch_partial_failure"
        if failures and sent_count == 0:
            reason = "video_batch_failed"
        elapsed_ms = round((time.perf_counter() - batch_started) * 1000)
        summary = f"视频处理完成：成功 {sent_count} 个，失败 {failure_count} 个，耗时 {elapsed_ms} ms"
        text = _format_final_result(summary, failures)
        await self._record_batch(
            trace_id=message.trace_id,
            result=reason,
            success_count=sent_count,
            failure_count=failure_count,
            duration_ms=elapsed_ms,
        )
        return VideoCommandResult(
            True,
            text,
            reason,
            sent_count=sent_count,
            failure_count=failure_count,
        )

    async def _send_progress_after_threshold(
        self,
        on_progress: ProgressSender | None,
    ) -> None:
        if on_progress is None:
            return
        await asyncio.sleep(self._config.progress_threshold_seconds)
        await on_progress("视频处理中…")

    async def _download(
        self,
        source_url: str,
        per_message: asyncio.Semaphore,
        *,
        trace_id: str,
    ) -> _DownloadOutcome:
        platform = video_platform_label(source_url)
        source_lock = await self._acquire_source_lock(source_url)
        canonical_url = source_url
        try:
            canonical_url, resolve_error = await self._canonical_url(
                source_url,
                platform=platform,
                trace_id=trace_id,
            )
            if resolve_error is not None:
                return _DownloadOutcome(
                    source_url,
                    canonical_url,
                    platform,
                    error=resolve_error,
                    source_lock=source_lock,
                )
            domain = _video_circuit_domain(canonical_url)
            breaker = self._domain_breakers.setdefault(
                domain,
                CircuitBreaker(
                    failure_threshold=self._config.domain_failure_threshold,
                    recovery_seconds=self._config.domain_recovery_seconds,
                    clock=self._clock,
                ),
            )
            if not breaker.allow_request():
                error = RetryExhaustedError(
                    stage="video_download",
                    category="domain_circuit_open",
                    attempts=1,
                    timeout_seconds=self._config.download_timeout_seconds,
                )
                await self._record_stage(
                    platform=platform,
                    target=canonical_url,
                    stage="download",
                    success=False,
                    attempts=1,
                    started=time.perf_counter(),
                    error_category=error.category,
                    circuit_state=breaker.state,
                    trace_id=trace_id,
                )
                return _DownloadOutcome(
                    source_url,
                    canonical_url,
                    platform,
                    error=error,
                    source_lock=source_lock,
                )
            async with per_message, self._global_downloads:
                started = time.perf_counter()

                def classify_download_attempt(exc: Exception) -> RetryClassification:
                    classification = _classify_download_error(exc)
                    if classification.category in {"network_timeout", "network_error"}:
                        breaker.record_failure()
                        if breaker.state == "open":
                            return RetryClassification(
                                classification.category,
                                retryable=False,
                            )
                    return classification

                try:
                    result = await run_with_retry(
                        lambda timeout: self._extractor.extract(canonical_url),  # type: ignore[union-attr]
                        stage="video_download",
                        base_timeout_seconds=self._config.download_timeout_seconds,
                        policy=self._retry_policy,
                        classify=classify_download_attempt,
                        sleep=self._sleep_with_jitter,
                    )
                except RetryExhaustedError as exc:
                    if exc.category not in {"network_timeout", "network_error"}:
                        breaker.record_success()
                    await self._record_stage(
                        platform=platform,
                        target=canonical_url,
                        stage="download",
                        success=False,
                        attempts=exc.attempts,
                        started=started,
                        error_category=exc.category,
                        circuit_state=breaker.state,
                        trace_id=trace_id,
                    )
                    return _DownloadOutcome(
                        source_url,
                        canonical_url,
                        platform,
                        error=exc,
                        source_lock=source_lock,
                    )
                breaker.record_success()
                await self._record_stage(
                    platform=platform,
                    target=canonical_url,
                    stage="download",
                    success=True,
                    attempts=result.attempts,
                    started=started,
                    circuit_state=breaker.state,
                    trace_id=trace_id,
                )
                return _DownloadOutcome(
                    source_url,
                    canonical_url,
                    platform,
                    asset=result.value,
                    source_lock=source_lock,
                )
        except BaseException:
            try:
                await self._cleanup_source(canonical_url)
            finally:
                await self._release_source_lock(source_url, source_lock)
            raise

    async def _canonical_url(
        self,
        source_url: str,
        *,
        platform: str,
        trace_id: str,
    ) -> tuple[str, RetryExhaustedError | None]:
        self._prune_canonical_urls()
        cached = self._canonical_urls.get(source_url)
        if cached is not None and cached[1] > self._clock():
            self._canonical_urls.move_to_end(source_url)
            return cached[0], None
        resolver = getattr(self._extractor, "resolve_url", None)
        if resolver is None:
            self._cache_canonical_url(
                source_url,
                source_url,
            )
            return source_url, None
        started = time.perf_counter()
        resolve_timeout = min(self._config.download_timeout_seconds, 30.0)
        try:
            result = await run_with_retry(
                lambda timeout: resolver(source_url),
                stage="video_resolve",
                base_timeout_seconds=resolve_timeout,
                policy=self._retry_policy,
                classify=_classify_download_error,
                sleep=self._sleep_with_jitter,
            )
        except RetryExhaustedError as exc:
            await self._record_stage(
                platform=platform,
                target=source_url,
                stage="resolve",
                success=False,
                attempts=exc.attempts,
                started=started,
                error_category=exc.category,
                trace_id=trace_id,
            )
            return source_url, exc
        canonical = result.value
        canonical = canonical if _is_supported_video_url(canonical) else source_url
        self._cache_canonical_url(source_url, canonical)
        await self._record_stage(
            platform=platform,
            target=canonical,
            stage="resolve",
            success=True,
            attempts=result.attempts,
            started=started,
            trace_id=trace_id,
        )
        return canonical, None

    async def _acquire_source_lock(self, source_url: str) -> _SourceLockEntry:
        async with self._source_locks_guard:
            entry = self._source_locks.get(source_url)
            if entry is None:
                entry = _SourceLockEntry(lock=asyncio.Lock())
                self._source_locks[source_url] = entry
            entry.users += 1
        try:
            await entry.lock.acquire()
        except BaseException:
            async with self._source_locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._source_locks.get(source_url) is entry:
                    self._source_locks.pop(source_url, None)
            raise
        return entry

    async def _release_source_lock(
        self,
        source_url: str,
        entry: _SourceLockEntry,
    ) -> None:
        entry.lock.release()
        async with self._source_locks_guard:
            entry.users -= 1
            if entry.users == 0 and self._source_locks.get(source_url) is entry:
                self._source_locks.pop(source_url, None)

    def _cache_canonical_url(self, source_url: str, canonical_url: str) -> None:
        self._canonical_urls[source_url] = (
            canonical_url,
            self._clock() + self._config.canonical_url_cache_seconds,
        )
        self._canonical_urls.move_to_end(source_url)
        while len(self._canonical_urls) > VIDEO_CANONICAL_URL_CACHE_MAX_ENTRIES:
            self._canonical_urls.popitem(last=False)

    def _prune_canonical_urls(self) -> None:
        now = self._clock()
        expired = [
            source_url
            for source_url, (_canonical_url, expires_at) in self._canonical_urls.items()
            if expires_at <= now
        ]
        for source_url in expired:
            self._canonical_urls.pop(source_url, None)

    async def _probe_upload_group_file(
        self,
        bot,
        *,
        group_id: str,
        trace_id: str,
    ) -> str | None:
        if self._upload_supported is True:
            return None
        if self._upload_supported is False:
            return "capability_unsupported"
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._config.send_timeout_seconds):
                response = await bot.call_api(
                    "upload_group_file",
                    group_id=int(group_id),
                    file="",
                    name="",
                )
            response_category = _probe_response_category(response)
            if response_category == "capability_unsupported":
                raise _VideoSendError(response_category, retryable=False)
        except Exception as exc:
            classification = _classify_send_error(exc)
            if _is_expected_probe_validation_error(exc):
                self._upload_supported = True
                await self._record_stage(
                    platform="napcat",
                    target="onebot://upload_group_file",
                    stage="capability_probe",
                    success=True,
                    attempts=1,
                    started=started,
                    trace_id=trace_id,
                )
                return None
            if classification.category == "capability_unsupported":
                self._upload_supported = False
            await self._record_stage(
                platform="napcat",
                target="onebot://upload_group_file",
                stage="capability_probe",
                success=False,
                attempts=1,
                started=started,
                error_category=classification.category,
                trace_id=trace_id,
            )
            return classification.category
        self._upload_supported = True
        await self._record_stage(
            platform="napcat",
            target="onebot://upload_group_file",
            stage="capability_probe",
            success=True,
            attempts=1,
            started=started,
            trace_id=trace_id,
        )
        return None

    async def _upload_group_file(
        self,
        bot,
        *,
        group_id: str,
        asset: VideoAsset,
    ):
        if self._upload_supported is False:
            raise _VideoSendError("capability_unsupported", retryable=False)
        container_path = _container_file_path(
            asset.file_path,
            host_root=self._config.host_cache_path,
            container_root=self._config.container_cache_path,
        )
        try:
            return await bot.call_api(
                "upload_group_file",
                group_id=int(group_id),
                file=container_path,
                name=_upload_name(asset),
            )
        except Exception as exc:
            classification = _classify_send_error(exc)
            if classification.category == "capability_unsupported":
                self._upload_supported = False
            raise

    async def _record_stage(
        self,
        *,
        platform: str,
        target: str,
        stage: str,
        success: bool,
        attempts: int,
        started: float,
        trace_id: str,
        error_category: str | None = None,
        circuit_state: str = "closed",
    ) -> None:
        await self._health.record_attempt(
            kind="video",
            provider=platform,
            target=target,
            stage=stage,
            success=success,
            attempts=attempts,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_category=error_category,
            circuit_state=circuit_state,
            record_system_event=self._record_system_event,
            trace_id=trace_id,
        )

    async def _record_batch(
        self,
        *,
        trace_id: str,
        result: str,
        success_count: int,
        failure_count: int,
        duration_ms: int,
    ) -> None:
        if self._record_system_event is None:
            return
        await self._record_system_event(
            level="INFO" if failure_count == 0 else "WARNING",
            event="video_batch_completed",
            detail=json.dumps(
                {
                    "result": result,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            trace_id=trace_id,
        )

    async def _sleep_with_jitter(self, delay: float) -> None:
        jitter = self._jitter(0.0, self._config.backoff_jitter_seconds)
        await self._retry_sleep(delay + jitter)

    def _validate_asset_size(self, asset: VideoAsset) -> RetryExhaustedError | None:
        path = Path(asset.file_path)
        if not path.is_file():
            return RetryExhaustedError(
                stage="video_send",
                category="file_missing",
                attempts=1,
                timeout_seconds=self._config.send_timeout_seconds,
            )
        limit = self._config.qq_video_max_bytes
        if limit is not None and path.stat().st_size > limit:
            return RetryExhaustedError(
                stage="video_send",
                category="file_too_large",
                attempts=1,
                timeout_seconds=self._config.send_timeout_seconds,
            )
        return None

    async def _cleanup_asset(self, asset: VideoAsset) -> None:
        try:
            Path(asset.file_path).unlink(missing_ok=True)
        except OSError:
            pass

    async def _cleanup_source(self, source_url: str) -> None:
        cleanup = getattr(self._extractor, "cleanup", None)
        if cleanup is not None:
            await cleanup(source_url)


class _VideoSendError(RuntimeError):
    def __init__(self, category: str, *, retryable: bool) -> None:
        self.category = category
        self.retryable = retryable
        super().__init__(category)


def extract_supported_video_urls(text: str) -> list[str]:
    urls = []
    seen = set()
    for raw_url in re.findall(r"https?://[^\s<>\"']+", str(text or "")):
        url = raw_url.rstrip("，。！？!?、；;,)）]】}")
        if not _is_supported_video_url(url) or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def video_platform_label(source_url: str) -> str:
    hostname = (urlsplit(source_url).hostname or "").lower()
    return "抖音" if "douyin" in hostname else "B站"


def _is_supported_video_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ("douyin.com", "iesdouyin.com", "bilibili.com", "b23.tv")
    )


def _video_circuit_domain(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    if any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ("douyin.com", "iesdouyin.com")
    ):
        return "douyin.com"
    if any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ("bilibili.com", "b23.tv")
    ):
        return "bilibili.com"
    return "unknown"


def _classify_download_error(exc: Exception) -> RetryClassification:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return RetryClassification("network_timeout", True)
    if isinstance(exc, VideoExtractorError):
        return RetryClassification(exc.category, exc.retryable)
    if isinstance(exc, OSError):
        return RetryClassification("network_error", True)
    return RetryClassification("download_failed", True)


def _classify_send_error(exc: Exception) -> RetryClassification:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return RetryClassification("network_timeout", True)
    if isinstance(exc, _VideoSendError):
        return RetryClassification(exc.category, exc.retryable)
    if type(exc).__name__ == "ApiNotAvailable":
        return RetryClassification("capability_unsupported", False)
    text = _exception_text(exc)
    if any(
        marker in text
        for marker in ("unsupported action", "action not found", "retcode=1404")
    ):
        return RetryClassification("capability_unsupported", False)
    if any(marker in text for marker in ("too large", "file size", "文件过大")):
        return RetryClassification("file_too_large", False)
    if any(marker in text for marker in ("unauthorized", "forbidden", "token")):
        return RetryClassification("authentication", False)
    if any(marker in text for marker in ("timeout", "timed out")):
        return RetryClassification("network_timeout", True)
    return RetryClassification("send_failed", True)


def _probe_response_category(response) -> str | None:
    if not isinstance(response, Mapping):
        return None
    retcode = response.get("retcode")
    if str(retcode).strip() == "1404":
        return "capability_unsupported"
    return None


def _is_expected_probe_validation_error(exc: Exception) -> bool:
    info = getattr(exc, "info", None)
    if isinstance(info, Mapping):
        if _probe_response_category(info) == "capability_unsupported":
            return False
        status = str(info.get("status") or "").strip().lower()
        retcode = str(info.get("retcode") or "").strip()
        if status == "failed" and retcode not in {"", "0"}:
            return True
    text = _exception_text(exc)
    return any(
        marker in text
        for marker in (
            "file not found",
            "no such file",
            "file is empty",
            "invalid file",
            "missing parameter",
            "文件不存在",
            "文件为空",
            "参数",
        )
    )


def _exception_text(exc: Exception) -> str:
    parts = [str(exc)]
    info = getattr(exc, "info", None)
    if isinstance(info, Mapping):
        parts.extend(
            f"{key}={info[key]}"
            for key in ("retcode", "status", "message", "wording")
            if key in info and info[key] is not None
        )
    return " ".join(" ".join(parts).lower().split())


def _container_file_path(file_path: str, *, host_root: str, container_root: str) -> str:
    host_path = Path(file_path).resolve()
    try:
        relative = host_path.relative_to(Path(host_root).resolve())
    except ValueError as exc:
        raise _VideoSendError("invalid_cache_path", retryable=False) from exc
    container_path = PurePosixPath(container_root) / PurePosixPath(relative.as_posix())
    return f"file://{container_path}"


def _upload_name(asset: VideoAsset) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", asset.title or "视频")
    title = re.sub(r"\s+", " ", title).strip(" ._")[:80] or "视频"
    suffix = Path(asset.file_path).suffix.lower() or ".mp4"
    provider = asset.provider or video_platform_label(asset.source_url)
    return f"{provider}-{title}{suffix}"


def _format_final_result(summary: str, failures: list[str]) -> str:
    complete = "\n".join((summary, *failures))
    if len(complete) <= VIDEO_FINAL_MESSAGE_MAX_CHARS:
        return complete

    lines = [summary]
    body_limit = VIDEO_FINAL_MESSAGE_MAX_CHARS - len(_VIDEO_FINAL_TRUNCATION) - 1
    for failure in failures:
        candidate = "\n".join((*lines, failure))
        if len(candidate) > body_limit:
            break
        lines.append(failure)
    return "\n".join((*lines, _VIDEO_FINAL_TRUNCATION))


def _format_probe_failure(category: str) -> str:
    reasons = {
        "capability_unsupported": "NapCat不支持群文件上传",
        "authentication": "OneBot鉴权失败",
        "network_timeout": "NapCat上传能力探测超时",
        "send_failed": "NapCat上传能力暂不可用",
    }
    return f"视频处理未开始：{reasons.get(category, 'NapCat上传能力探测失败')}"


def _format_failure(
    platform: str,
    title: str | None,
    stage: str,
    error: RetryExhaustedError,
) -> str:
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()[:80]
    subject = platform + (f"《{clean_title}》" if clean_title else "") + "视频"
    if error.category == "network_timeout":
        return f"{subject}{stage}超时：连续{error.attempts}次网络连接未完成"
    reasons = {
        "dependency_missing": "下载器未安装",
        "unsupported": "链接或内容不受支持",
        "access_denied": "内容需要登录、付费或访问权限",
        "file_too_large": "文件超过QQ上传上限",
        "file_missing": "本地视频文件不存在",
        "invalid_cache_path": "视频不在NapCat共享缓存目录",
        "capability_unsupported": "NapCat不支持群文件上传",
        "authentication": "OneBot鉴权失败",
        "network_error": f"网络连接连续{error.attempts}次失败",
        "domain_circuit_open": "该视频域名暂时熔断，请稍后重试",
        "canonical_url_missing": "短链接规范地址解析失败",
        "send_failed": f"QQ上传接口连续{error.attempts}次失败",
        "download_failed": f"视频源连续{error.attempts}次不可用",
        "download_output_missing": "下载结果文件缺失",
        "download_metadata_missing": "下载结果元数据缺失",
    }
    return f"{subject}{stage}失败：{reasons.get(error.category, '未知错误')}"
