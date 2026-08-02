from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.features.contracts import MarketDataProvider, MarketQuote
from app.features.provider_health import (
    CircuitBreaker,
    ProviderHealthRegistry,
    SystemEventRecorder,
    classify_provider_error,
    default_provider_health_registry,
)
from app.models import MarketProviderConfig, MarketsConfig


class MarketProviderUnavailableError(RuntimeError):
    def __init__(self, message: str, *, category: str = "provider_error") -> None:
        self.category = category
        super().__init__(message)


class YFinanceMarketProvider:
    async def quote(self, market: str, symbol: str) -> MarketQuote:
        return await _quote_in_isolated_process("yfinance", market, symbol)

    @staticmethod
    def _quote_sync(market: str, symbol: str) -> MarketQuote:
        import yfinance as yf

        history = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
        closes = [float(value) for value in history["Close"].dropna().tolist()]
        if not closes:
            raise RuntimeError("empty yfinance quote")
        price = closes[-1]
        previous = closes[-2] if len(closes) >= 2 else None
        change = ((price - previous) / previous * 100) if previous else None
        return MarketQuote(
            market=market,
            symbol=symbol,
            price=price,
            previous_close=previous,
            change_percent=change,
            source="Yahoo Finance via yfinance",
            observed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            delayed=True,
        )


class AkShareMarketProvider:
    async def quote(self, market: str, symbol: str) -> MarketQuote:
        return await _quote_in_isolated_process("akshare", market, symbol)

    @staticmethod
    def _quote_sync(market: str, symbol: str) -> MarketQuote:
        import akshare as ak

        frame = ak.stock_zh_a_spot_em()
        code = symbol.split(".", 1)[0]
        matched = frame.loc[frame["代码"].astype(str).str.zfill(6) == code]
        if matched.empty:
            raise RuntimeError("stock symbol was not found")
        row = matched.iloc[0]
        price = float(row["最新价"])
        previous = float(row["昨收"]) if "昨收" in row and row["昨收"] else None
        raw_change = row["涨跌幅"] if "涨跌幅" in row else None
        change = float(raw_change) if raw_change is not None else None
        return MarketQuote(
            market=market,
            symbol=symbol,
            price=price,
            previous_close=previous,
            change_percent=change,
            source="东方财富 via AkShare",
            observed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            delayed=True,
        )


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _quote_in_isolated_process(
    provider: str,
    market: str,
    symbol: str,
) -> MarketQuote:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.features.market_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=_PROJECT_ROOT,
    )
    request = json.dumps(
        {"provider": provider, "market": market, "symbol": symbol},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        stdout, _stderr = await process.communicate(request)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    response = _last_worker_payload(stdout)
    if process.returncode != 0 or not response.get("ok"):
        category = str(response.get("category") or "provider_error")
        raise MarketProviderUnavailableError(category, category=category)
    quote = response.get("quote")
    if not isinstance(quote, dict):
        raise MarketProviderUnavailableError(
            "invalid_response",
            category="invalid_response",
        )
    try:
        return MarketQuote(**quote)
    except (TypeError, ValueError) as exc:
        raise MarketProviderUnavailableError(
            "invalid_response",
            category="invalid_response",
        ) from exc


def _last_worker_payload(stdout: bytes) -> dict[str, Any]:
    lines = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line]
    if not lines:
        raise MarketProviderUnavailableError(
            "invalid_response",
            category="invalid_response",
        )
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise MarketProviderUnavailableError(
            "invalid_response",
            category="invalid_response",
        ) from exc
    if not isinstance(payload, dict):
        raise MarketProviderUnavailableError(
            "invalid_response",
            category="invalid_response",
        )
    return payload


class SinaMarketProvider:
    def __init__(
        self,
        base_url: str = "https://hq.sinajs.cn",
        *,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def quote(self, market: str, symbol: str) -> MarketQuote:
        request_symbol = _sina_symbol(symbol)
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
            headers={
                "User-Agent": "Mozilla/5.0 QQBotMarket/1.0",
                "Referer": "https://finance.sina.com.cn/",
            },
        ) as client:
            response = await client.get(
                f"{self._base_url}/list={request_symbol}"
            )
            response.raise_for_status()
        text = response.content.decode("gb18030", errors="replace")
        match = re.search(r'="([^"]*)"', text)
        if match is None or not match.group(1).strip():
            raise ValueError("empty sina quote")
        fields = match.group(1).split(",")
        if len(fields) < 4:
            raise ValueError("invalid sina quote")
        previous = float(fields[2])
        price = float(fields[3])
        observed_at = _sina_observed_at(fields)
        change = ((price - previous) / previous * 100) if previous else None
        return MarketQuote(
            market=market,
            symbol=symbol,
            price=price,
            previous_close=previous,
            change_percent=change,
            source="新浪财经",
            observed_at=observed_at,
            delayed=True,
        )


class InstrumentedMarketProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        provider: MarketDataProvider,
        target: str,
        health_registry: ProviderHealthRegistry,
        attempt_timeout_seconds: float = 8.0,
        record_system_event: SystemEventRecorder | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._provider = provider
        self._target = target
        self._health = health_registry
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._record_system_event = record_system_event

    async def quote(self, market: str, symbol: str) -> MarketQuote:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                quote = await self._provider.quote(market, symbol)
        except Exception as exc:
            error_category = classify_provider_error(exc)
            await self._record(
                symbol=symbol,
                success=False,
                started=started,
                error_category=error_category,
            )
            if isinstance(exc, TimeoutError):
                raise MarketProviderUnavailableError(
                    "market provider timed out",
                    category=error_category,
                ) from exc
            raise
        await self._record(symbol=symbol, success=True, started=started)
        return quote

    async def _record(
        self,
        *,
        symbol: str,
        success: bool,
        started: float,
        error_category: str | None = None,
    ) -> None:
        await self._health.record_attempt(
            kind="market",
            provider=self._provider_name,
            target=self._target,
            stage="quote",
            success=success,
            attempts=1,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_category=error_category,
            record_system_event=self._record_system_event,
        )


class FailoverMarketProvider:
    def __init__(
        self,
        providers: list[tuple[str, str, MarketDataProvider]],
        *,
        failure_threshold: int,
        recovery_seconds: float,
        health_registry: ProviderHealthRegistry,
        attempt_timeout_seconds: float = 8.0,
        record_system_event: SystemEventRecorder | None = None,
        clock=time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("at least one market provider is required")
        self._providers = providers
        self._breakers = {
            name: CircuitBreaker(
                failure_threshold=failure_threshold,
                recovery_seconds=recovery_seconds,
                clock=clock,
            )
            for name, _target, _provider in providers
        }
        self._provider_gates = {
            name: asyncio.Semaphore(failure_threshold)
            for name, _target, _provider in providers
        }
        self._health = health_registry
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._record_system_event = record_system_event

    async def quote(self, market: str, symbol: str) -> MarketQuote:
        last_error: Exception | None = None
        for index, (name, target, provider) in enumerate(self._providers):
            breaker = self._breakers[name]
            async with self._provider_gates[name]:
                if not breaker.allow_request():
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote",
                        success=False,
                        attempts=1,
                        duration_ms=0,
                        error_category="circuit_open",
                        circuit_state=breaker.state,
                        record_system_event=self._record_system_event,
                    )
                    continue
                started = time.perf_counter()
                try:
                    async with asyncio.timeout(self._attempt_timeout_seconds):
                        quote = await provider.quote(market, symbol)
                except Exception as exc:
                    last_error = exc
                    breaker.record_failure()
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote",
                        success=False,
                        attempts=1,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        error_category=classify_provider_error(exc),
                        circuit_state=breaker.state,
                        record_system_event=self._record_system_event,
                    )
                    continue
                breaker.record_success()
                await self._health.record_attempt(
                    kind="market",
                    provider=name,
                    target=target,
                    stage="quote",
                    success=True,
                    attempts=1,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    circuit_state=breaker.state,
                    record_system_event=self._record_system_event,
                )
                if index > 0:
                    quote = replace(quote, source=f"{quote.source}（备用源）")
                return quote
        raise MarketProviderUnavailableError("all configured market providers failed") from last_error

    def circuit_state(self, provider_name: str) -> str:
        return self._breakers[provider_name].state


def create_market_providers(
    config: MarketsConfig,
    *,
    health_registry: ProviderHealthRegistry | None = None,
    record_system_event: SystemEventRecorder | None = None,
) -> dict[str, MarketDataProvider]:
    if not config.enabled:
        return {}
    health = health_registry or default_provider_health_registry()
    providers: dict[str, MarketDataProvider] = {}

    a_share_slots = []
    for provider_config in (config.a_share, *config.a_share_fallbacks):
        built = _build_market_provider(provider_config)
        if built is None:
            continue
        name, target, provider = built
        if any(existing[0] == name for existing in a_share_slots):
            continue
        a_share_slots.append((name, target, provider))
    if a_share_slots:
        providers["a_share"] = FailoverMarketProvider(
            a_share_slots,
            failure_threshold=config.circuit_failure_threshold,
            recovery_seconds=config.circuit_recovery_seconds,
            health_registry=health,
            attempt_timeout_seconds=config.provider_timeout_seconds,
            record_system_event=record_system_event,
        )

    built_us = _build_market_provider(config.us_share)
    if built_us is not None:
        name, target, provider = built_us
        providers["us_share"] = InstrumentedMarketProvider(
            provider_name=name,
            provider=provider,
            target=target,
            health_registry=health,
            attempt_timeout_seconds=config.provider_timeout_seconds,
            record_system_event=record_system_event,
        )
    return providers


def _build_market_provider(
    config: MarketProviderConfig,
) -> tuple[str, str, MarketDataProvider] | None:
    name = config.provider.strip().lower()
    if name == "akshare":
        return name, config.base_url or "akshare://eastmoney", AkShareMarketProvider()
    if name == "yfinance":
        return name, config.base_url or "yfinance://yahoo", YFinanceMarketProvider()
    if name == "sina":
        base_url = config.base_url or "https://hq.sinajs.cn"
        return name, base_url, SinaMarketProvider(base_url)
    return None


def _sina_symbol(symbol: str) -> str:
    code, _, suffix = symbol.upper().partition(".")
    if suffix == "SH":
        return f"sh{code}"
    if suffix == "SZ":
        return f"sz{code}"
    if suffix == "BJ":
        return f"bj{code}"
    raise ValueError("unsupported A-share symbol")


def _sina_observed_at(fields: list[str]) -> str:
    if len(fields) > 31 and fields[30] and fields[31]:
        try:
            parsed = datetime.fromisoformat(f"{fields[30]}T{fields[31]}")
            return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
        except ValueError:
            pass
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
