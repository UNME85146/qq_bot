from __future__ import annotations

from dataclasses import dataclass
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
    def __init__(self, *, model_resilience_service: ModelResilienceService) -> None:
        self._model_resilience_service = model_resilience_service

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

        safety = await self._classify_image(image.url or "", scope_type=scope_type)
        if safety.category in HIGH_RISK_IMAGE_CATEGORIES:
            return ImageAnalysisResult(
                action="silence" if scope_type == "group" else "refuse",
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
            "请看这张 QQ 群聊图片，只用中文短句回复。"
            "不要输出图片安全分类，不要描述任何隐私细节。"
            f"\n用户附带文字：{user_text or '无'}"
        )
        messages = [
            {"role": "system", "content": style_system_prompt},
            {"role": "user", "content": _multimodal_content(prompt_text, image.url or "")},
        ]
        result = await self._model_resilience_service.generate(messages, scope_type=scope_type)
        return ImageAnalysisResult(
            action="reply",
            category="safe",
            reply=result.reply,
            model_called=result.model_called,
            failure_reason=result.failure_reason,
        )

    async def _classify_image(self, image_url: str, *, scope_type: str) -> ImageAnalysisResult:
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
        result = await self._model_resilience_service.generate(messages, scope_type=scope_type)
        category = _normalize_category(result.reply.raw_model_text or result.reply.text)
        if result.failure_reason is not None:
            category = "unknown"
        return ImageAnalysisResult(
            action="classify",
            category=category,
            reply=result.reply,
            model_called=result.model_called,
            failure_reason=result.failure_reason,
        )


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


def _image_unavailable_text(scope_type: str) -> str:
    if scope_type == "group":
        return "图裂了 我看不了"
    return "这图我这边看不了，你直接说内容吧"
