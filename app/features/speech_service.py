from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.features.contracts import SpeechAsset
from app.features.speech_provider import SpeechProviderError, speech_endpoint_path
from app.features.tts_service import prepare_tts_speech_text
from app.models import GeneratedReply, NormalizedMessage, RetryConfig, SpeechConfig
from app.retry import RetryClassification, RetryExhaustedError, run_with_retry


@dataclass(frozen=True)
class SpeechGenerationResult:
    audio_path: str
    format: str
    voice_profile_id: str
    transcript: str | None = None
    provider_model: str | None = None


@dataclass(frozen=True)
class SpeechDeliveryOutcome:
    handled: bool
    delivery_status: str
    message_id: str | None = None

    @property
    def sent(self) -> bool:
        return self.delivery_status == "sent"

    def __bool__(self) -> bool:
        return self.handled


class SpeechService:
    def __init__(
        self,
        config: SpeechConfig,
        *,
        provider,
        retry_policy: RetryConfig | None = None,
        record_system_event=None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._provider = provider
        self._retry_policy = retry_policy or RetryConfig()
        self._record_system_event = record_system_event
        self._retry_sleep = retry_sleep
        self._now = now
        self._last_attempt_at: dict[str, float] = {}
        self._failures: dict[str, tuple[str, str, int]] = {}

    async def generate_for_reply(
        self,
        message: NormalizedMessage,
        reply: GeneratedReply,
    ) -> SpeechGenerationResult | None:
        return await self.generate_for_text(message, reply.text)

    async def generate_for_text(
        self,
        message: NormalizedMessage,
        text: str,
        *,
        voice_profile_id: str | None = None,
        exact_short: bool = False,
        ignore_cooldown: bool = False,
        segment_max_chars: int | None = None,
    ) -> SpeechGenerationResult | None:
        del voice_profile_id, segment_max_chars
        speech_text = prepare_tts_speech_text(text, exact_short=exact_short)
        if not speech_text:
            self._failures[message.trace_id] = ("generate", "empty_text", 1)
            return None
        if not self._config.enabled or self._provider is None:
            self._failures[message.trace_id] = ("generate", "unconfigured", 0)
            return None
        if len(speech_text) > self._config.max_chars:
            self._failures[message.trace_id] = ("generate", "text_too_long", 1)
            return None
        cooldown = (
            self._config.group_cooldown_seconds
            if message.scope_type == "group"
            else self._config.private_cooldown_seconds
        )
        key = _cooldown_key(message)
        now = self._now()
        previous = self._last_attempt_at.get(key)
        if not ignore_cooldown and previous is not None and now - previous < cooldown:
            self._failures[message.trace_id] = ("generate", "cooldown", 1)
            return None
        self._last_attempt_at[key] = now
        try:
            result = await run_with_retry(
                lambda timeout: self._provider.synthesize(
                    speech_text,
                    timeout_seconds=timeout,
                    request_id=message.trace_id,
                ),
                stage="speech_generate",
                base_timeout_seconds=self._config.timeout_seconds,
                policy=self._retry_policy,
                classify=_classify_generation_error,
                sleep=self._retry_sleep,
            )
        except RetryExhaustedError as exc:
            self._failures[message.trace_id] = (
                "generate",
                exc.category,
                exc.attempts,
            )
            await self._record_failure(message, exc.category, exc.attempts)
            return None
        self._failures.pop(message.trace_id, None)
        return SpeechGenerationResult(
            audio_path=result.value.file_path,
            format=result.value.format,
            voice_profile_id=self._config.voice,
            transcript=result.value.transcript,
            provider_model=result.value.provider_model,
        )

    async def send_and_cleanup(
        self,
        message: NormalizedMessage,
        result: SpeechGenerationResult,
        send: Callable[[], Awaitable[object]],
    ) -> object | None:
        asset = SpeechAsset(file_path=result.audio_path, format=result.format)
        try:
            try:
                sent = await asyncio.wait_for(
                    send(),
                    timeout=self._config.send_timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError):
                self._failures[message.trace_id] = (
                    "send",
                    "delivery_unknown",
                    1,
                )
                await self._record_failure(message, "delivery_unknown", 1)
                return None
            except Exception as exc:
                classification = _classify_send_error(exc)
                self._failures[message.trace_id] = (
                    "send",
                    classification.category,
                    1,
                )
                await self._record_failure(message, classification.category, 1)
                return None
            self._failures.pop(message.trace_id, None)
            return sent
        finally:
            cleanup = getattr(self._provider, "cleanup", None)
            if cleanup is not None:
                await cleanup(asset)

    def failure_message(self, trace_id: str) -> str | None:
        failure = self._failures.get(trace_id)
        if failure is None:
            return None
        stage, category, attempts = failure
        if category == "unconfigured":
            return (
                "当前配置不支持语音：请管理员配置远程语音模型、接口模式和 API Key"
            )
        label = "语音生成" if stage == "generate" else "语音发送"
        if category == "timeout":
            return f"{label}超时：连续{attempts}次未完成"
        reasons = {
            "authentication": "API 鉴权失败",
            "capability_unsupported": (
                f"接口不支持 {speech_endpoint_path(self._config)}"
            ),
            "model_or_parameter_unsupported": "模型或音色不支持语音生成",
            "rate_limited": f"接口连续{attempts}次限流",
            "provider_unavailable": f"服务连续{attempts}次不可用",
            "request_failed": "接口拒绝请求",
            "empty_audio": "接口返回空音频",
            "invalid_audio_response": "接口返回了无效音频结构",
            "invalid_audio_format": "接口返回的音频格式不正确",
            "transcript_mismatch": "语音内容与待朗读文本不一致",
            "audio_too_large": "接口返回的音频超过大小限制",
            "text_too_long": "文本超过语音接口长度限制",
            "empty_text": "没有可朗读的文本",
            "cooldown": "语音请求过于频繁",
            "file_too_large": "QQ拒绝文件，文件大小超过限制",
            "send_failed": f"QQ连续{attempts}次发送失败",
            "delivery_unknown": "QQ投递状态未知，已停止重发以避免重复语音",
        }
        return f"{label}失败：{reasons.get(category, '未知错误')}"

    def failure_category(self, trace_id: str) -> str | None:
        failure = self._failures.get(trace_id)
        return failure[1] if failure is not None else None

    async def _record_failure(
        self,
        message: NormalizedMessage,
        category: str,
        attempts: int,
    ) -> None:
        if self._record_system_event is None:
            return
        await self._record_system_event(
            level="ERROR",
            event="speech_operation_failed",
            detail=(
                f"scope={message.scope_type}; category={category}; attempts={attempts}"
            ),
            trace_id=message.trace_id,
        )


def _classify_generation_error(exc: Exception) -> RetryClassification:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return RetryClassification("timeout", True)
    if isinstance(exc, SpeechProviderError):
        return RetryClassification(exc.category, exc.retryable)
    return RetryClassification("provider_unavailable", True)


def _classify_send_error(exc: Exception) -> RetryClassification:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return RetryClassification("timeout", True)
    text = " ".join(str(exc).lower().split())
    if any(marker in text for marker in ("too large", "file size", "文件过大")):
        return RetryClassification("file_too_large", False)
    return RetryClassification("send_failed", True)


def _cooldown_key(message: NormalizedMessage) -> str:
    if message.scope_type == "group":
        return f"group:{message.group_id or message.scope_id}"
    return f"private:{message.user_id}"
