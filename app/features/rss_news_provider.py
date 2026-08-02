from __future__ import annotations

import asyncio
import calendar
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import feedparser
import httpx

from app.features.contracts import NewsItem
from app.features.provider_health import (
    ProviderHealthRegistry,
    SystemEventRecorder,
    classify_provider_error,
    default_provider_health_registry,
)


class NewsSourceUnavailableError(RuntimeError):
    pass


class RssNewsProvider:
    def __init__(
        self,
        *,
        feeds: dict[str, tuple[str, ...]],
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        health_registry: ProviderHealthRegistry | None = None,
        record_system_event: SystemEventRecorder | None = None,
    ) -> None:
        self._feeds = feeds
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._health = health_registry or default_provider_health_registry()
        self._record_system_event = record_system_event

    async def fetch(self, category: str) -> list[NewsItem]:
        urls = tuple(dict.fromkeys(self._feeds.get(category, ())))
        if not urls:
            raise NewsSourceUnavailableError("no feeds configured")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            transport=self._transport,
            follow_redirects=True,
            headers={"User-Agent": "QQBotNews/1.0 (+RSS reader)"},
        ) as client:
            results = await asyncio.gather(
                *(self._fetch_one(client, category, url) for url in urls),
                return_exceptions=True,
            )
        successful = [result for result in results if not isinstance(result, Exception)]
        if not successful:
            raise NewsSourceUnavailableError("all feeds failed")
        items = [item for result in successful for item in result]
        unique: dict[str, NewsItem] = {}
        for item in items:
            key = (item.url or item.title).strip().lower()
            unique.setdefault(key, item)
        return sorted(
            unique.values(),
            key=lambda item: item.published_at or "",
            reverse=True,
        )

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        category: str,
        url: str,
    ) -> list[NewsItem]:
        started = time.perf_counter()
        try:
            response = await client.get(url)
            response.raise_for_status()
            parsed = await asyncio.to_thread(feedparser.parse, response.content)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                raise ValueError("invalid RSS payload")
            source = str(parsed.feed.get("title") or urlsplit(url).netloc or "RSS")
            items = []
            for entry in parsed.entries:
                title = str(entry.get("title") or "").strip()
                if not title:
                    continue
                link = str(entry.get("link") or "").strip() or None
                if link is not None and not link.startswith(("http://", "https://")):
                    link = None
                items.append(
                    NewsItem(
                        category=category,
                        title=title,
                        source=source,
                        url=link,
                        published_at=_published_at(entry),
                    )
                )
        except Exception as exc:
            await self._health.record_attempt(
                kind="news_feed",
                provider="rss",
                target=url,
                stage="fetch_parse",
                success=False,
                attempts=1,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_category=classify_provider_error(exc),
                record_system_event=self._record_system_event,
            )
            raise
        await self._health.record_attempt(
            kind="news_feed",
            provider="rss",
            target=url,
            stage="fetch_parse",
            success=True,
            attempts=1,
            duration_ms=round((time.perf_counter() - started) * 1000),
            record_system_event=self._record_system_event,
        )
        return items


def _published_at(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is not None:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC).isoformat()
    raw = str(entry.get("published") or entry.get("updated") or "").strip()
    return raw[:80] or None
