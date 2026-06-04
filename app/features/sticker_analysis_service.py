from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from app.model.resilience import ModelResilienceService
from app.models import StickerAsset, StickerAssetAnalysis
from app.storage.repositories import StickerAssetAnalysisRepository

HIGH_RISK_STICKER_CATEGORIES = {"adult", "illegal", "violence", "privacy"}


class StickerAnalysisService:
    def __init__(
        self,
        *,
        repository: StickerAssetAnalysisRepository,
        model_resilience_service: ModelResilienceService,
    ) -> None:
        self._repository = repository
        self._model_resilience_service = model_resilience_service

    async def ensure_analyzed(self, asset: StickerAsset) -> StickerAssetAnalysis | None:
        existing = await self._repository.get(asset.asset_id)
        if existing is not None and existing.analysis_status == "completed":
            return existing
        if existing is None:
            await self._repository.ensure_pending(asset.asset_id)
        return await self.analyze_asset(asset)

    async def get_completed_analysis(self, asset_id: str | None) -> StickerAssetAnalysis | None:
        existing = await self._repository.get(asset_id)
        if existing is None or existing.analysis_status != "completed":
            return None
        return existing

    async def analyze_asset(self, asset: StickerAsset) -> StickerAssetAnalysis | None:
        image_url = _file_to_data_url(asset.file_path)
        if image_url is None:
            return await self._repository.mark_failed(asset_id=asset.asset_id)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 QQ 表情包语义分析器。只输出 JSON，不要 Markdown。"
                    "字段：intent_summary、emotion_tags、scene_tags、text_tags、reply_usage_hint、safety_category。"
                    "safety_category 只能是 safe/adult/illegal/violence/privacy/unknown。"
                    "标签用逗号分隔，保持短、低敏，不要输出图片 URL 或隐私细节。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "分析这个 QQ 表情包适合表达什么情绪、什么聊天场景，"
                            f"已有文本标签：{asset.tags or '无'}。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]
        result = await self._model_resilience_service.generate(messages, scope_type=asset.source_scope_type)
        if result.failure_reason is not None:
            return await self._repository.mark_failed(asset_id=asset.asset_id)
        data = _parse_json(result.reply.raw_model_text or result.reply.text)
        if data is None:
            return await self._repository.mark_failed(asset_id=asset.asset_id)
        safety_category = _normalize_safety(str(data.get("safety_category", "unknown")))
        return await self._repository.upsert_completed(
            asset_id=asset.asset_id,
            intent_summary=_clean_field(data.get("intent_summary", ""), 120),
            emotion_tags=_clean_field(data.get("emotion_tags", ""), 120),
            scene_tags=_clean_field(data.get("scene_tags", ""), 120),
            text_tags=_clean_field(data.get("text_tags", ""), 120),
            reply_usage_hint=_clean_field(data.get("reply_usage_hint", ""), 160),
            safety_category=safety_category,
        )


def _file_to_data_url(file_path: str) -> str | None:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return None
    data = path.read_bytes()
    if not data:
        return None
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _parse_json(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", candidate)
        if not match:
            return None
        candidate = match.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _normalize_safety(value: str) -> str:
    lowered = value.strip().lower()
    for category in ("safe", "adult", "illegal", "violence", "privacy", "unknown"):
        if category in lowered:
            return category
    return "unknown"


def _clean_field(value: Any, max_chars: int) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:，,。. ")
    return text[:max_chars]
