from __future__ import annotations

import asyncio
from html import unescape
import os
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.features.contracts import SearchProvider, SearchResult
from app.features.provider_health import (
    ProviderHealthRegistry,
    SystemEventRecorder,
    classify_provider_error,
    default_provider_health_registry,
)
from app.models import SearchConfig


class SearxngSearchProvider:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def search(self, query: str) -> list[SearchResult]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.get(
                        f"{self._base_url}/search",
                        params={"q": query, "format": "json", "pageno": page},
                        headers={"Accept": "application/json"},
                    )
                    for page in (1, 2)
                ),
                return_exceptions=True,
            )
            successful = []
            first_error = None
            for response in responses:
                if isinstance(response, BaseException):
                    first_error = first_error or response
                    continue
                try:
                    response.raise_for_status()
                except Exception as exc:
                    first_error = first_error or exc
                    continue
                successful.append(response)
            if not successful:
                if first_error is not None:
                    raise first_error
                return []
        return _deduplicate_results(
            result
            for response in successful
            for result in _map_searxng_results(response.json())
        )


class BraveSearchProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def search(self, query: str) -> list[SearchResult]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = await client.get(
                self._base_url,
                params={"q": query, "count": 20},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        return _map_brave_results(payload)


class WikipediaSearchProvider:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport
        self._core_language = _wikimedia_core_language(self._base_url)

    async def search(self, query: str) -> list[SearchResult]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "QQBot/1.0 (https://github.com/UNME85146/qq_bot)",
            },
        ) as client:
            if self._core_language is not None:
                response = await client.get(
                    self._base_url,
                    params={"q": query, "limit": 20},
                )
                response.raise_for_status()
                return _map_wikimedia_core_results(
                    response.json(),
                    language=self._core_language,
                )
            response = await client.get(
                self._base_url,
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrnamespace": 0,
                    "gsrlimit": 20,
                    "prop": "extracts|info",
                    "exintro": 1,
                    "explaintext": 1,
                    "inprop": "url",
                    "redirects": 1,
                    "format": "json",
                    "formatversion": 2,
                },
            )
            response.raise_for_status()
            payload = response.json()
        return _map_wikipedia_results(payload)


class InstrumentedSearchProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        provider: SearchProvider,
        target: str,
        health_registry: ProviderHealthRegistry,
        record_system_event: SystemEventRecorder | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._provider = provider
        self._target = target
        self._health = health_registry
        self._record_system_event = record_system_event

    async def search(self, query: str) -> list[SearchResult]:
        started = time.perf_counter()
        try:
            results = list(await self._provider.search(query))
        except Exception as exc:
            await self._health.record_attempt(
                kind="search",
                provider=self._provider_name,
                target=self._target,
                stage="search",
                success=False,
                attempts=1,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_category=classify_provider_error(exc),
                record_system_event=self._record_system_event,
            )
            raise
        await self._health.record_attempt(
            kind="search",
            provider=self._provider_name,
            target=self._target,
            stage="search",
            success=True,
            attempts=1,
            duration_ms=round((time.perf_counter() - started) * 1000),
            record_system_event=self._record_system_event,
        )
        return results


def create_search_provider(
    config: SearchConfig,
    *,
    health_registry: ProviderHealthRegistry | None = None,
    record_system_event: SystemEventRecorder | None = None,
):
    if not config.enabled or not config.base_url:
        return None
    provider = None
    if config.provider == "searxng":
        provider = SearxngSearchProvider(config.base_url)
    elif config.provider == "brave":
        api_key = os.getenv(config.api_key_env) if config.api_key_env else None
        if not api_key:
            return None
        provider = BraveSearchProvider(config.base_url, api_key)
    elif config.provider == "wikipedia":
        provider = WikipediaSearchProvider(config.base_url)
    if provider is None:
        return None
    if health_registry is None and record_system_event is None:
        return provider
    return InstrumentedSearchProvider(
        provider_name=config.provider,
        provider=provider,
        target=config.base_url,
        health_registry=health_registry or default_provider_health_registry(),
        record_system_event=record_system_event,
    )


def _map_searxng_results(payload: Any) -> list[SearchResult]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    results = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 160)
        url = _safe_url(item.get("url"))
        if not title or not url:
            continue
        engine = _clean(item.get("engine"), 40) or "SearXNG"
        source = engine if engine == "SearXNG" else f"{engine} via SearXNG"
        results.append(
            SearchResult(
                title=title,
                url=url,
                source=source,
                snippet=_clean(item.get("content"), 300) or None,
            )
        )
    return results


def _map_brave_results(payload: Any) -> list[SearchResult]:
    if not isinstance(payload, dict):
        return []
    web = payload.get("web")
    items = web.get("results") if isinstance(web, dict) else None
    if not isinstance(items, list):
        return []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 160)
        url = _safe_url(item.get("url"))
        if not title or not url:
            continue
        results.append(
            SearchResult(
                title=title,
                url=url,
                source="Brave Search",
                snippet=_clean(item.get("description"), 300) or None,
            )
        )
    return results


def _map_wikipedia_results(payload: Any) -> list[SearchResult]:
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list):
        return []
    results = []
    for item in pages:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 160)
        url = _safe_url(item.get("fullurl"))
        if not title or not url:
            continue
        results.append(
            SearchResult(
                title=title,
                url=url,
                source="Wikipedia",
                snippet=_clean(item.get("extract"), 300) or None,
            )
        )
    return results


def _map_wikimedia_core_results(
    payload: Any,
    *,
    language: str,
) -> list[SearchResult]:
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        return []
    results = []
    for item in payload["pages"]:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 160)
        key = _clean(item.get("key"), 500)
        if not title or not key:
            continue
        snippet = _clean_html(item.get("excerpt"), 300)
        if not snippet:
            snippet = _clean(item.get("description"), 300)
        results.append(
            SearchResult(
                title=title,
                url=f"https://{language}.wikipedia.org/wiki/{quote(key, safe='')}",
                source="Wikipedia",
                snippet=snippet or None,
            )
        )
    return results


def _wikimedia_core_language(base_url: str) -> str | None:
    parts = [part for part in urlsplit(base_url).path.split("/") if part]
    try:
        index = parts.index("wikipedia")
    except ValueError:
        return None
    if parts[index + 2 :] != ["search", "page"]:
        return None
    language = parts[index + 1] if len(parts) > index + 1 else ""
    return language if re.fullmatch(r"[a-z-]{2,20}", language) else None


def _clean(value: Any, limit: int) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _clean_html(value: Any, limit: int) -> str:
    return _clean(re.sub(r"<[^>]+>", "", unescape(str(value or ""))), limit)


def _safe_url(value: Any) -> str:
    url = _clean(value, 1000)
    return url if url.startswith(("http://", "https://")) else ""


def _deduplicate_results(results) -> list[SearchResult]:
    unique: dict[str, SearchResult] = {}
    for result in results:
        unique.setdefault(result.url, result)
    return list(unique.values())
