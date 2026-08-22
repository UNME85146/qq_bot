from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Callable, Protocol, runtime_checkable

import httpx

from app.features.provider_health import (
    ProviderHealthRegistry,
    classify_provider_error,
    default_provider_health_registry,
)
from app.models import GeneratedReply, ModelConfig


class LlmClient(Protocol):
    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        ...


@runtime_checkable
class ModelProbeClient(LlmClient, Protocol):
    async def generate_probe(self) -> GeneratedReply:
        ...


class ModelEndpointSelectionError(RuntimeError):
    pass


class MockModelClient:
    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        last_user_message = next(
            (
                _content_to_text(message.get("content", ""))
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        text = "收到啦，我在。"
        if "真人" in last_user_message or "AI" in last_user_message:
            text = "小黄"
        return GeneratedReply(
            text=text,
            raw_model_text=text,
            model_name="mock",
            finish_reason="stop",
        )


class OpenAICompatibleClient:
    def __init__(self, config: ModelConfig) -> None:
        if not config.api_key:
            raise ValueError("Model API key is required for OpenAICompatibleClient.")
        self._config = config

    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        return await self.generate_with_options(
            messages,
            reasoning_effort=self._config.reasoning_effort,
        )

    async def generate_with_options(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> GeneratedReply:
        payload = {
            "model": self._config.name,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": (
                self._config.max_tokens if max_tokens is None else max_tokens
            ),
        }
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        timeout = httpx.Timeout(self._config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._config.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        choice = data["choices"][0]
        text = choice["message"]["content"].strip()
        usage = data.get("usage", {})
        return GeneratedReply(
            text=text,
            raw_model_text=text,
            model_name=self._config.name,
            finish_reason=choice.get("finish_reason", "unknown"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    async def generate_probe(self) -> GeneratedReply:
        return await self.generate_with_options(
            [{"role": "user", "content": "只回复 ok"}],
            max_tokens=1,
            reasoning_effort=self._config.reasoning_effort,
        )


class LatencyAwareModelClient:
    """Keeps one active model endpoint, refreshed by authenticated probes."""

    def __init__(
        self,
        *,
        clients: dict[str, LlmClient],
        interval_seconds: float,
        health_registry: ProviderHealthRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(clients) < 2:
            raise ValueError("latency-aware client needs at least two endpoints")
        self._clients = dict(clients)
        self._interval_seconds = interval_seconds
        self._health_registry = health_registry or default_provider_health_registry()
        self._clock = clock
        self._active_endpoint: str | None = None
        self._last_probe_at: float | None = None
        self._probe_lock = asyncio.Lock()
        self._last_probe_results: dict[str, int | None] = {}

    @property
    def active_endpoint(self) -> str | None:
        return self._active_endpoint

    def status(self) -> dict[str, object]:
        return {
            "active_endpoint": self._active_endpoint,
            "last_probe_at": self._last_probe_at,
            "last_probe_results_ms": dict(self._last_probe_results),
        }

    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        return await self.generate_with_options(messages)

    async def generate_with_options(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> GeneratedReply:
        await self.refresh_if_due()
        endpoint = self._active_endpoint
        if endpoint is None:
            raise ModelEndpointSelectionError("no healthy model endpoint")
        started_at = self._clock()
        try:
            return await _call_client(
                self._clients[endpoint],
                messages,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
        except Exception as first_error:
            await self._record_endpoint_attempt(
                endpoint,
                stage="request",
                success=False,
                duration_ms=int((self._clock() - started_at) * 1000),
                error_category=classify_provider_error(first_error),
            )
            for alternate in self._alternate_endpoints(endpoint):
                alternate_started_at = self._clock()
                try:
                    reply = await _call_client(
                        self._clients[alternate],
                        messages,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                except Exception as alternate_error:
                    await self._record_endpoint_attempt(
                        alternate,
                        stage="request_failover",
                        success=False,
                        duration_ms=int(
                            (self._clock() - alternate_started_at) * 1000
                        ),
                        error_category=classify_provider_error(alternate_error),
                    )
                    continue
                self._active_endpoint = alternate
                await self._record_endpoint_attempt(
                    alternate,
                    stage="request_failover",
                    success=True,
                    duration_ms=int(
                        (self._clock() - alternate_started_at) * 1000
                    ),
                    error_category=None,
                )
                return reply
            raise first_error

    async def refresh_if_due(self, *, force: bool = False) -> bool:
        current = self._clock()
        if (
            not force
            and self._last_probe_at is not None
            and current - self._last_probe_at < self._interval_seconds
        ):
            return False
        async with self._probe_lock:
            current = self._clock()
            if (
                not force
                and self._last_probe_at is not None
                and current - self._last_probe_at < self._interval_seconds
            ):
                return False
            await self._refresh_locked(current)
            return True

    async def _refresh_locked(self, started_at: float) -> None:
        results = await asyncio.gather(
            *(self._probe_endpoint(endpoint) for endpoint in self._clients),
            return_exceptions=False,
        )
        healthy = [(endpoint, duration_ms) for endpoint, duration_ms in results if duration_ms is not None]
        self._last_probe_at = started_at
        self._last_probe_results = dict(results)
        if healthy:
            best_duration = min(duration for _, duration in healthy)
            best_endpoints = {
                endpoint for endpoint, duration in healthy if duration == best_duration
            }
            if self._active_endpoint not in best_endpoints:
                self._active_endpoint = next(
                    endpoint for endpoint in self._clients if endpoint in best_endpoints
                )
            return
        if self._active_endpoint is not None:
            return
        raise ModelEndpointSelectionError("all model endpoint probes failed")

    async def _probe_endpoint(self, endpoint: str) -> tuple[str, int | None]:
        started_at = self._clock()
        try:
            client = self._clients[endpoint]
            if isinstance(client, ModelProbeClient):
                await client.generate_probe()
            else:
                await client.generate([{"role": "user", "content": "只回复 ok"}])
        except Exception as exc:
            elapsed_ms = int((self._clock() - started_at) * 1000)
            await self._record_endpoint_attempt(
                endpoint,
                stage="latency_probe",
                success=False,
                duration_ms=elapsed_ms,
                error_category=classify_provider_error(exc),
            )
            return endpoint, None
        elapsed_ms = int((self._clock() - started_at) * 1000)
        await self._record_endpoint_attempt(
            endpoint,
            stage="latency_probe",
            success=True,
            duration_ms=elapsed_ms,
            error_category=None,
        )
        return endpoint, elapsed_ms

    async def _record_endpoint_attempt(
        self,
        endpoint: str,
        *,
        stage: str,
        success: bool,
        duration_ms: int,
        error_category: str | None,
    ) -> None:
        try:
            await self._health_registry.record_attempt(
                kind="model_endpoint",
                provider="openai_compatible",
                target=endpoint,
                stage=stage,
                success=success,
                attempts=1,
                duration_ms=duration_ms,
                error_category=error_category,
            )
        except Exception:
            # Telemetry must not prevent a model response or endpoint failover.
            return

    def _alternate_endpoints(self, endpoint: str):
        return (candidate for candidate in self._clients if candidate != endpoint)


_MODEL_CLIENTS: dict[ModelConfig, LlmClient] = {}
_MODEL_PROBE_TASKS: dict[ModelConfig, asyncio.Task[None]] = {}


async def start_model_endpoint_probe_worker(config: ModelConfig) -> None:
    client = create_model_client(config)
    if not isinstance(client, LatencyAwareModelClient):
        return
    existing = _MODEL_PROBE_TASKS.get(config)
    if existing is not None and not existing.done():
        return
    try:
        await client.refresh_if_due(force=True)
    except ModelEndpointSelectionError:
        # Startup must remain available while the periodic worker keeps probing.
        pass
    _MODEL_PROBE_TASKS[config] = asyncio.create_task(
        _model_endpoint_probe_loop(client, config.endpoint_probe_interval_seconds),
        name="model-endpoint-probe",
    )


async def stop_model_endpoint_probe_workers() -> None:
    tasks = tuple(_MODEL_PROBE_TASKS.values())
    _MODEL_PROBE_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _model_endpoint_probe_loop(
    client: LatencyAwareModelClient,
    interval_seconds: float,
) -> None:
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await client.refresh_if_due(force=True)
            except Exception:
                # Endpoint failures are recorded in the provider status JSON.
                continue
    except asyncio.CancelledError:
        raise


def create_model_client(config: ModelConfig) -> LlmClient:
    if config.use_mock:
        return MockModelClient()
    cached = _MODEL_CLIENTS.get(config)
    if cached is not None:
        return cached
    candidates = config.base_url_candidates
    if not candidates:
        client: LlmClient = OpenAICompatibleClient(config)
    else:
        client = LatencyAwareModelClient(
            clients={
                endpoint: OpenAICompatibleClient(replace(config, base_url=endpoint))
                for endpoint in candidates
            },
            interval_seconds=config.endpoint_probe_interval_seconds,
        )
    _MODEL_CLIENTS[config] = client
    return client


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)


async def _call_client(
    client: LlmClient,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int | None,
    reasoning_effort: str | None,
) -> GeneratedReply:
    if max_tokens is None and reasoning_effort is None:
        return await client.generate(messages)
    generate_with_options = getattr(client, "generate_with_options", None)
    if callable(generate_with_options):
        return await generate_with_options(
            messages,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
    return await client.generate(messages)
