from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.models import MediaItem, NormalizedMessage, QQConfig, StickerAsset
from app.routing.permission_service import PermissionService
from app.safety.safety_service import SafetyService
from app.storage.repositories import StickerAssetAnalysisRepository, StickerAssetRepository


MAX_STICKER_BYTES = 8 * 1024 * 1024
MAX_STICKER_ASSETS = 3500


@dataclass(frozen=True)
class StickerSaveResult:
    asset: StickerAsset | None
    reason: str


class StickerService:
    def __init__(
        self,
        *,
        repository: StickerAssetRepository,
        qq_config: QQConfig,
        root_dir: str | Path,
        safety_service: SafetyService,
        random_fn=None,
        downloader=None,
        image_classifier=None,
        analysis_repository: StickerAssetAnalysisRepository | None = None,
        max_assets: int = MAX_STICKER_ASSETS,
    ) -> None:
        self._repository = repository
        self._permission_service = PermissionService(qq_config)
        self._root_dir = Path(root_dir)
        self._root_dir.mkdir(parents=True, exist_ok=True)
        (self._root_dir / "global").mkdir(parents=True, exist_ok=True)
        self._safety_service = safety_service
        self._random = random_fn or random.random
        self._downloader = downloader or _download_bytes
        self._image_classifier = image_classifier
        self._analysis_repository = analysis_repository
        self._max_assets = max_assets

    async def save_from_message(self, message: NormalizedMessage) -> StickerSaveResult:
        if not self._is_allowed(message):
            return StickerSaveResult(None, "not_allowed")
        if not message.media_items:
            return StickerSaveResult(None, "no_media")
        if message.scope_type != "group":
            if not self._safety_service.can_store_long_term_memory(message.text):
                return StickerSaveResult(None, "sensitive_text")
            input_safety = self._safety_service.check_input(
                message.text,
                scope_type=message.scope_type,
            )
            if input_safety.action == "block":
                return StickerSaveResult(None, f"text_{input_safety.reason}")

        image = _first_sticker_image_with_url(message.media_items)
        if image is None:
            if any(item.type == "image" and item.url for item in message.media_items):
                return StickerSaveResult(None, "not_sticker_media")
            return StickerSaveResult(None, "no_image_url")
        if self._image_classifier is not None and message.scope_type != "group":
            try:
                classification = await self._image_classifier(image.url or "", message.scope_type)
            except Exception:
                classification = "unknown"
            if classification in {"adult", "illegal", "violence", "privacy"}:
                return StickerSaveResult(None, f"image_{classification}")
        try:
            data = await self._downloader(image.url or "")
        except Exception:
            return StickerSaveResult(None, "download_failed")
        if not data:
            return StickerSaveResult(None, "download_failed")
        if len(data) > MAX_STICKER_BYTES:
            return StickerSaveResult(None, "too_large")

        digest = hashlib.sha256(data).hexdigest()
        url_hash = _hash_text(image.url or image.file or digest)
        existing_asset = await self._repository.get_by_asset_id(digest)
        if existing_asset is None:
            existing_asset = await self._repository.get_by_url_hash(url_hash)
        if (
            existing_asset is None
            and self._max_assets >= 0
            and await self._repository.count() >= self._max_assets
        ):
            return StickerSaveResult(None, "library_full")

        suffix = _file_suffix(image.file, image.url)
        sticker_dir = self._root_dir / "global"
        sticker_dir.mkdir(parents=True, exist_ok=True)
        file_path = sticker_dir / f"{digest}{suffix}"
        if not file_path.exists():
            file_path.write_bytes(data)

        tags = ",".join(sorted(extract_sticker_tags(message.text, image)))
        asset = await self._repository.upsert(
            asset_id=digest,
            source_scope_type=message.scope_type,
            source_scope_id=message.scope_id,
            source_user_id=message.user_id,
            source_message_id=message.message_id,
            file_path=str(file_path),
            url_hash=url_hash,
            media_type=image.sub_type or image.type,
            source_file=image.file,
            tags=tags,
            risk_level="safe",
        )
        if self._analysis_repository is not None:
            await self._analysis_repository.ensure_pending(asset.asset_id)
        return StickerSaveResult(asset, "saved")

    async def choose_for_text(self, text: str) -> StickerAsset | None:
        query_tags = sorted(extract_query_tags(text))
        limit = self._max_assets if self._max_assets > 0 else MAX_STICKER_ASSETS
        assets = await self._matching_assets_by_analysis(query_tags, limit=limit)
        if not assets:
            assets = await self._repository.find_matching(query_tags=query_tags, limit=limit)
        if not assets and query_tags == ["default"]:
            assets = await self._repository.find_matching(query_tags=[], limit=limit)
        assets = [asset for asset in assets if Path(asset.file_path).exists()]
        if not assets:
            return None
        index = int(self._random() * len(assets))
        index = max(0, min(index, len(assets) - 1))
        return assets[index]

    async def mark_used(self, asset_id: str) -> None:
        await self._repository.mark_used(asset_id)

    def _is_allowed(self, message: NormalizedMessage) -> bool:
        if message.scope_type == "private":
            return self._permission_service.is_private_user_allowed(message.user_id)
        if message.scope_type == "group" and message.group_id is not None:
            return self._permission_service.is_group_allowed(message.group_id)
        return False

    async def _matching_assets_by_analysis(
        self,
        query_tags: list[str],
        *,
        limit: int,
    ) -> list[StickerAsset]:
        if self._analysis_repository is None:
            return []
        asset_ids = await self._analysis_repository.find_matching_asset_ids(
            query_tags=query_tags,
            limit=limit,
        )
        assets: list[StickerAsset] = []
        for asset_id in asset_ids:
            asset = await self._repository.get_by_asset_id(asset_id)
            if asset is not None and asset.risk_level == "safe":
                assets.append(asset)
        return assets


def extract_sticker_tags(text: str, media: MediaItem | None = None) -> set[str]:
    tags = set(extract_query_tags(text))
    if media is not None:
        for value in (media.summary, media.sub_type, media.file):
            tags.update(extract_query_tags(value or ""))
        if media.type:
            tags.add(media.type)
    if not tags:
        tags.add("default")
    return tags


def extract_query_tags(text: str) -> set[str]:
    lowered = text.lower()
    mapping = {
        "无语": ("无语", "汗", "流汗", "尬"),
        "开心": ("开心", "笑", "哈哈", "乐"),
        "疑惑": ("疑惑", "问号", "啥", "啊?"),
        "震惊": ("震惊", "惊", "离谱"),
        "支持": ("支持", "+1", "加一", "赞"),
        "拒绝": ("不要", "拒绝", "别"),
        "可爱": ("可爱", "狗", "猫", "萌"),
        "黄豆": ("黄豆", "豆"),
        "gif": ("gif", "动图"),
    }
    tags: set[str] = set()
    for tag, words in mapping.items():
        if any(word.lower() in lowered for word in words):
            tags.add(tag)
    if "表情" in text or "图" in text:
        tags.add("default")
    return tags


def is_sticker_request(text: str, *, allow_short: bool = False) -> bool:
    compact = "".join(text.split()).lower()
    if is_sticker_save_request(text):
        return False
    if allow_short and compact in {"表情包", "表情", "图"}:
        return True
    if "表情" in compact and any(marker in compact for marker in ("有吗", "有没有", "有没", "有表情")):
        return True
    return any(
        marker in compact
        for marker in (
            "发个表情包",
            "来个表情包",
            "要个表情包",
            "整个表情包",
            "换个表情包",
            "换一个表情包",
            "随机表情包",
            "发个表情",
            "来个表情",
            "要个表情",
            "整个表情",
            "换个表情",
            "换一个表情",
            "随机表情",
            "发张图",
            "来张图",
            "换张图",
            "换一张图",
            "复读这个表情",
            "复读这个图",
        )
    )


def is_sticker_save_request(text: str) -> bool:
    compact = "".join(text.split()).lower()
    if not any(marker in compact for marker in ("表情", "图")):
        return False
    return any(
        marker in compact
        for marker in (
            "存",
            "保存",
            "存下来",
            "收下",
            "收起来",
            "记下",
            "留着",
        )
    )


def is_sticker_media(item: MediaItem) -> bool:
    marker_text = " ".join(
        value
        for value in (item.sub_type, item.summary, item.file)
        if value
    ).lower()
    markers = (
        "sticker",
        "emoji",
        "face",
        "marketface",
        "mface",
        "表情",
        "动画表情",
        "贴纸",
    )
    return any(marker in marker_text for marker in markers)


def _first_sticker_image_with_url(media_items: tuple[MediaItem, ...]) -> MediaItem | None:
    for item in media_items:
        if item.type == "image" and item.url and is_sticker_media(item):
            return item
    return None


async def _download_bytes(url: str) -> bytes | None:
    if not url.startswith(("http://", "https://")):
        return None
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _file_suffix(file_name: str | None, url: str | None) -> str:
    for value in (file_name, url):
        if not value:
            continue
        suffix = Path(value.split("?", 1)[0]).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            return suffix
    return ".jpg"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()
