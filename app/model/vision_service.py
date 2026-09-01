from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import time
from typing import Any

from app.model.resilience import ModelResilienceService
from app.models import GeneratedReply, MediaItem


HIGH_RISK_IMAGE_CATEGORIES = {"adult", "illegal", "violence", "privacy"}
KNOWN_IMAGE_CATEGORIES = HIGH_RISK_IMAGE_CATEGORIES | {"safe", "unknown"}


@dataclass(frozen=True)
class ImageAnalysisResult:
    action: str
    category: str
    reply: GeneratedReply | None = None
    model_called: bool = False
    failure_reason: str | None = None


class ImageUnderstandingService:
    def __init__(
        self,
        *,
        model_resilience_service: ModelResilienceService,
        classification_cache_ttl_seconds: float = 300.0,
        classification_cache_limit: int = 256,
        request_timeout_seconds: float = 18.0,
        reasoning_effort: str | None = None,
    ) -> None:
        if classification_cache_ttl_seconds <= 0:
            raise ValueError("classification_cache_ttl_seconds must be positive")
        if classification_cache_limit <= 0:
            raise ValueError("classification_cache_limit must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._model_resilience_service = model_resilience_service
        self._classification_cache_ttl_seconds = classification_cache_ttl_seconds
        self._classification_cache_limit = classification_cache_limit
        self._request_timeout_seconds = request_timeout_seconds
        self._reasoning_effort = reasoning_effort
        self._classification_cache: dict[str, tuple[float, str]] = {}
        self._classification_inflight: dict[
            str,
            asyncio.Task[ImageAnalysisResult],
        ] = {}

    async def analyze(
        self,
        *,
        user_text: str,
        media_items: tuple[MediaItem, ...],
        scope_type: str,
        style_system_prompt: str,
    ) -> ImageAnalysisResult | None:
        image = first_image_with_url(media_items)
        if image is None:
            return None

        if scope_type != "group":
            safety = await self._classify_image(image.url or "", scope_type=scope_type)
            if safety.category in HIGH_RISK_IMAGE_CATEGORIES:
                return ImageAnalysisResult(
                    action="refuse",
                    category=safety.category,
                    model_called=safety.model_called,
                    failure_reason=safety.failure_reason,
                )
            if safety.category != "safe":
                reply = GeneratedReply(
                    text=_image_unavailable_text(scope_type),
                    raw_model_text=_image_unavailable_text(scope_type),
                    model_name="vision",
                    finish_reason=safety.failure_reason or safety.category,
                )
                return ImageAnalysisResult(
                    action="reply",
                    category=safety.category,
                    reply=reply,
                    model_called=safety.model_called,
                    failure_reason=safety.failure_reason,
                )

        prompt_text = (
            "请分析这张 QQ 聊天图片或表情包的大概含义、主要情绪和聊天意图。"
            "只输出 JSON，不要 Markdown，不要输出图片安全分类。"
            "JSON 只使用 reply_text、send_sticker、sticker_intent 三个字段："
            "reply_text 是找不到合适表情包或媒体发送失败时才显示的一句中文短文本；"
            "send_sticker 固定为 true；"
            "sticker_intent 用 1-3 个简短中文情绪或回应标签描述适合回什么表情包。"
            f"\n用户附带文字：{user_text or '无'}"
        )
        messages = [
            {"role": "system", "content": style_system_prompt},
            {"role": "user", "content": _multimodal_content(prompt_text, image.url or "")},
        ]
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                result = await self._model_resilience_service.generate(
                    messages,
                    scope_type=scope_type,
                    max_tokens=320,
                    reasoning_effort=self._reasoning_effort,
                )
        except TimeoutError:
            unavailable = _image_unavailable_text(scope_type)
            return ImageAnalysisResult(
                action="reply",
                category="unknown",
                reply=GeneratedReply(
                    text=unavailable,
                    raw_model_text=unavailable,
                    model_name="vision",
                    finish_reason="timeout",
                ),
                model_called=True,
                failure_reason="timeout",
            )
        reply = result.reply
        category = "safe"
        if result.failure_reason is not None:
            unavailable = _image_unavailable_text(scope_type)
            reply = GeneratedReply(
                text=unavailable,
                raw_model_text=unavailable,
                model_name="vision",
                finish_reason=result.failure_reason,
            )
            category = "unknown"
        return ImageAnalysisResult(
            action="reply",
            category=category,
            reply=reply,
            model_called=result.model_called,
            failure_reason=result.failure_reason,
        )

    async def _classify_image(self, image_url: str, *, scope_type: str) -> ImageAnalysisResult:
        cache_key = _classification_cache_key(image_url)
        cached = self._cached_classification(cache_key)
        if cached is not None:
            return cached
        task = self._classification_inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._classify_image_uncached(image_url, scope_type=scope_type)
            )
            self._classification_inflight[cache_key] = task
            task.add_done_callback(
                lambda completed, key=cache_key: self._finish_classification_task(
                    key,
                    completed,
                )
            )
        return await asyncio.shield(task)

    async def _classify_image_uncached(
        self,
        image_url: str,
        *,
        scope_type: str,
    ) -> ImageAnalysisResult:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是图片安全分类器。只输出一个英文标签："
                    "safe、adult、illegal、violence、privacy、unknown。"
                    "不要解释。"
                ),
            },
            {
                "role": "user",
                "content": _multimodal_content("请分类这张图片。", image_url),
            },
        ]
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                result = await self._model_resilience_service.generate(
                    messages,
                    scope_type=scope_type,
                    max_tokens=16,
                    reasoning_effort=self._reasoning_effort,
                )
        except TimeoutError:
            return ImageAnalysisResult(
                action="classify",
                category="unknown",
                model_called=True,
                failure_reason="timeout",
            )
        category = _normalize_category(result.reply.raw_model_text or result.reply.text)
        if result.failure_reason is not None:
            category = "unknown"
        else:
            cache_key = _classification_cache_key(image_url)
            self._classification_cache[cache_key] = (time.monotonic(), category)
            if len(self._classification_cache) > self._classification_cache_limit:
                oldest = min(
                    self._classification_cache,
                    key=lambda key: self._classification_cache[key][0],
                )
                self._classification_cache.pop(oldest, None)
        return ImageAnalysisResult(
            action="classify",
            category=category,
            reply=result.reply,
            model_called=result.model_called,
            failure_reason=result.failure_reason,
        )

    def _cached_classification(self, cache_key: str) -> ImageAnalysisResult | None:
        cached = self._classification_cache.get(cache_key)
        if cached is None:
            return None
        cached_at, cached_category = cached
        if time.monotonic() - cached_at >= self._classification_cache_ttl_seconds:
            self._classification_cache.pop(cache_key, None)
            return None
        return ImageAnalysisResult(
            action="classify",
            category=cached_category,
            model_called=False,
        )

    def _finish_classification_task(
        self,
        cache_key: str,
        completed: asyncio.Task[ImageAnalysisResult],
    ) -> None:
        if self._classification_inflight.get(cache_key) is completed:
            self._classification_inflight.pop(cache_key, None)


def first_image_with_url(media_items: tuple[MediaItem, ...]) -> MediaItem | None:
    for item in media_items:
        if item.type == "image" and item.url:
            return item
    return None


def has_media(media_items: tuple[MediaItem, ...]) -> bool:
    return any(item.type in {"image", "face"} for item in media_items)


def _multimodal_content(text: str, image_url: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]


def _normalize_category(text: str) -> str:
    lowered = text.strip().lower()
    for category in KNOWN_IMAGE_CATEGORIES:
        if category in lowered:
            return category
    return "unknown"


def _classification_cache_key(image_url: str) -> str:
    return hashlib.sha256(image_url.encode("utf-8", errors="surrogatepass")).hexdigest()


def _image_unavailable_text(scope_type: str) -> str:
    if scope_type == "group":
        return "图裂了 我看不了"
    return "这图我这边看不了，你直接说内容吧"
