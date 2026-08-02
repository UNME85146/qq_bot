from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.models import RetryConfig


T = TypeVar("T")
RetryOperation = Callable[[float], Awaitable[T]]
RetryClassifier = Callable[[Exception], "RetryClassification"]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryClassification:
    category: str
    retryable: bool


@dataclass(frozen=True)
class RetryResult:
    value: T
    attempts: int


class RetryExhaustedError(RuntimeError):
    def __init__(
        self,
        *,
        stage: str,
        category: str,
        attempts: int,
        timeout_seconds: float,
    ) -> None:
        self.stage = stage
        self.category = category
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"retry exhausted: stage={stage}; category={category}; "
            f"attempts={attempts}; timeout_seconds={timeout_seconds}"
        )


async def run_with_retry(
    operation: RetryOperation[T],
    *,
    stage: str,
    base_timeout_seconds: float,
    classify: RetryClassifier,
    policy: RetryConfig | None = None,
    sleep: Sleep = asyncio.sleep,
) -> RetryResult[T]:
    if base_timeout_seconds <= 0:
        raise ValueError("base_timeout_seconds must be positive")

    active_policy = policy or RetryConfig()
    _validate_policy(active_policy)

    for attempt, multiplier in enumerate(active_policy.timeout_multipliers, start=1):
        timeout_seconds = float(base_timeout_seconds) * multiplier
        try:
            value = await asyncio.wait_for(
                operation(timeout_seconds),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            classification = classify(exc)
            should_retry = classification.retryable and attempt < active_policy.max_attempts
            if not should_retry:
                raise RetryExhaustedError(
                    stage=stage,
                    category=classification.category,
                    attempts=attempt,
                    timeout_seconds=timeout_seconds,
                ) from exc
            await sleep(active_policy.backoff_seconds[attempt - 1])
        else:
            return RetryResult(value=value, attempts=attempt)

    raise AssertionError("validated retry policy must return or raise")


def _validate_policy(policy: RetryConfig) -> None:
    if policy.max_attempts <= 0:
        raise ValueError("retry max_attempts must be positive")
    if len(policy.timeout_multipliers) != policy.max_attempts:
        raise ValueError("retry timeout_multipliers must match max_attempts")
    if len(policy.backoff_seconds) != policy.max_attempts - 1:
        raise ValueError("retry backoff_seconds length must be max_attempts minus one")
    if any(multiplier <= 0 for multiplier in policy.timeout_multipliers):
        raise ValueError("retry timeout_multipliers must be positive")
    if any(delay < 0 for delay in policy.backoff_seconds):
        raise ValueError("retry backoff_seconds must be non-negative")
