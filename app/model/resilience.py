from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.model.llm_client import LlmClient
from app.models import GeneratedReply, LimitsConfig


@dataclass(frozen=True)
class ModelFailureDetail:
    category: str
    detail: str
    retryable: bool
    status_code: int | None = None


@dataclass(frozen=True)
class ModelCallResult:
    reply: GeneratedReply
    model_called: bool
    failure_reason: str | None = None
    system_events: tuple[str, ...] = ()


class ModelResilienceService:
    def __init__(
        self,
        *,
        model_client: LlmClient,
        limits: LimitsConfig,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        self._model_client = model_client
        self._limits = limits
        self._retry_delay_seconds = retry_delay_seconds
        self._consecutive_failures = 0
        self._breaker_open_until: float | None = None

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        scope_type: str,
        now: float | None = None,
    ) -> ModelCallResult:
        current = time.monotonic() if now is None else now
        if self._is_breaker_open(current):
            return ModelCallResult(
                reply=_fallback_reply(scope_type, finish_reason="model_breaker_open"),
                model_called=False,
                failure_reason="model_breaker_open",
                system_events=("model_breaker_open: call skipped",),
            )

        events: list[str] = []
        attempts = 0
        last_failure: ModelFailureDetail | None = None
        while attempts < 2:
            attempts += 1
            started_at = time.monotonic()
            try:
                reply = await self._model_client.generate(messages)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                failure = classify_model_exception(exc)
                last_failure = failure
                will_retry = failure.retryable and attempts < 2
                breaker_open = self._record_failure(time.monotonic())
                events.append(
                    "model_failure "
                    f"category={failure.category}; "
                    f"status={failure.status_code}; "
                    f"attempt={attempts}; "
                    f"retry={str(will_retry).lower()}; "
                    f"breaker_open={str(breaker_open).lower()}; "
                    f"elapsed_ms={elapsed_ms}; "
                    f"detail={failure.detail}"
                )
                if will_retry:
                    await asyncio.sleep(self._retry_delay_seconds)
                    continue
                return ModelCallResult(
                    reply=_fallback_reply(scope_type, finish_reason=failure.category),
                    model_called=True,
                    failure_reason=failure.category,
                    system_events=tuple(events),
                )
            else:
                self._record_success()
                return ModelCallResult(
                    reply=reply,
                    model_called=True,
                    system_events=tuple(events),
                )

        failure_reason = last_failure.category if last_failure else "unknown_model_error"
        return ModelCallResult(
            reply=_fallback_reply(scope_type, finish_reason=failure_reason),
            model_called=True,
            failure_reason=failure_reason,
            system_events=tuple(events),
        )

    def _is_breaker_open(self, now: float) -> bool:
        return self._breaker_open_until is not None and now < self._breaker_open_until

    def _record_failure(self, now: float) -> bool:
        self._consecutive_failures += 1
        threshold = self._limits.model_failure_break_count
        if threshold > 0 and self._consecutive_failures >= threshold:
            self._breaker_open_until = now + self._limits.model_failure_break_seconds
            return True
        return False

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_open_until = None


def classify_model_exception(exc: Exception) -> ModelFailureDetail:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = _redact_sensitive_text(" ".join(exc.response.text.split()))[:180]
        if status in {401, 403}:
            return ModelFailureDetail("auth_error", body, retryable=False, status_code=status)
        if status == 404:
            return ModelFailureDetail(
                "model_or_endpoint_not_found",
                body,
                retryable=False,
                status_code=status,
            )
        if status == 429:
            return ModelFailureDetail("rate_limited", body, retryable=True, status_code=status)
        if 500 <= status <= 599:
            return ModelFailureDetail("provider_error", body, retryable=True, status_code=status)
        return ModelFailureDetail("unknown_model_error", body, retryable=False, status_code=status)
    if isinstance(exc, httpx.TimeoutException):
        return ModelFailureDetail("timeout", "model request timed out", retryable=True)
    if isinstance(exc, httpx.RequestError):
        return ModelFailureDetail("network_error", exc.__class__.__name__, retryable=True)
    return ModelFailureDetail(
        "unknown_model_error",
        _redact_sensitive_text(str(exc))[:180],
        retryable=False,
    )


def _fallback_reply(scope_type: str, *, finish_reason: str) -> GeneratedReply:
    text = "卡了，等下再说。" if scope_type == "group" else "我刚刚有点卡，等下再说这个。"
    return GeneratedReply(
        text=text,
        raw_model_text=text,
        model_name="fallback",
        finish_reason=finish_reason,
    )


def _redact_sensitive_text(text: str) -> str:
    redacted = text
    replacements = (
        (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer [redacted]"),
        (re.compile(r"sk-[A-Za-z0-9_-]+"), "sk-[redacted]"),
        (re.compile(r"fe_oa_[A-Za-z0-9]+", re.IGNORECASE), "fe_oa_[redacted]"),
        (
            re.compile(r"(api[_-]?key|token|authorization)\s*[:=]\s*\S+", re.IGNORECASE),
            r"\1=[redacted]",
        ),
    )
    for pattern, replacement in replacements:
        redacted = pattern.sub(replacement, redacted)
    for marker in ("QQ_BOT_MODEL_API_KEY", "QQ_BOT_ONEBOT_TOKEN"):
        redacted = redacted.replace(marker, "[redacted_env]")
    return redacted
