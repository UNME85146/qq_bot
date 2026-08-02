from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.features.contracts import ImageAsset
from app.features.image_provider import ImageProviderError
from app.models import ImageGenerationConfig, NormalizedMessage, RetryConfig
from app.retry import RetryClassification, RetryExhaustedError, run_with_retry


@dataclass(frozen=True)
class ImageGenerationResult:
    asset: ImageAsset
    source_asset: ImageAsset | None = None

    @property
    def file_path(self) -> str:
        return self.asset.file_path


@dataclass(frozen=True)
class ImageCommand:
    prompt: str
    edit: bool


@dataclass
class _RecentImage:
    asset: ImageAsset
    expires_at: float
    expiry_task: asyncio.Task | None = None


class ImageGenerationService:
    def __init__(
        self,
        config: ImageGenerationConfig,
        *,
        provider,
        retry_policy: RetryConfig | None = None,
        record_system_event=None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        expiry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._provider = provider
        self._retry_policy = retry_policy or RetryConfig()
        self._record_system_event = record_system_event
        self._retry_sleep = retry_sleep
        self._expiry_sleep = expiry_sleep
        self._now = now
        self._recent: dict[str, _RecentImage] = {}
        self._expired_keys: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._failures: dict[str, tuple[str, str, int]] = {}

    async def execute(
        self,
        message: NormalizedMessage,
        prompt: str,
        *,
        edit: bool,
        send: Callable[[str], Awaitable[object]],
    ) -> object | None:
        key = _recent_key(message)
        async with self._locks.setdefault(key, asyncio.Lock()):
            result = await self.generate(message, prompt, edit=edit)
            if result is None:
                return None
            return await self.send_and_retain(
                message,
                result,
                lambda: send(result.file_path),
            )

    async def generate(
        self,
        message: NormalizedMessage,
        prompt: str,
        *,
        edit: bool = False,
    ) -> ImageGenerationResult | None:
        text = str(prompt or "").strip()
        if not text:
            self._failures[message.trace_id] = ("edit" if edit else "generate", "empty_prompt", 1)
            return None
        if not self._config.enabled or self._provider is None:
            self._failures[message.trace_id] = ("edit" if edit else "generate", "unconfigured", 0)
            return None

        key = _recent_key(message)
        await self._drop_if_expired(key)
        source = self._recent.get(key) if edit else None
        if edit and source is None:
            category = "edit_window_expired" if key in self._expired_keys else "no_recent_image"
            self._expired_keys.discard(key)
            self._failures[message.trace_id] = ("edit", category, 0)
            return None
        if source is not None and not Path(source.asset.file_path).is_file():
            await self._remove_recent(key, mark_expired=False)
            self._failures[message.trace_id] = ("edit", "no_recent_image", 0)
            return None

        stage = "edit" if edit else "generate"
        try:
            if source is None:
                result = await run_with_retry(
                    lambda timeout: self._provider.generate(
                        text,
                        timeout_seconds=timeout,
                        request_id=message.trace_id,
                    ),
                    stage="image_generate",
                    base_timeout_seconds=self._config.timeout_seconds,
                    policy=self._retry_policy,
                    classify=_classify_provider_error,
                    sleep=self._retry_sleep,
                )
            else:
                result = await run_with_retry(
                    lambda timeout: self._provider.edit(
                        source.asset.file_path,
                        text,
                        timeout_seconds=timeout,
                        request_id=message.trace_id,
                    ),
                    stage="image_edit",
                    base_timeout_seconds=self._config.timeout_seconds,
                    policy=self._retry_policy,
                    classify=_classify_provider_error,
                    sleep=self._retry_sleep,
                )
        except RetryExhaustedError as exc:
            self._failures[message.trace_id] = (stage, exc.category, exc.attempts)
            if edit:
                await self._remove_recent(key, mark_expired=False)
            await self._record_failure(message, stage, exc.category, exc.attempts)
            return None

        self._failures.pop(message.trace_id, None)
        return ImageGenerationResult(
            asset=result.value,
            source_asset=source.asset if source is not None else None,
        )

    async def send_and_retain(
        self,
        message: NormalizedMessage,
        result: ImageGenerationResult,
        send: Callable[[], Awaitable[object]],
    ) -> object | None:
        key = _recent_key(message)
        try:
            sent = await run_with_retry(
                lambda _timeout: send(),
                stage="image_send",
                base_timeout_seconds=self._config.send_timeout_seconds,
                policy=self._retry_policy,
                classify=_classify_send_error,
                sleep=self._retry_sleep,
            )
        except RetryExhaustedError as exc:
            self._failures[message.trace_id] = ("send", exc.category, exc.attempts)
            await self._cleanup_asset(result.asset)
            await self._remove_recent(key, mark_expired=False)
            await self._record_failure(message, "send", exc.category, exc.attempts)
            return None

        await self._remove_recent(key, mark_expired=False)
        recent = _RecentImage(
            asset=result.asset,
            expires_at=self._now() + self._config.edit_window_seconds,
        )
        self._recent[key] = recent
        self._expired_keys.discard(key)
        recent.expiry_task = asyncio.create_task(self._expire_after_window(key, result.asset))
        self._failures.pop(message.trace_id, None)
        return sent.value

    def failure_message(self, trace_id: str) -> str | None:
        failure = self._failures.get(trace_id)
        if failure is None:
            return None
        stage, category, attempts = failure
        if category == "unconfigured":
            return "当前配置不支持生图：请管理员配置 imageGeneration 模型和 API Key"
        if category == "no_recent_image":
            return "没有可修改的最近图片，请先使用 #画图"
        if category == "edit_window_expired":
            return "最近图片已超过3分钟并清除，请重新使用 #画图"
        label = {"generate": "图片生成", "edit": "图片修改", "send": "图片发送"}[stage]
        if category == "timeout":
            return f"{label}超时：连续{attempts}次未完成"
        reasons = {
            "empty_prompt": "没有图片描述",
            "authentication": "API 鉴权失败",
            "capability_unsupported": "接口不支持图片生成或修改",
            "safety_rejected": "请求未通过安全检查",
            "model_or_parameter_unsupported": "模型或参数不支持",
            "rate_limited": f"接口连续{attempts}次限流",
            "provider_unavailable": f"服务连续{attempts}次不可用",
            "request_failed": "接口拒绝请求",
            "invalid_response": "接口返回的图片数据无效",
            "empty_image": "接口返回空图片",
            "cache_write_failed": "本地图片缓存写入失败",
            "source_image_missing": "原图片缓存不存在",
            "send_failed": f"QQ连续{attempts}次发送失败",
        }
        return f"{label}失败：{reasons.get(category, '未知错误')}"

    def failure_category(self, trace_id: str) -> str | None:
        failure = self._failures.get(trace_id)
        return failure[1] if failure is not None else None

    async def close(self) -> None:
        for key in list(self._recent):
            await self._remove_recent(key, mark_expired=False)
        self._expired_keys.clear()

    async def _drop_if_expired(self, key: str) -> None:
        recent = self._recent.get(key)
        if recent is not None and self._now() >= recent.expires_at:
            await self._remove_recent(key, mark_expired=True)

    async def _expire_after_window(self, key: str, asset: ImageAsset) -> None:
        try:
            await self._expiry_sleep(max(0, self._config.edit_window_seconds))
        except asyncio.CancelledError:
            return
        recent = self._recent.get(key)
        if recent is not None and recent.asset.file_path == asset.file_path:
            await self._remove_recent(key, mark_expired=True)

    async def _remove_recent(self, key: str, *, mark_expired: bool) -> None:
        recent = self._recent.pop(key, None)
        if recent is None:
            return
        if mark_expired:
            self._expired_keys.add(key)
        task = recent.expiry_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self._cleanup_asset(recent.asset)

    async def _cleanup_asset(self, asset: ImageAsset) -> None:
        cleanup = getattr(self._provider, "cleanup", None)
        if cleanup is not None:
            await cleanup(asset)
        else:
            Path(asset.file_path).unlink(missing_ok=True)

    async def _record_failure(
        self,
        message: NormalizedMessage,
        stage: str,
        category: str,
        attempts: int,
    ) -> None:
        if self._record_system_event is None:
            return
        await self._record_system_event(
            level="ERROR",
            event="image_operation_failed",
            detail=f"scope={message.scope_type}; stage={stage}; category={category}; attempts={attempts}",
            trace_id=message.trace_id,
        )


def _recent_key(message: NormalizedMessage) -> str:
    scope_id = message.group_id or message.scope_id
    return f"{message.scope_type}:{scope_id}:{message.user_id}"


def parse_image_command(text: str) -> ImageCommand | None:
    normalized = str(text or "").strip()
    for prefix, edit in (("#画图", False), ("#生图", False), ("#改图", True)):
        if normalized == prefix:
            return ImageCommand(prompt="", edit=edit)
        if not normalized.startswith(prefix):
            continue
        separator = normalized[len(prefix) : len(prefix) + 1]
        if separator and (separator.isspace() or separator in {":", "："}):
            prompt = re.sub(r"^[\s:：]+", "", normalized[len(prefix) :])
            return ImageCommand(prompt=prompt, edit=edit)
    return None


def _classify_provider_error(exc: Exception) -> RetryClassification:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return RetryClassification("timeout", True)
    if isinstance(exc, ImageProviderError):
        return RetryClassification(exc.category, exc.retryable)
    return RetryClassification("provider_unavailable", True)


def _classify_send_error(exc: Exception) -> RetryClassification:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return RetryClassification("timeout", True)
    return RetryClassification("send_failed", True)
