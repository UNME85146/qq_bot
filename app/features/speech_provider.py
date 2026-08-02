from __future__ import annotations

import os
import re
from pathlib import Path

import httpx

from app.features.contracts import SpeechAsset
from app.models import SpeechConfig


class SpeechProviderError(RuntimeError):
    def __init__(self, category: str, *, retryable: bool) -> None:
        self.category = category
        self.retryable = retryable
        super().__init__(category)


class OpenAICompatibleSpeechProvider:
    def __init__(
        self,
        config: SpeechConfig,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._transport = transport

    async def synthesize(
        self,
        text: str,
        *,
        timeout_seconds: float,
        request_id: str,
    ) -> SpeechAsset:
        cache_dir = Path(self._config.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_id).strip("-")
        output_path = cache_dir / f"speech-{safe_request_id}.{self._config.format}"
        temporary_path = output_path.with_suffix(output_path.suffix + ".part")
        payload = {
            "model": self._config.model,
            "input": text,
            "voice": self._config.voice,
            "response_format": self._config.format,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": request_id,
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=self._transport,
        ) as client:
            response = await client.post(
                f"{self._config.base_url}/audio/speech",
                json=payload,
                headers=headers,
            )
        if response.is_error:
            raise _classify_response_error(response.status_code)
        if not response.content:
            raise SpeechProviderError("empty_audio", retryable=True)
        temporary_path.write_bytes(response.content)
        temporary_path.replace(output_path)
        return SpeechAsset(file_path=str(output_path), format=self._config.format)

    async def cleanup(self, asset: SpeechAsset) -> None:
        path = Path(asset.file_path)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)


def create_speech_provider(config: SpeechConfig):
    if not config.enabled or not config.base_url or not config.model or not config.voice:
        return None
    api_key = os.getenv(config.api_key_env) if config.api_key_env else None
    if not api_key:
        return None
    return OpenAICompatibleSpeechProvider(config, api_key)


def _classify_response_error(status_code: int) -> SpeechProviderError:
    if status_code in {401, 403}:
        return SpeechProviderError("authentication", retryable=False)
    if status_code == 404:
        return SpeechProviderError("capability_unsupported", retryable=False)
    if status_code in {400, 409, 422}:
        return SpeechProviderError("model_or_parameter_unsupported", retryable=False)
    if status_code == 429:
        return SpeechProviderError("rate_limited", retryable=True)
    if status_code >= 500:
        return SpeechProviderError("provider_unavailable", retryable=True)
    return SpeechProviderError("request_failed", retryable=False)
