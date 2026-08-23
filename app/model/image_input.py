from __future__ import annotations

import asyncio
import base64
import subprocess
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urljoin, urlparse

import httpx

from app.models import MediaItem, NormalizedMessage


MAX_IMAGE_INPUT_BYTES = 8 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 8.0
IMAGE_CONVERSION_TIMEOUT_SECONDS = 8.0
MAX_IMAGE_REDIRECTS = 3
_QQ_CDN_SUFFIXES = ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.cn", "gtimg.com")
_DIRECT_MIME_TYPES = {"image/jpeg", "image/png"}
_CONVERTED_MIME_TYPES = {"image/gif", "image/webp"}


class ImageInputPreparationError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


async def prepare_onebot_image_message(
    bot,
    message: NormalizedMessage,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    converter: Callable[[bytes], bytes] | None = None,
    max_bytes: int = MAX_IMAGE_INPUT_BYTES,
    download_timeout_seconds: float = IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
) -> NormalizedMessage:
    if max_bytes <= 0 or download_timeout_seconds <= 0:
        raise ValueError("image input limits must be positive")
    image_index = next(
        (index for index, item in enumerate(message.media_items) if item.type == "image"),
        None,
    )
    if image_index is None:
        return message
    image = message.media_items[image_index]
    source_url = image.url or ""
    if image.file:
        try:
            async with asyncio.timeout(download_timeout_seconds):
                result = await bot.call_api("get_image", file=image.file)
        except Exception as exc:
            raise ImageInputPreparationError("onebot_get_image_failed") from exc
        data = _onebot_result_data(result)
        source_url = str(data.get("url") or source_url).strip()
    if source_url.startswith("data:image/"):
        return message
    if not _is_trusted_qq_cdn_url(source_url):
        if image.file:
            raise ImageInputPreparationError("untrusted_image_url")
        return message

    payload, mime_type = await _download_image(
        source_url,
        transport=transport,
        timeout_seconds=download_timeout_seconds,
        max_bytes=max_bytes,
    )
    if mime_type in _CONVERTED_MIME_TYPES:
        conversion = converter or _convert_first_frame_to_png
        try:
            payload = await asyncio.to_thread(conversion, payload)
        except ImageInputPreparationError:
            raise
        except Exception as exc:
            raise ImageInputPreparationError("image_conversion_failed") from exc
        mime_type = "image/png"
    if mime_type not in _DIRECT_MIME_TYPES:
        raise ImageInputPreparationError("unsupported_image_type")
    if not payload or len(payload) > max_bytes:
        raise ImageInputPreparationError("image_size_invalid")

    encoded = base64.b64encode(payload).decode("ascii")
    prepared_items = list(message.media_items)
    prepared_items[image_index] = replace(
        image,
        url=f"data:{mime_type};base64,{encoded}",
    )
    return replace(message, media_items=tuple(prepared_items))


def _onebot_result_data(result) -> dict:
    if not isinstance(result, dict):
        return {}
    nested = result.get("data")
    return nested if isinstance(nested, dict) else result


def _is_trusted_qq_cdn_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if parsed.port not in {None, 443}:
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _QQ_CDN_SUFFIXES)


async def _download_image(
    source_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[bytes, str]:
    current_url = source_url
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for redirect_index in range(MAX_IMAGE_REDIRECTS + 1):
            if not _is_trusted_qq_cdn_url(current_url):
                raise ImageInputPreparationError("untrusted_image_url")
            try:
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "").strip()
                        if not location or redirect_index >= MAX_IMAGE_REDIRECTS:
                            raise ImageInputPreparationError("image_redirect_invalid")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code != 200:
                        raise ImageInputPreparationError("image_download_failed")
                    mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if mime_type not in _DIRECT_MIME_TYPES | _CONVERTED_MIME_TYPES:
                        raise ImageInputPreparationError("unsupported_image_type")
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            declared_size = int(declared)
                        except ValueError as exc:
                            raise ImageInputPreparationError(
                                "image_download_failed"
                            ) from exc
                        if declared_size > max_bytes:
                            raise ImageInputPreparationError("image_too_large")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ImageInputPreparationError("image_too_large")
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                    if not payload:
                        raise ImageInputPreparationError("image_empty")
                    return payload, mime_type
            except ImageInputPreparationError:
                raise
            except (httpx.HTTPError, TimeoutError) as exc:
                raise ImageInputPreparationError("image_download_failed") from exc
    raise ImageInputPreparationError("image_redirect_invalid")


def _convert_first_frame_to_png(payload: bytes) -> bytes:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-max_alloc",
                str(64 * 1024 * 1024),
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=w='min(2048,iw)':h='min(2048,ih)':force_original_aspect_ratio=decrease",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            input=payload,
            capture_output=True,
            timeout=IMAGE_CONVERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageInputPreparationError("image_conversion_failed") from exc
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageInputPreparationError("image_conversion_failed")
    return result.stdout
