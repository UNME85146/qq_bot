from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import mimetypes
import os
import re
import socket
from pathlib import Path

import httpx

from app.features.contracts import ImageAsset
from app.models import ImageGenerationConfig


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_BASE64_IMAGE_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)


class ImageProviderError(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
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
        return await self._write_response(response, request_id=request_id, operation="generated")

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
        return await self._write_response(response, request_id=request_id, operation="edited")

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

    async def _write_response(
        self,
        response: httpx.Response,
        *,
        request_id: str,
        operation: str,
    ) -> ImageAsset:
        try:
            payload = response.json()
            item = payload["data"][0]
            encoded = item.get("b64_json") if isinstance(item, dict) else None
            image_url = item.get("url") if isinstance(item, dict) else None
            if encoded:
                image_bytes = _decode_image_base64(encoded)
                content_type = _detect_image_type(image_bytes)[0]
            elif isinstance(image_url, str) and image_url:
                image_bytes, content_type = await self._download_image_url(image_url)
            else:
                raise ValueError("image response has no b64_json or url")
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error, httpx.HTTPError) as exc:
            # A 2xx response may already have consumed a paid generation;
            # malformed output is not safe to replay automatically.
            raise ImageProviderError("invalid_response", retryable=False) from exc
        if not image_bytes:
            raise ImageProviderError("empty_image", retryable=False)

        cache_dir = Path(self._config.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_id).strip("-")
        extension = mimetypes.guess_extension(content_type) or ".png"
        output_path = cache_dir / f"image-{safe_request_id or 'no-trace'}-{operation}{extension}"
        temporary_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            temporary_path.write_bytes(image_bytes)
            temporary_path.replace(output_path)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise ImageProviderError("cache_write_failed", retryable=False) from exc
        return ImageAsset(file_path=str(output_path), content_type=content_type)

    async def _download_image_url(self, image_url: str) -> tuple[bytes, str]:
        parsed = httpx.URL(image_url)
        if not _is_public_https_url(parsed):
            raise ValueError("image URL must use HTTPS")
        if self._transport is None and not await _resolves_to_public_host(parsed.host):
            raise ValueError("image URL resolves to a non-public host")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._config.timeout_seconds),
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            async with client.stream("GET", image_url) as response:
                if response.is_redirect or response.is_informational:
                    raise ValueError("image URL redirects are not allowed")
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_IMAGE_BYTES:
                    raise ValueError("image response is too large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ValueError("image response is too large")
                    chunks.append(chunk)
                body = b"".join(chunks)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                detected_type, _ = _detect_image_type(body)
                if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                    content_type = detected_type
                return body, content_type


def _decode_image_base64(encoded: object) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > MAX_BASE64_IMAGE_CHARS:
        raise ValueError("image response is too large")
    image_bytes = base64.b64decode(encoded, validate=True)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("image response is too large")
    return image_bytes


def _detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("unsupported image format")


def _is_public_https_url(parsed: httpx.URL) -> bool:
    if parsed.scheme != "https" or not parsed.host:
        return False
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        return False
    host = parsed.host.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # DNS names are resolved by the transport. Reject special-use names;
        # public provider/CDN hostnames remain allowed.
        return not host.endswith((".internal", ".lan", ".home.arpa"))
    return address.is_global


async def _resolves_to_public_host(host: str) -> bool:
    try:
        addresses = await asyncio.to_thread(_resolve_host_addresses, host)
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


def _resolve_host_addresses(host: str) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return tuple({str(info[4][0]) for info in infos if info[4]})


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
    for stem in ("generated", "edited"):
        for extension in ("png", "jpg", "jpeg", "webp"):
            for suffix in (f".{extension}", f".{extension}.part"):
                for path in cache_dir.glob(f"image-*{stem}{suffix}"):
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
    error_text = _response_error_text(response)
    if _is_no_compatible_account_error(error_text):
        return ImageProviderError(
            "upstream_no_compatible_account",
            retryable=False,
            status_code=status_code,
        )
    if status_code in {401, 403}:
        return ImageProviderError("authentication", retryable=False, status_code=status_code)
    if status_code == 404:
        return ImageProviderError("capability_unsupported", retryable=False, status_code=status_code)
    if status_code in {400, 409, 422}:
        if any(
            marker in error_text
            for marker in ("content_policy", "safety", "moderation", "policy violation")
        ):
            return ImageProviderError("safety_rejected", retryable=False, status_code=status_code)
        return ImageProviderError(
            "model_or_parameter_unsupported",
            retryable=False,
            status_code=status_code,
        )
    if status_code == 429:
        return ImageProviderError("rate_limited", retryable=True, status_code=status_code)
    if status_code >= 500:
        # A 5xx can arrive after the provider accepted a billable request.
        # Do not replay image generation or edit requests automatically.
        return ImageProviderError("provider_unavailable", retryable=False, status_code=status_code)
    return ImageProviderError("request_failed", retryable=False, status_code=status_code)


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


def _is_no_compatible_account_error(error_text: str) -> bool:
    return any(
        marker in error_text
        for marker in (
            "no available compatible account",
            "no compatible account",
            "no_available_compatible_account",
            "no_compatible_account",
        )
    )
