from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from app.features.contracts import MarketQuote


HITHINK_OFFICIAL_BASE_URL = "https://fuyao.aicubes.cn"
_HITHINK_OFFICIAL_HOST = "fuyao.aicubes.cn"
_HITHINK_A_SHARE_ASSET_TYPE = "a-share"
_HITHINK_SNAPSHOT_CHUNK_SIZE = 50
_HITHINK_NAME_SEARCH_LIMIT = 50
_HITHINK_DIRECTORY_LIMIT = 10_000
_HITHINK_MAX_NAME_CANDIDATES = 8
_HITHINK_DIRECTORY_TTL_SECONDS = 300.0
_SHANGHAI_TIME_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_A_SHARE_SYMBOL_PATTERN = re.compile(r"(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)")
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
_HITHINK_ERROR_MESSAGES = {
    "network_timeout": "HiThink request timed out",
    "network_error": "HiThink network request failed",
    "rate_limited": "HiThink request was rate limited",
    "auth_error": "HiThink authentication failed",
    "http_4xx": "HiThink request was rejected",
    "http_5xx": "HiThink upstream service failed",
    "provider_error": "HiThink provider request failed",
}
_HITHINK_BUSINESS_CODE_CATEGORIES = {
    2001: "auth_error",
    2003: "auth_error",
    4001: "rate_limited",
}


class HiThinkProviderError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = (
            category if category in _HITHINK_ERROR_MESSAGES else "provider_error"
        )
        super().__init__(_HITHINK_ERROR_MESSAGES[self.category])


class HiThinkMarketProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = HITHINK_OFFICIAL_BASE_URL,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        directory_ttl_seconds: float = _HITHINK_DIRECTORY_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("HiThink API key is not configured")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("HiThink timeout must be positive")
        if (
            isinstance(directory_ttl_seconds, bool)
            or not isinstance(directory_ttl_seconds, (int, float))
            or not math.isfinite(directory_ttl_seconds)
            or directory_ttl_seconds <= 0
        ):
            raise ValueError("HiThink directory TTL must be positive")

        self._api_key = api_key.strip()
        self._base_url = _validate_official_base_url(base_url)
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport
        self._directory_ttl_seconds = float(directory_ttl_seconds)
        self._clock = clock
        self._directory_by_symbol: dict[str, str] | None = None
        self._directory_expires_at = 0.0
        self._directory_lock = asyncio.Lock()

    async def quote(self, market: str, symbol: str) -> MarketQuote:
        _require_a_share_market(market)
        raw_symbol = _require_text(symbol, "HiThink stock symbol is invalid")
        if _CJK_PATTERN.search(raw_symbol):
            return await self.quote_by_name(market, (raw_symbol,))
        quotes = await self.quote_many(market, [raw_symbol])
        return quotes[0]

    async def quote_many(self, market: str, symbols: list[str]) -> list[MarketQuote]:
        _require_a_share_market(market)
        if not symbols:
            raise ValueError("HiThink quote request is empty")
        requested_symbols = [_canonical_a_share_symbol(symbol) for symbol in symbols]

        async with self._new_client() as client:
            directory = await self._ticker_directory(client)
            quotes: list[MarketQuote] = []
            for index in range(0, len(requested_symbols), _HITHINK_SNAPSHOT_CHUNK_SIZE):
                requested_chunk = requested_symbols[
                    index : index + _HITHINK_SNAPSHOT_CHUNK_SIZE
                ]
                snapshot_symbols: list[str] = []
                for symbol in requested_chunk:
                    if symbol in directory and symbol not in snapshot_symbols:
                        snapshot_symbols.append(symbol)
                if not snapshot_symbols:
                    continue
                names_by_symbol = {
                    symbol: directory[symbol] for symbol in snapshot_symbols
                }
                quotes_by_symbol = await self._snapshot_quotes(
                    client,
                    snapshot_symbols,
                    names_by_symbol,
                )
                quotes.extend(
                    quotes_by_symbol[symbol]
                    for symbol in requested_chunk
                    if symbol in quotes_by_symbol
                )

        if not quotes:
            raise ValueError("HiThink did not return requested A-share quotes")
        return quotes

    async def quote_by_name(
        self,
        market: str,
        candidates: Sequence[str],
    ) -> MarketQuote:
        _require_a_share_market(market)
        normalized_candidates = _normalize_name_candidates(candidates)
        async with self._new_client() as client:
            symbol, name = await self._resolve_name(client, normalized_candidates)
            quotes_by_symbol = await self._snapshot_quotes(
                client,
                [symbol],
                {symbol: name},
            )
        quote = quotes_by_symbol.get(symbol)
        if quote is None:
            raise ValueError("HiThink did not return the requested A-share quote")
        return quote

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            transport=self._transport,
            headers={"X-api-key": self._api_key},
            follow_redirects=False,
        )

    async def _ticker_directory(self, client: httpx.AsyncClient) -> dict[str, str]:
        now = self._clock()
        if (
            self._directory_by_symbol is not None
            and now < self._directory_expires_at
        ):
            return self._directory_by_symbol

        async with self._directory_lock:
            now = self._clock()
            if (
                self._directory_by_symbol is not None
                and now < self._directory_expires_at
            ):
                return self._directory_by_symbol

            data = await self._request_data(
                client,
                "/api/meta/tickers/list",
                {
                    "asset_type": _HITHINK_A_SHARE_ASSET_TYPE,
                    "limit": _HITHINK_DIRECTORY_LIMIT,
                    "offset": 0,
                },
            )
            items = _response_items(data)
            if len(items) >= _HITHINK_DIRECTORY_LIMIT:
                raise HiThinkProviderError("provider_error")

            directory: dict[str, str] = {}
            for item in items:
                if item.get("asset_type") != _HITHINK_A_SHARE_ASSET_TYPE:
                    continue
                try:
                    symbol = _canonical_a_share_symbol(item.get("thscode"))
                except ValueError:
                    continue
                name = _normalized_name(item.get("name"))
                if name:
                    directory.setdefault(symbol, name)
            if not directory:
                raise ValueError("HiThink ticker directory is invalid")
            self._directory_by_symbol = directory
            self._directory_expires_at = now + self._directory_ttl_seconds
            return directory

    async def _resolve_name(
        self,
        client: httpx.AsyncClient,
        candidates: Sequence[str],
    ) -> tuple[str, str]:
        ambiguous = False
        truncated = False
        for candidate in candidates:
            data = await self._request_data(
                client,
                "/api/meta/tickers/search",
                {
                    "q": candidate,
                    "asset_type": _HITHINK_A_SHARE_ASSET_TYPE,
                    "limit": _HITHINK_NAME_SEARCH_LIMIT,
                },
            )
            items = _response_items(data)
            matches: dict[str, str] = {}
            for item in items:
                if item.get("asset_type") != _HITHINK_A_SHARE_ASSET_TYPE:
                    continue
                try:
                    symbol = _canonical_a_share_symbol(item.get("thscode"))
                except ValueError:
                    continue
                name = _normalized_name(item.get("name"))
                if name:
                    matches.setdefault(symbol, name)

            exact_matches = {
                symbol: name
                for symbol, name in matches.items()
                if _normalized_name(name) == candidate
            }
            if len(exact_matches) == 1:
                return next(iter(exact_matches.items()))
            if len(exact_matches) > 1:
                ambiguous = True
                continue
            if len(items) >= _HITHINK_NAME_SEARCH_LIMIT:
                truncated = True
                continue

            substring_matches = {
                symbol: name
                for symbol, name in matches.items()
                if candidate in _normalized_name(name)
            }
            if len(substring_matches) == 1:
                return next(iter(substring_matches.items()))
            if len(substring_matches) > 1:
                ambiguous = True

        if ambiguous or truncated:
            raise ValueError("HiThink stock name is ambiguous")
        raise ValueError("HiThink stock name was not found")

    async def _snapshot_quotes(
        self,
        client: httpx.AsyncClient,
        requested_symbols: Sequence[str],
        names_by_symbol: Mapping[str, str],
    ) -> dict[str, MarketQuote]:
        data = await self._request_data(
            client,
            "/api/a-share/prices/snapshot",
            {"thscodes": ",".join(requested_symbols)},
        )
        observed_at = _observed_at_from_milliseconds(data.get("timestamp"))
        requested_set = set(requested_symbols)
        quotes: dict[str, MarketQuote] = {}
        for item in _response_items(data):
            try:
                symbol = _canonical_a_share_symbol(item.get("thscode"))
            except ValueError:
                continue
            if symbol not in requested_set or symbol in quotes:
                continue
            price = _finite_number(item.get("last_price"))
            if price is None or price <= 0:
                continue
            try:
                previous_close = _nullable_finite_number(item.get("prev_price"))
                change_percent = _nullable_finite_number(
                    item.get("price_change_ratio_pct")
                )
            except ValueError:
                continue
            name = names_by_symbol.get(symbol)
            if not name:
                continue
            quotes[symbol] = MarketQuote(
                market="a_share",
                symbol=symbol,
                name=name,
                price=price,
                previous_close=previous_close,
                change_percent=change_percent,
                source="HiThink Financial-API",
                observed_at=observed_at,
                delayed=True,
            )
        return quotes

    async def _request_data(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Mapping[str, str | int],
    ) -> Mapping[str, object]:
        try:
            response = await client.get(path, params=params)
        except httpx.TimeoutException:
            raise HiThinkProviderError("network_timeout") from None
        except httpx.HTTPError:
            raise HiThinkProviderError("network_error") from None
        if response.status_code != 200:
            raise HiThinkProviderError(_http_status_category(response.status_code))
        try:
            payload = response.json()
        except ValueError:
            raise HiThinkProviderError("provider_error") from None
        if not isinstance(payload, Mapping):
            raise HiThinkProviderError("provider_error")
        code = payload.get("code")
        if type(code) is not int or code != 0:
            raise HiThinkProviderError(_business_code_category(code))
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise HiThinkProviderError("provider_error")
        return data


def _validate_official_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("HiThink base URL must use the official HTTPS endpoint")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            "HiThink base URL must use the official HTTPS endpoint"
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _HITHINK_OFFICIAL_HOST
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HiThink base URL must use the official HTTPS endpoint")
    return HITHINK_OFFICIAL_BASE_URL


def _require_a_share_market(market: str) -> None:
    if market != "a_share":
        raise ValueError("HiThink only supports A shares")


def _http_status_category(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if 400 <= status_code < 500:
        return "http_4xx"
    if status_code >= 500:
        return "http_5xx"
    return "provider_error"


def _business_code_category(code: object) -> str:
    if type(code) is not int:
        return "provider_error"
    return _HITHINK_BUSINESS_CODE_CATEGORIES.get(code, "provider_error")


def _require_text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()


def _canonical_a_share_symbol(value: object) -> str:
    raw = _require_text(value, "HiThink stock symbol is invalid").upper()
    match = _A_SHARE_SYMBOL_PATTERN.fullmatch(raw)
    if match is None:
        raise ValueError("HiThink stock symbol is invalid")
    return f"{match.group('code')}.{match.group('exchange')}"


def _normalize_name_candidates(candidates: Sequence[str]) -> tuple[str, ...]:
    if isinstance(candidates, str) or len(candidates) > _HITHINK_MAX_NAME_CANDIDATES:
        raise ValueError("HiThink name candidates are invalid")
    normalized: list[str] = []
    for candidate in candidates:
        name = _normalized_name(candidate)
        if not name or len(name) > 64:
            continue
        if name not in normalized:
            normalized.append(name)
    if not normalized:
        raise ValueError("HiThink name candidates are invalid")
    return tuple(normalized)


def _normalized_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _response_items(data: Mapping[str, object]) -> list[Mapping[str, object]]:
    items = data.get("item")
    if not isinstance(items, list):
        raise ValueError("HiThink response is invalid")
    return [item for item in items if isinstance(item, Mapping)]


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nullable_finite_number(value: object) -> float | None:
    if value is None:
        return None
    parsed = _finite_number(value)
    if parsed is None:
        raise ValueError("HiThink quote number is invalid")
    return parsed


def _observed_at_from_milliseconds(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("HiThink snapshot timestamp is invalid")
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("HiThink snapshot timestamp is invalid") from None
    if not math.isfinite(milliseconds) or milliseconds < 0 or not milliseconds.is_integer():
        raise ValueError("HiThink snapshot timestamp is invalid")
    try:
        observed_at = datetime.fromtimestamp(
            milliseconds / 1000,
            tz=timezone.utc,
        ).astimezone(_SHANGHAI_TIME_ZONE)
    except (OverflowError, OSError, ValueError):
        raise ValueError("HiThink snapshot timestamp is invalid") from None
    return observed_at.isoformat()
