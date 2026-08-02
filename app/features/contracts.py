from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class StructuredReply:
    messages: tuple[str, ...]
    page: int = 1
    total_pages: int = 1
    fallback_messages: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return self.page < self.total_pages

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


@dataclass(frozen=True)
class VideoAsset:
    source_url: str
    file_path: str
    title: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class NewsItem:
    category: str
    title: str
    source: str
    url: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class MarketQuote:
    market: str
    symbol: str
    price: float
    source: str
    observed_at: str | None = None
    previous_close: float | None = None
    change_percent: float | None = None
    delayed: bool = True


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    source: str
    snippet: str | None = None


@dataclass(frozen=True)
class ImageAsset:
    file_path: str
    content_type: str | None = None


@dataclass(frozen=True)
class SpeechAsset:
    file_path: str
    format: str


@dataclass(frozen=True)
class FeatureRecognition:
    feature: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureRoute:
    feature: str
    available: bool
    provider_key: str | None = None
    reason: str | None = None


@runtime_checkable
class VideoExtractor(Protocol):
    async def extract(self, source_url: str) -> VideoAsset: ...


@runtime_checkable
class NewsProvider(Protocol):
    async def fetch(self, category: str) -> Sequence[NewsItem]: ...


@runtime_checkable
class MarketDataProvider(Protocol):
    async def quote(self, market: str, symbol: str) -> MarketQuote: ...


@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, query: str) -> Sequence[SearchResult]: ...


@runtime_checkable
class ImageProvider(Protocol):
    async def generate(self, prompt: str) -> ImageAsset: ...

    async def edit(self, image_path: str, prompt: str) -> ImageAsset: ...


@runtime_checkable
class SpeechProvider(Protocol):
    async def synthesize(self, text: str) -> SpeechAsset: ...


@runtime_checkable
class FeatureRouter(Protocol):
    def route(self, recognition: FeatureRecognition) -> FeatureRoute: ...
