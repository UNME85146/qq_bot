from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import re
from pathlib import Path

import httpx

from app.features.contracts import ImageAsset
from app.models import ImageGenerationConfig


class ImageProviderError(RuntimeError):
    def __init__(self, category: str, *, retryable: bool) -> None:
        self.category = category
        self.retryable = retryable
        super().__init__(category)


class OpenAICompatibleImageProvider:
    def __init__(
        self,
        config: ImageGenerationConfig,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._transport = transport

    async def generate(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        request_id: str,
    ) -> ImageAsset:
        response = await self._post(
            self._config.generation_endpoint,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
            json={"model": self._config.model, "prompt": prompt, "n": 1},
        )
        return self._write_response(response, request_id=request_id, operation="generated")

    async def edit(
        self,
        image_path: str,
        prompt: str,
        *,
        timeout_seconds: float,
        request_id: str,
    ) -> ImageAsset:
        source = Path(image_path)
        if not source.is_file():
            raise ImageProviderError("source_image_missing", retryable=False)
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        response = await self._post(
            self._config.edit_endpoint,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
            data={"model": self._config.model, "prompt": prompt},
            files={"image": (source.name, source.read_bytes(), content_type)},
        )
        return self._write_response(response, request_id=request_id, operation="edited")

    async def cleanup(self, asset: ImageAsset) -> None:
        path = Path(asset.file_path)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)

    async def _post(self, endpoint: str, *, timeout_seconds: float, request_id: str, **kwargs):
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": request_id,
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=self._transport,
        ) as client:
            response = await client.post(
                _endpoint_url(self._config.base_url, endpoint),
                headers=headers,
                **kwargs,
            )
        if response.is_error:
            raise _classify_response_error(response)
        return response

    def _write_response(
        self,
        response: httpx.Response,
        *,
        request_id: str,
        operation: str,
    ) -> ImageAsset:
        try:
            payload = response.json()
            encoded = payload["data"][0]["b64_json"]
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error) as exc:
            raise ImageProviderError("invalid_response", retryable=True) from exc
        if not image_bytes:
            raise ImageProviderError("empty_image", retryable=True)

        cache_dir = Path(self._config.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_id).strip("-")
        output_path = cache_dir / f"image-{safe_request_id or 'no-trace'}-{operation}.png"
        temporary_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            temporary_path.write_bytes(image_bytes)
            temporary_path.replace(output_path)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise ImageProviderError("cache_write_failed", retryable=False) from exc
        return ImageAsset(file_path=str(output_path), content_type="image/png")


def create_image_provider(config: ImageGenerationConfig):
    if not config.enabled or not config.base_url or not config.model:
        return None
    api_key = os.getenv(config.api_key_env) if config.api_key_env else None
    if not api_key:
        return None
    return OpenAICompatibleImageProvider(config, api_key)


def cleanup_stale_image_cache(config: ImageGenerationConfig) -> int:
    cache_dir = Path(config.cache_dir)
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for pattern in (
        "image-*-generated.png",
        "image-*-generated.png.part",
        "image-*-edited.png",
        "image-*-edited.png.part",
    ):
        for path in cache_dir.glob(pattern):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            removed += 1
    return removed


def _endpoint_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def _classify_response_error(response: httpx.Response) -> ImageProviderError:
    status_code = response.status_code
    if status_code in {401, 403}:
        return ImageProviderError("authentication", retryable=False)
    if status_code == 404:
        return ImageProviderError("capability_unsupported", retryable=False)
    if status_code in {400, 409, 422}:
        error_text = _response_error_text(response)
        if any(
            marker in error_text
            for marker in ("content_policy", "safety", "moderation", "policy violation")
        ):
            return ImageProviderError("safety_rejected", retryable=False)
        return ImageProviderError("model_or_parameter_unsupported", retryable=False)
    if status_code == 429:
        return ImageProviderError("rate_limited", retryable=True)
    if status_code >= 500:
        return ImageProviderError("provider_unavailable", retryable=True)
    return ImageProviderError("request_failed", retryable=False)


def _response_error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return ""
    return " ".join(
        str(error.get(field, "")).lower() for field in ("code", "type", "message")
    )
