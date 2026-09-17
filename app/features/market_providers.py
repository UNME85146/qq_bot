from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

import httpx

from app.features.contracts import MarketDataProvider, MarketQuote
from app.features.hithink_market_provider import (
    HITHINK_OFFICIAL_BASE_URL,
    HiThinkMarketProvider,
)
from app.features.provider_health import (
    CircuitBreaker,
    ProviderHealthRegistry,
    SystemEventRecorder,
    classify_provider_error,
    default_provider_health_registry,
)
from app.models import MarketProviderConfig, MarketsConfig


_BEIJING_TIME_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_YFINANCE_BATCH_CHUNK_SIZE = 25
_YFINANCE_BATCH_CONCURRENCY = 4
_SINA_SUGGEST_MAX_BYTES = 256 * 1024
_SINA_SUGGEST_URL_PREFIX = (
    "https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key="
)
_AKSHARE_NAME_COLUMNS = (
    "名称",
    "股票简称",
    "证券简称",
    "简称",
    "公司名称",
    "公司简称",
    "公司",
)


class MarketProviderUnavailableError(RuntimeError):
    def __init__(self, message: str, *, category: str = "provider_error") -> None:
        self.category = category
        super().__init__(message)


class YFinanceMarketProvider:
    async def quote(self, market: str, symbol: str) -> MarketQuote:
        return await _quote_in_isolated_process("yfinance", market, symbol)

    async def quote_many(self, market: str, symbols: list[str]) -> list[MarketQuote]:
        chunks = [
            symbols[index : index + _YFINANCE_BATCH_CHUNK_SIZE]
            for index in range(0, len(symbols), _YFINANCE_BATCH_CHUNK_SIZE)
        ]
        gate = asyncio.Semaphore(_YFINANCE_BATCH_CONCURRENCY)

        async def fetch_chunk(chunk: list[str]) -> list[MarketQuote]:
            async with gate:
                return await _quotes_in_isolated_process("yfinance", market, chunk)

        results = await asyncio.gather(*(fetch_chunk(chunk) for chunk in chunks))
        return [quote for chunk_quotes in results for quote in chunk_quotes]

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
            observed_at=datetime.now(_BEIJING_TIME_ZONE).isoformat(),
            delayed=True,
        )

    @staticmethod
    def _quotes_sync(market: str, symbols: list[str]) -> list[MarketQuote]:
        if not symbols:
            return []
        import yfinance as yf

        frame = yf.download(
            tickers=symbols,
            period="5d",
            interval="1d",
            auto_adjust=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )
        quotes: list[MarketQuote] = []
        observed_at = datetime.now(_BEIJING_TIME_ZONE).isoformat()
        for symbol in symbols:
            try:
                closes = [float(value) for value in frame[symbol]["Close"].dropna().tolist()]
            except Exception:
                continue
            if not closes:
                continue
            price = closes[-1]
            previous = closes[-2] if len(closes) >= 2 else None
            change = ((price - previous) / previous * 100) if previous else None
            quotes.append(
                MarketQuote(
                    market=market,
                    symbol=symbol,
                    name=symbol,
                    price=price,
                    previous_close=previous,
                    change_percent=change,
                    source="Yahoo Finance via yfinance",
                    observed_at=observed_at,
                    delayed=True,
                )
            )
        return quotes


class AkShareMarketProvider:
    async def quote(self, market: str, symbol: str) -> MarketQuote:
        return await _quote_in_isolated_process("akshare", market, symbol)

    async def quote_many(self, market: str, symbols: list[str]) -> list[MarketQuote]:
        return await _quotes_in_isolated_process("akshare", market, symbols)

    async def quote_by_name(
        self,
        market: str,
        candidates: Sequence[str],
    ) -> MarketQuote:
        return await _quote_name_in_isolated_process(
            "akshare",
            market,
            tuple(str(candidate) for candidate in candidates),
        )

    @staticmethod
    def _quote_sync(market: str, symbol: str) -> MarketQuote:
        if market == "a_share" and _is_a_share_code_symbol(symbol):
            return _akshare_tencent_code_quote(market, symbol)
        frame = _akshare_tencent_frame(market)
        return _akshare_quotes_from_frame(market, frame, [symbol], strict=True)[0]

    @staticmethod
    def _quotes_sync(market: str, symbols: list[str]) -> list[MarketQuote]:
        frame = _akshare_tencent_frame(market)
        return _akshare_quotes_from_frame(market, frame, symbols, strict=False)

    @staticmethod
    def _quote_by_name_sync(
        market: str,
        candidates: Sequence[str],
    ) -> MarketQuote:
        frame = _akshare_tencent_frame(market)
        matched = _akshare_name_match(frame, candidates)
        if matched.empty:
            raise RuntimeError("stock name was not found")
        return _akshare_quote_from_row(market, matched.iloc[0], requested_symbol="")


def _akshare_tencent_frame(market: str):
    if market != "a_share":
        raise ValueError("AkShare only supports A shares")
    import akshare as ak

    raw = ak.stock_zh_a_spot_tx()
    rows = _normalize_akshare_tencent_rows(raw.to_dict("records"))
    if not rows:
        raise RuntimeError("AkShare returned no valid A-share prices")
    return type(raw)(rows)


def _is_a_share_code_symbol(symbol: str) -> bool:
    return re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", symbol.strip().upper()) is not None


def _akshare_tencent_code_quote(market: str, symbol: str) -> MarketQuote:
    if market != "a_share":
        raise ValueError("AkShare only supports A shares")
    import requests

    normalized = symbol.strip().upper()
    code, exchange = normalized.split(".")
    response = requests.get(
        "https://qt.gtimg.cn/q=" + exchange.lower() + code,
        timeout=10,
    )
    response.raise_for_status()
    payload = response.content.decode("gbk", errors="replace")
    match = re.search(r'v_[^=]+="([^"]*)"', payload)
    if match is None:
        raise RuntimeError("Tencent returned an invalid quote")
    fields = match.group(1).split("~")
    if len(fields) < 8 or fields[2] != code:
        raise RuntimeError("Tencent did not return the requested stock")
    price = _coerce_float(fields[3])
    previous = _coerce_float(fields[4])
    volume = _coerce_float(fields[6])
    if price is None or price <= 0 or previous is None or previous <= 0:
        raise RuntimeError("Tencent returned no current quote")
    # The quote endpoint keeps the last price for suspended symbols; zero volume
    # with no price movement is treated as unavailable instead of current data.
    if volume == 0 and price == previous:
        raise RuntimeError("Tencent quote is suspended or unavailable")
    return MarketQuote(
        market=market,
        symbol=f"{code}.{exchange}",
        name=fields[1].strip(),
        price=price,
        previous_close=previous,
        change_percent=((price - previous) / previous * 100) if previous else None,
        source="腾讯证券 via AkShare",
        observed_at=datetime.now(_BEIJING_TIME_ZONE).isoformat(),
        delayed=True,
    )


def _normalize_akshare_tencent_rows(records: list[dict]) -> list[dict]:
    rows = []
    for record in records:
        symbol = re.fullmatch(r"(sh|sz|bj)([0-9]{6})", str(record.get("code", "")))
        price = _coerce_float(record.get("zxj"))
        # Tencent may retain the last traded price while state=S is suspended.
        if symbol is None or record.get("state") not in {"", None} or price is None or price <= 0:
            continue
        change_amount = _coerce_float(record.get("zd"))
        previous = round(price - change_amount, 4) if change_amount is not None else None
        if previous is not None and previous <= 0:
            previous = None
        rows.append({
            "代码": symbol.group(2),
            "交易所": symbol.group(1).upper(),
            "名称": str(record.get("name") or ""),
            "最新价": price,
            "昨收": previous,
            "涨跌幅": _coerce_float(record.get("zdf")),
        })
    return rows


def _akshare_quotes_from_frame(
    market: str,
    frame,
    symbols: list[str],
    *,
    strict: bool,
) -> list[MarketQuote]:
    quotes: list[MarketQuote] = []
    for symbol in symbols:
        raw_symbol = symbol.strip().upper()
        raw_code, _separator, raw_suffix = raw_symbol.partition(".")
        is_code_query = raw_code.isdigit()
        code = raw_code.zfill(6) if is_code_query else ""
        matched = (
            frame.loc[frame["代码"].astype(str).str.zfill(6) == code]
            if is_code_query
            else frame.iloc[0:0]
        )
        if matched.empty and not is_code_query:
            matched = _akshare_name_match(frame, (symbol,))
        if matched.empty:
            if strict:
                raise RuntimeError("stock symbol was not found")
            continue
        quotes.append(
            _akshare_quote_from_row(
                market,
                matched.iloc[0],
                requested_symbol=raw_symbol if is_code_query else "",
            )
        )
    return quotes


def _akshare_name_match(frame, candidates: Sequence[str]):
    columns = [column for column in _AKSHARE_NAME_COLUMNS if column in frame.columns]
    if not columns:
        return frame.iloc[0:0]
    normalized_columns = {
        column: frame[column].map(_normalize_name_value)
        for column in columns
    }
    for candidate in candidates:
        normalized_candidate = _normalize_name_value(candidate)
        if not normalized_candidate:
            continue
        for mode in ("exact", "prefix", "contains"):
            for column in columns:
                values = normalized_columns[column]
                if mode == "exact":
                    mask = values == normalized_candidate
                elif mode == "prefix":
                    mask = values.str.startswith(normalized_candidate)
                else:
                    mask = values.str.contains(normalized_candidate, regex=False)
                matched = frame.loc[mask]
                if not matched.empty:
                    return matched
    return frame.iloc[0:0]


def _normalize_name_value(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _akshare_quote_from_row(market: str, row, *, requested_symbol: str) -> MarketQuote:
    code = str(row["代码"]).strip().zfill(6)
    raw_symbol = requested_symbol.upper()
    _raw_code, _separator, raw_suffix = raw_symbol.partition(".")
    actual_suffix = str(row.get("交易所", ""))
    if actual_suffix in {"SH", "SZ", "BJ"}:
        if raw_suffix and raw_suffix != actual_suffix:
            raise ValueError("stock exchange does not match the directory")
        suffix = actual_suffix
    else:
        suffix = raw_suffix if raw_suffix in {"SH", "SZ", "BJ"} else _a_share_suffix(code)
    canonical_symbol = f"{code}.{suffix}"
    raw_name = ""
    for column in _AKSHARE_NAME_COLUMNS:
        if column in row.index:
            raw_name = _normalize_name_value(row[column])
            if raw_name:
                break
    price = _coerce_float(row["最新价"])
    if price is None or price <= 0:
        raise RuntimeError("stock quote was unavailable")
    previous = _coerce_float(row["昨收"]) if "昨收" in row.index else None
    raw_change = row["涨跌幅"] if "涨跌幅" in row.index else None
    change = _coerce_float(raw_change)
    return MarketQuote(
        market=market,
        symbol=canonical_symbol,
        price=price,
        previous_close=previous,
        change_percent=change,
        source="腾讯证券 via AkShare",
        observed_at=datetime.now(_BEIJING_TIME_ZONE).isoformat(),
        delayed=True,
        name=raw_name or None,
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


async def _quote_name_in_isolated_process(
    provider: str,
    market: str,
    candidates: tuple[str, ...],
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
        {
            "provider": provider,
            "market": market,
            "nameCandidates": list(candidates),
        },
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
        raise MarketProviderUnavailableError("invalid_response", category="invalid_response")
    try:
        return MarketQuote(**quote)
    except (TypeError, ValueError) as exc:
        raise MarketProviderUnavailableError("invalid_response", category="invalid_response") from exc


async def _quotes_in_isolated_process(
    provider: str,
    market: str,
    symbols: list[str],
) -> list[MarketQuote]:
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
        {"provider": provider, "market": market, "symbols": symbols},
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
    quotes = response.get("quotes")
    if not isinstance(quotes, list):
        raise MarketProviderUnavailableError("invalid_response", category="invalid_response")
    try:
        return [MarketQuote(**quote) for quote in quotes if isinstance(quote, dict)]
    except (TypeError, ValueError) as exc:
        raise MarketProviderUnavailableError("invalid_response", category="invalid_response") from exc


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
        quotes = await self.quote_many(market, [symbol])
        if not quotes:
            raise ValueError("empty sina quote")
        return quotes[0]

    async def quote_by_name(
        self,
        market: str,
        candidates: Sequence[str],
    ) -> MarketQuote:
        if market != "a_share":
            raise ValueError("Sina name lookup only supports A shares")
        symbol = await self._resolve_name(candidates)
        return await self.quote(market, symbol)

    async def _resolve_name(self, candidates: Sequence[str]) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 QQBotMarket/1.0",
            "Referer": "https://finance.sina.com.cn/",
        }
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
            headers=headers,
            follow_redirects=False,
        ) as client:
            for candidate in candidates:
                normalized = _normalize_name_value(candidate)
                if not normalized:
                    continue
                response = await client.get(
                    _SINA_SUGGEST_URL_PREFIX
                    + url_quote(normalized, safe="")
                    + "&name=suggestdata"
                )
                response.raise_for_status()
                if len(response.content) > _SINA_SUGGEST_MAX_BYTES:
                    raise ValueError("Sina stock suggestion response is too large")
                suggestions = _parse_sina_name_suggestions(response.content)
                selected = _select_sina_name_suggestion(normalized, suggestions)
                if selected is not None:
                    return selected[1]
        raise ValueError("Sina stock name was not found")

    async def quote_many(self, market: str, symbols: list[str]) -> list[MarketQuote]:
        request_symbols = [_sina_symbol(symbol) for symbol in symbols]
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
            headers={
                "User-Agent": "Mozilla/5.0 QQBotMarket/1.0",
                "Referer": "https://finance.sina.com.cn/",
            },
        ) as client:
            response = await client.get(
                f"{self._base_url}/list={','.join(request_symbols)}"
            )
            response.raise_for_status()
        text = response.content.decode("gb18030", errors="replace")
        payloads = {
            request_symbol: fields_text
            for request_symbol, fields_text in re.findall(
                r'hq_str_([^=]+)="([^"]*)"',
                text,
            )
            if fields_text.strip()
        }
        quotes: list[MarketQuote] = []
        for symbol, request_symbol in zip(symbols, request_symbols, strict=True):
            fields_text = payloads.get(request_symbol)
            if not fields_text:
                continue
            fields = fields_text.split(",")
            if len(fields) < 4:
                continue
            previous = float(fields[2])
            price = float(fields[3])
            if price <= 0 < previous:
                price = previous
            if price <= 0:
                continue
            observed_at = _sina_observed_at(fields)
            change = ((price - previous) / previous * 100) if previous else None
            quotes.append(
                MarketQuote(
                    market=market,
                    symbol=symbol,
                    name=str(fields[0]).strip() or None,
                    price=price,
                    previous_close=previous,
                    change_percent=change,
                    source="新浪财经",
                    observed_at=observed_at,
                    delayed=True,
                )
            )
        return quotes


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

    @property
    def supports_quote_many(self) -> bool:
        return callable(getattr(self._provider, "quote_many", None))

    @property
    def supports_name_lookup(self) -> bool:
        return callable(getattr(self._provider, "quote_by_name", None))

    async def quote(self, market: str, symbol: str) -> MarketQuote:
        for attempt in range(1, 3):
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
                    attempts=attempt,
                    error_category=error_category,
                )
                if attempt == 1 and error_category in {
                    "network_error",
                    "network_timeout",
                }:
                    await asyncio.sleep(0)
                    continue
                if error_category == "network_timeout":
                    raise MarketProviderUnavailableError(
                        "market provider timed out",
                        category=error_category,
                    ) from exc
                raise
            await self._record(
                symbol=symbol,
                success=True,
                started=started,
                attempts=attempt,
            )
            return quote
        raise AssertionError("unreachable")

    async def quote_many(self, market: str, symbols: list[str]) -> list[MarketQuote]:
        quote_many = getattr(self._provider, "quote_many", None)
        if not callable(quote_many):
            return await asyncio.gather(*(self.quote(market, symbol) for symbol in symbols))
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                quotes = await quote_many(market, symbols)
        except Exception as exc:
            await self._health.record_attempt(
                kind="market",
                provider=self._provider_name,
                target=self._target,
                stage="quote_many",
                success=False,
                attempts=1,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_category=classify_provider_error(exc),
                record_system_event=self._record_system_event,
            )
            raise
        await self._health.record_attempt(
            kind="market",
            provider=self._provider_name,
            target=self._target,
            stage="quote_many",
            success=True,
            attempts=1,
            duration_ms=round((time.perf_counter() - started) * 1000),
            record_system_event=self._record_system_event,
        )
        return quotes

    async def quote_by_name(
        self,
        market: str,
        candidates: Sequence[str],
    ) -> MarketQuote:
        quote_by_name = getattr(self._provider, "quote_by_name", None)
        if not callable(quote_by_name):
            raise MarketProviderUnavailableError(
                "market provider does not support name lookup",
                category="unsupported",
            )
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                quote = await quote_by_name(market, candidates)
        except Exception as exc:
            await self._health.record_attempt(
                kind="market",
                provider=self._provider_name,
                target=self._target,
                stage="quote_by_name",
                success=False,
                attempts=1,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_category=classify_provider_error(exc),
                record_system_event=self._record_system_event,
            )
            raise
        await self._health.record_attempt(
            kind="market",
            provider=self._provider_name,
            target=self._target,
            stage="quote_by_name",
            success=True,
            attempts=1,
            duration_ms=round((time.perf_counter() - started) * 1000),
            record_system_event=self._record_system_event,
        )
        return quote

    async def _record(
        self,
        *,
        symbol: str,
        success: bool,
        started: float,
        attempts: int,
        error_category: str | None = None,
    ) -> None:
        await self._health.record_attempt(
            kind="market",
            provider=self._provider_name,
            target=self._target,
            stage="quote",
            success=success,
            attempts=attempts,
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

    @property
    def supports_quote_many(self) -> bool:
        return any(callable(getattr(provider, "quote_many", None)) for _, _, provider in self._providers)

    @property
    def supports_name_lookup(self) -> bool:
        return any(callable(getattr(provider, "quote_by_name", None)) for _, _, provider in self._providers)

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
                attempt_limit = 1 if index == 0 or breaker.state == "half_open" else 2
                for attempt in range(1, attempt_limit + 1):
                    started = time.perf_counter()
                    try:
                        async with asyncio.timeout(self._attempt_timeout_seconds):
                            quote = await provider.quote(market, symbol)
                    except Exception as exc:
                        last_error = exc
                        error_category = classify_provider_error(exc)
                        should_retry = (
                            attempt < attempt_limit
                            and error_category in {"network_error", "network_timeout"}
                        )
                        if not should_retry:
                            breaker.record_failure()
                        await self._health.record_attempt(
                            kind="market",
                            provider=name,
                            target=target,
                            stage="quote",
                            success=False,
                            attempts=attempt,
                            duration_ms=round((time.perf_counter() - started) * 1000),
                            error_category=error_category,
                            circuit_state=breaker.state,
                            record_system_event=self._record_system_event,
                        )
                        if should_retry:
                            await asyncio.sleep(0)
                            continue
                        break
                    breaker.record_success()
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote",
                        success=True,
                        attempts=attempt,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        circuit_state=breaker.state,
                        record_system_event=self._record_system_event,
                    )
                    if index > 0:
                        quote = replace(quote, source=f"{quote.source}（备用源）")
                    return quote
        raise MarketProviderUnavailableError("all configured market providers failed") from last_error

    async def quote_by_name(
        self,
        market: str,
        candidates: Sequence[str],
    ) -> MarketQuote:
        last_error: Exception | None = None
        for index, (name, target, provider) in enumerate(self._providers):
            quote_by_name = getattr(provider, "quote_by_name", None)
            if not callable(quote_by_name):
                continue
            breaker = self._breakers[name]
            async with self._provider_gates[name]:
                if not breaker.allow_request():
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote_by_name",
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
                        quote = await quote_by_name(market, candidates)
                except Exception as exc:
                    last_error = exc
                    breaker.record_failure()
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote_by_name",
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
                    stage="quote_by_name",
                    success=True,
                    attempts=1,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    circuit_state=breaker.state,
                    record_system_event=self._record_system_event,
                )
                if index > 0:
                    quote = replace(quote, source=f"{quote.source}（备用源）")
                return quote
        raise MarketProviderUnavailableError(
            "all configured market providers failed name lookup",
        ) from last_error

    async def quote_many(self, market: str, symbols: list[str]) -> list[MarketQuote]:
        last_error: Exception | None = None
        for index, (name, target, provider) in enumerate(self._providers):
            breaker = self._breakers[name]
            async with self._provider_gates[name]:
                if not breaker.allow_request():
                    continue
                started = time.perf_counter()
                try:
                    quote_many = getattr(provider, "quote_many", None)
                    if callable(quote_many):
                        async with asyncio.timeout(self._attempt_timeout_seconds):
                            quotes = await quote_many(market, symbols)
                    else:
                        quotes = await asyncio.gather(
                            *(provider.quote(market, symbol) for symbol in symbols)
                        )
                    breaker.record_success()
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote_many",
                        success=True,
                        attempts=1,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        circuit_state=breaker.state,
                        record_system_event=self._record_system_event,
                    )
                    if index > 0:
                        quotes = [replace(quote, source=f"{quote.source}（备用源）") for quote in quotes]
                    return quotes
                except Exception as exc:
                    last_error = exc
                    breaker.record_failure()
                    await self._health.record_attempt(
                        kind="market",
                        provider=name,
                        target=target,
                        stage="quote_many",
                        success=False,
                        attempts=1,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        error_category=classify_provider_error(exc),
                        circuit_state=breaker.state,
                        record_system_event=self._record_system_event,
                    )
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
        return name, config.base_url or "akshare://tencent", AkShareMarketProvider()
    if name == "yfinance":
        return name, config.base_url or "yfinance://yahoo", YFinanceMarketProvider()
    if name == "sina":
        base_url = config.base_url or "https://hq.sinajs.cn"
        return name, base_url, SinaMarketProvider(base_url)
    if name == "hithink":
        api_key_env = config.api_key_env or "QQ_BOT_HITHINK_API_KEY"
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError("HiThink API key is not configured")
        base_url = config.base_url or HITHINK_OFFICIAL_BASE_URL
        return name, base_url, HiThinkMarketProvider(
            api_key=api_key,
            base_url=base_url,
        )
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


def _parse_sina_name_suggestions(content: bytes) -> list[tuple[str, str]]:
    text = content.decode("gb18030", errors="strict")
    match = re.fullmatch(r'\s*var\s+suggestdata="(?P<body>.*)";?\s*', text)
    if match is None:
        raise ValueError("Sina stock suggestion response is invalid")
    suggestions: list[tuple[str, str]] = []
    for entry in match.group("body").split(";"):
        fields = entry.split(",")
        if len(fields) < 4 or fields[1].strip() != "11":
            continue
        name = _normalize_name_value(fields[0])
        raw_symbol = fields[3].strip().lower()
        symbol_match = re.fullmatch(r"(?P<exchange>sh|sz|bj)(?P<code>\d{6})", raw_symbol)
        if not name or symbol_match is None:
            continue
        suggestions.append(
            (
                name,
                f"{symbol_match.group('code')}.{symbol_match.group('exchange').upper()}",
            )
        )
    return suggestions


def _select_sina_name_suggestion(
    candidate: str,
    suggestions: Sequence[tuple[str, str]],
) -> tuple[str, str] | None:
    for matcher in (
        lambda name: name == candidate,
        lambda name: name.startswith(candidate),
        lambda name: candidate in name,
    ):
        for suggestion in suggestions:
            if matcher(suggestion[0]):
                return suggestion
    return None


def _a_share_suffix(code: str) -> str:
    if code.startswith(("4", "8")):
        return "BJ"
    if code.startswith(("5", "6", "9")):
        return "SH"
    return "SZ"


def _coerce_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sina_observed_at(fields: list[str]) -> str:
    if len(fields) > 31 and fields[30] and fields[31]:
        try:
            parsed = datetime.fromisoformat(f"{fields[30]}T{fields[31]}")
            return parsed.replace(tzinfo=_BEIJING_TIME_ZONE).isoformat()
        except ValueError:
            pass
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
