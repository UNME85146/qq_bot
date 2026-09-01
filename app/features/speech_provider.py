from __future__ import annotations

import base64
import binascii
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

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
                speech_endpoint_url(self._config),
                json=payload,
                headers=headers,
            )
        if response.is_error:
            raise _classify_response_error(response.status_code)
        if not response.content:
            raise SpeechProviderError("empty_audio", retryable=True)
        if len(response.content) > self._config.max_audio_bytes:
            raise SpeechProviderError("audio_too_large", retryable=False)
        return _write_audio_asset(
            self._config,
            request_id=request_id,
            audio_bytes=response.content,
        )

    async def cleanup(self, asset: SpeechAsset) -> None:
        path = Path(asset.file_path)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)


class OpenAIChatAudioSpeechProvider:
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
        payload = {
            "model": self._config.model,
            "modalities": ["text", "audio"],
            "audio": {
                "voice": self._config.voice,
                "format": self._config.format,
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是逐字朗读器。逐字朗读用户提供的文本，不得回答、解释、"
                        "改写、增删或补充任何内容。"
                    ),
                },
                {"role": "user", "content": text},
            ],
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
                speech_endpoint_url(self._config),
                json=payload,
                headers=headers,
            )
        if response.is_error:
            raise _classify_response_error(response.status_code)
        response_payload = _response_json(response)
        audio_payload = _chat_audio_payload(response_payload)
        audio_data = audio_payload.get("data")
        if audio_data == "":
            raise SpeechProviderError("empty_audio", retryable=False)
        if not isinstance(audio_data, str):
            raise SpeechProviderError("invalid_audio_response", retryable=False)
        max_encoded_length = 4 * ((self._config.max_audio_bytes + 2) // 3)
        if len(audio_data) > max_encoded_length:
            raise SpeechProviderError("audio_too_large", retryable=False)
        try:
            audio_bytes = base64.b64decode(audio_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SpeechProviderError(
                "invalid_audio_response",
                retryable=False,
            ) from exc
        if not audio_bytes:
            raise SpeechProviderError("empty_audio", retryable=False)
        if len(audio_bytes) > self._config.max_audio_bytes:
            raise SpeechProviderError("audio_too_large", retryable=False)
        if not _audio_signature_matches(audio_bytes, self._config.format):
            raise SpeechProviderError("invalid_audio_format", retryable=False)
        transcript = audio_payload.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise SpeechProviderError("invalid_audio_response", retryable=False)
        if _normalized_spoken_text(transcript) != _normalized_spoken_text(text):
            raise SpeechProviderError("transcript_mismatch", retryable=False)
        observed_model = response_payload.get("model")
        asset = _write_audio_asset(
            self._config,
            request_id=request_id,
            audio_bytes=audio_bytes,
        )
        return SpeechAsset(
            file_path=asset.file_path,
            format=asset.format,
            transcript=transcript,
            provider_model=(
                observed_model.strip()
                if isinstance(observed_model, str) and observed_model.strip()
                else None
            ),
        )

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
    if config.api_mode == "chat_completions_audio":
        return OpenAIChatAudioSpeechProvider(config, api_key)
    return OpenAICompatibleSpeechProvider(config, api_key)


def speech_endpoint_path(config: SpeechConfig) -> str:
    if config.api_mode == "chat_completions_audio":
        return "/chat/completions"
    return "/audio/speech"


def speech_endpoint_url(config: SpeechConfig) -> str:
    return f"{config.base_url.rstrip('/')}{speech_endpoint_path(config)}"


def _write_audio_asset(
    config: SpeechConfig,
    *,
    request_id: str,
    audio_bytes: bytes,
) -> SpeechAsset:
    cache_dir = Path(config.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_id).strip("-")
    safe_request_id = safe_request_id or "request"
    output_path = cache_dir / f"speech-{safe_request_id}.{config.format}"
    temporary_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        temporary_path.write_bytes(audio_bytes)
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return SpeechAsset(file_path=str(output_path), format=config.format)


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpeechProviderError("invalid_audio_response", retryable=False) from exc
    if not isinstance(payload, dict):
        raise SpeechProviderError("invalid_audio_response", retryable=False)
    return payload


def _chat_audio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise SpeechProviderError("invalid_audio_response", retryable=False)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise SpeechProviderError("invalid_audio_response", retryable=False)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise SpeechProviderError("invalid_audio_response", retryable=False)
    audio = message.get("audio")
    if not isinstance(audio, dict):
        raise SpeechProviderError("invalid_audio_response", retryable=False)
    return audio


def _normalized_spoken_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith(("P", "Z", "C"))
    )


def _audio_signature_matches(audio_bytes: bytes, audio_format: str) -> bool:
    if audio_format == "wav":
        return (
            len(audio_bytes) >= 12
            and audio_bytes.startswith(b"RIFF")
            and audio_bytes[8:12] == b"WAVE"
        )
    if audio_format == "mp3":
        return audio_bytes.startswith(b"ID3") or (
            len(audio_bytes) >= 2
            and audio_bytes[0] == 0xFF
            and audio_bytes[1] & 0xE0 == 0xE0
        )
    if audio_format == "flac":
        return audio_bytes.startswith(b"fLaC")
    if audio_format == "opus":
        return audio_bytes.startswith(b"OggS")
    return audio_format in {"pcm", "pcm16"}


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
