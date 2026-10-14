"""Private, loopback-only operator dashboard. Never expose OneBot or LLBot directly."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic import ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware
import httpx

from app.config import load_config


ASSETS = Path(__file__).with_name("dashboard_assets")
COOKIE = "__Host-qqbot-admin"
SESSION_SECONDS = 8 * 3600
MAX_QR_BYTES = 1024 * 1024
QR_FRESH_SECONDS = 180


class DashboardBackend(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...
    def qr_png(self) -> bytes | None: ...
    async def refresh_qr(self) -> bool: ...
    async def restart_bot(self) -> dict[str, Any]: ...


class DashboardConfigError(ValueError):
    """A user-correctable configuration error which must not expose secrets."""


class DashboardConfigConflict(DashboardConfigError):
    """The client attempted to overwrite a newer configuration revision."""


class DashboardRestartError(ValueError):
    """The fixed Bot service could not be restarted safely."""


class DashboardRestartInProgress(DashboardRestartError):
    """A restart request is already being handled."""


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=120)
    baseUrl: str | None = Field(default=None, max_length=2048)
    name: str | None = Field(default=None, max_length=256)
    reasoningEffort: str | None = Field(default=None, max_length=32)
    timeoutSeconds: float | None = Field(default=None, gt=0, le=600)
    temperature: float | None = Field(default=None, ge=0, le=2)
    maxTokens: int | None = Field(default=None, gt=0, le=200_000)
    maxTextChars: int | None = Field(default=None, ge=100, le=400_000)
    apiKey: str | None = Field(default=None, max_length=4096)
    apiKeyEnv: str | None = Field(default=None, max_length=128)
    apiKeyConfigured: bool | None = None


class SpeechUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apiMode: str | None = Field(default=None, max_length=64)
    baseUrl: str | None = Field(default=None, max_length=2048)
    apiKeyEnv: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    voice: str | None = Field(default=None, max_length=256)
    format: str | None = Field(default=None, max_length=32)
    timeoutSeconds: float | None = Field(default=None, gt=0, le=600)
    sendTimeoutSeconds: float | None = Field(default=None, gt=0, le=600)
    maxChars: int | None = Field(default=None, gt=0, le=400_000)
    maxAudioBytes: int | None = Field(default=None, gt=0, le=64 * 1024 * 1024)
    apiKey: str | None = Field(default=None, max_length=4096)
    apiKeyConfigured: bool | None = None


class ImageGenerationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseUrl: str | None = Field(default=None, max_length=2048)
    apiKeyEnv: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    generationEndpoint: str | None = Field(default=None, max_length=256)
    editEndpoint: str | None = Field(default=None, max_length=256)
    timeoutSeconds: float | None = Field(default=None, gt=0, le=600)
    sendTimeoutSeconds: float | None = Field(default=None, gt=0, le=600)
    editWindowSeconds: int | None = Field(default=None, ge=0, le=86_400)
    apiKey: str | None = Field(default=None, max_length=4096)
    apiKeyConfigured: bool | None = None


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str | None = Field(default=None, min_length=64, max_length=64)
    model: ModelUpdate = Field(default_factory=ModelUpdate)
    speech: SpeechUpdate | None = None
    imageGeneration: ImageGenerationUpdate | None = None
    features: list[dict[str, Any]] | dict[str, Any] | None = None


FEATURE_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("speech.enabled", "语音回复", "启用文本转语音回复"),
    ("speech.privateEnabled", "私聊语音", "允许私聊使用语音回复"),
    ("speech.groupEnabled", "群聊语音", "允许群聊使用语音回复"),
    ("speech.randomReplyEnabled", "随机语音", "允许随机触发语音回复"),
    ("codexRunway.enabled", "Codex Runway", "发送 Codex Runway 状态摘要"),
    ("usageRankingReport.enabled", "用量排行", "发送每日用量排行报告"),
    ("markets.enabled", "行情查询", "启用股票行情与提醒"),
    ("search.enabled", "联网搜索", "启用信息搜索功能"),
    ("imageGeneration.enabled", "图片生成", "启用图片生成与编辑"),
    ("groupFeatures.chatEnabled", "群聊文字回复", "允许群内提问、@ 和上下文追问的文字回复"),
    ("groupFeatures.imageUnderstandingEnabled", "群聊图片理解", "允许分析群内图片并生成回应"),
    ("groupFeatures.marketsEnabled", "群聊行情查询", "允许群内行情命令和股票预警，需开启全局行情功能"),
    ("groupFeatures.searchEnabled", "群聊联网搜索", "允许群内搜索与评价请求，需开启全局联网搜索"),
    ("imageGeneration.groupEnabled", "群聊生图", "允许群内生成和修改图片，默认关闭，需开启全局图片生成"),
)
FEATURE_PATHS = {key: tuple(key.split(".", 1)) for key, _, _ in FEATURE_DEFINITIONS}
GROUP_FEATURE_KEYS = {key for key in FEATURE_PATHS if key.startswith("groupFeatures.")} | {
    "imageGeneration.groupEnabled", "speech.groupEnabled",
}
FEATURE_DEFAULTS = {
    "speech.privateEnabled": True,
    "speech.groupEnabled": True,
    "speech.randomReplyEnabled": True,
    **{key: True for key in FEATURE_PATHS if key.startswith("groupFeatures.")},
}
ALLOWED_REASONING_EFFORTS = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
TEXT_CHARS_PER_TOKEN = 2
MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 400_000


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


def hash_password(password: str) -> str:
    if len(password) < 16:
        raise ValueError("dashboard password must have at least 16 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt-v1:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        version, salt_hex, digest_hex = encoded.strip().split(":")
        if version != "scrypt-v1" or len(salt_hex) != 32 or len(digest_hex) != 128:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, UnicodeError):
        return False


def read_password_hash(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("dashboard credential file must be a private regular file")
    value = path.read_text(encoding="ascii").strip()
    if not (value.startswith("scrypt-v1:") and len(value) == 171):
        raise ValueError("dashboard credential format is invalid")
    return value


class SystemDashboardBackend:
    def __init__(self, *, qr_path: Path,
                 bot_port: int, disk_path: Path = Path("/"),
                 webui_token_path: Path | None = None,
                 onebot_token_path: Path | None = None,
                 onebot_http_port: int | None = None,
                 config_path: Path | None = None,
                 env_path: Path | None = None,
                 llbot_webui_port: int | None = None,
                 service_name: str = "qq-bot.service") -> None:
        if service_name != "qq-bot.service":
            raise ValueError("unsupported dashboard restart service")
        self.qr_path = qr_path
        self.bot_port = bot_port
        self.disk_path = disk_path
        self.webui_token_path = webui_token_path
        self.onebot_token_path = onebot_token_path
        self.onebot_http_port = onebot_http_port
        self.llbot_webui_port = (
            llbot_webui_port
            if llbot_webui_port is not None
            else int(os.environ.get("QQ_BOT_DASHBOARD_LLBOT_WEBUI_PORT", "3080"))
        )
        self.config_path = config_path
        self.env_path = env_path
        self.service_name = service_name
        self._refresh_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._last_refresh = float("-inf")
        self._last_refresh_success = False
        self._qr_expires_at: float | None = None
        self._restart_required = False
        self._restart_in_progress = False

    def config_snapshot(self) -> dict[str, Any]:
        raw, revision = self._read_config()
        model = raw.get("model")
        if not isinstance(model, dict):
            raise DashboardConfigError("model configuration is invalid")
        api_key_env = _validate_env_name(str(model.get("apiKeyEnv", "")))
        api_key = _read_env_value(self.env_path, api_key_env) or os.environ.get(api_key_env, "")
        speech = raw.get("speech", {})
        if not isinstance(speech, dict):
            raise DashboardConfigError("speech configuration is invalid")
        image_generation = raw.get("imageGeneration", {})
        if not isinstance(image_generation, dict):
            raise DashboardConfigError("imageGeneration configuration is invalid")
        features = []
        for key, label, description in FEATURE_DEFINITIONS:
            features.append({
                "key": key,
                "label": label,
                "description": description,
                "enabled": _feature_value(raw, key),
                "scope": "group" if key in GROUP_FEATURE_KEYS else "global",
            })
        return {
            "model": {
                "provider": str(model.get("provider", "")),
                "baseUrl": str(model.get("baseUrl", "")),
                "name": str(model.get("name", "")),
                "reasoningEffort": str(model.get("reasoningEffort") or ""),
                "timeoutSeconds": float(model.get("timeoutSeconds", 90)),
                "temperature": float(model.get("temperature", 0.8)),
                "maxTokens": int(model.get("maxTokens", 2400)),
                "maxTextChars": _tokens_to_text_chars(int(model.get("maxTokens", 2400))),
                "apiKeyEnv": api_key_env,
                "apiKeyConfigured": bool(api_key),
            },
            "speech": {
                "apiMode": str(speech.get("apiMode", "audio_speech")),
                "baseUrl": str(model.get("baseUrl", "")),
                "apiKeyEnv": api_key_env,
                "model": str(speech.get("model", "")),
                "voice": str(speech.get("voice", "")),
                "format": str(speech.get("format", "mp3")),
                "timeoutSeconds": float(speech.get("timeoutSeconds", 60)),
                "sendTimeoutSeconds": float(speech.get("sendTimeoutSeconds", 60)),
                "maxChars": int(speech.get("maxChars", 4096)),
                "maxAudioBytes": int(speech.get("maxAudioBytes", 8 * 1024 * 1024)),
                "apiKeyConfigured": bool(api_key),
            },
            "imageGeneration": {
                "baseUrl": str(model.get("baseUrl", "")),
                "apiKeyEnv": api_key_env,
                "model": str(image_generation.get("model", "")),
                "generationEndpoint": str(image_generation.get("generationEndpoint", "/images/generations")),
                "editEndpoint": str(image_generation.get("editEndpoint", "/images/edits")),
                "timeoutSeconds": float(image_generation.get("timeoutSeconds", 120)),
                "sendTimeoutSeconds": float(image_generation.get("sendTimeoutSeconds", 60)),
                "editWindowSeconds": int(image_generation.get("editWindowSeconds", 180)),
                "apiKeyConfigured": bool(api_key),
            },
            "features": features,
            "revision": revision,
            "restartRequired": self._restart_required,
        }

    async def update_config(self, request: ConfigUpdateRequest) -> dict[str, Any]:
        async with self._config_lock:
            raw, revision = self._read_config()
            if request.revision is not None and request.revision != revision:
                raise DashboardConfigConflict("configuration changed; reload and retry")
            if not isinstance(raw.get("model"), dict):
                raise DashboardConfigError("model configuration is invalid")

            model_patch = request.model.model_dump(exclude_unset=True)
            model_api_key = model_patch.pop("apiKey", None)
            model_patch.pop("apiKeyConfigured", None)
            max_text_chars = model_patch.pop("maxTextChars", None)
            if max_text_chars is not None:
                model_patch["maxTokens"] = _text_chars_to_tokens(max_text_chars)
            old_api_key_env = str(raw["model"].get("apiKeyEnv", ""))
            api_key_env = model_patch.get("apiKeyEnv", old_api_key_env)
            api_key_env = _validate_env_name(str(api_key_env))
            model_patch["apiKeyEnv"] = api_key_env
            if "baseUrl" in model_patch:
                model_patch["baseUrl"] = _validate_model_base_url(model_patch["baseUrl"])
            if "provider" in model_patch:
                model_patch["provider"] = _required_text(model_patch["provider"], "provider", 120)
            if "name" in model_patch:
                model_patch["name"] = _required_text(model_patch["name"], "name", 256)
            if "reasoningEffort" in model_patch:
                effort = str(model_patch["reasoningEffort"] or "").strip().lower()
                if effort not in ALLOWED_REASONING_EFFORTS:
                    raise DashboardConfigError("reasoningEffort is invalid")
                model_patch["reasoningEffort"] = effort or None
            raw["model"].update(model_patch)

            env_updates: dict[str, str] = {}
            _queue_env_update(env_updates, model_api_key, api_key_env, "model")
            if (model_api_key is None or str(model_api_key) == "") and api_key_env != old_api_key_env:
                old_key = _read_env_value(self.env_path, old_api_key_env) or os.environ.get(old_api_key_env, "")
                _queue_env_update(env_updates, old_key, api_key_env, "model")

            if request.speech is not None:
                speech = raw.get("speech", {})
                if not isinstance(speech, dict):
                    raise DashboardConfigError("speech configuration is invalid")
                speech_patch = request.speech.model_dump(exclude_unset=True)
                speech_api_key = speech_patch.pop("apiKey", None)
                speech_patch.pop("apiKeyConfigured", None)
                speech_patch.pop("baseUrl", None)
                speech_patch.pop("apiKeyEnv", None)
                _normalize_speech_patch(speech_patch)
                speech.update(speech_patch)
                _queue_env_update(env_updates, speech_api_key, api_key_env, "model")

            if request.imageGeneration is not None:
                image_generation = raw.get("imageGeneration", {})
                if not isinstance(image_generation, dict):
                    raise DashboardConfigError("imageGeneration configuration is invalid")
                image_patch = request.imageGeneration.model_dump(exclude_unset=True)
                image_api_key = image_patch.pop("apiKey", None)
                image_patch.pop("apiKeyConfigured", None)
                image_patch.pop("baseUrl", None)
                image_patch.pop("apiKeyEnv", None)
                _normalize_image_generation_patch(image_patch)
                image_generation.update(image_patch)
                _queue_env_update(env_updates, image_api_key, api_key_env, "model")

            # Speech and image generation share the primary model endpoint and key.
            # Keep their runtime config blocks synchronized for older Bot code and
            # for a clean transition from previously separate provider settings.
            speech = raw.setdefault("speech", {})
            image_generation = raw.setdefault("imageGeneration", {})
            if isinstance(speech, dict):
                speech["baseUrl"] = str(raw["model"].get("baseUrl", ""))
                speech["apiKeyEnv"] = str(raw["model"].get("apiKeyEnv", ""))
            if isinstance(image_generation, dict):
                image_generation["baseUrl"] = str(raw["model"].get("baseUrl", ""))
                image_generation["apiKeyEnv"] = str(raw["model"].get("apiKeyEnv", ""))

            _apply_feature_patch(raw, request.features)

            config_bytes = _validated_config_bytes(raw, self._require_config_path().parent)
            env_bytes = _updated_env_bytes_many(self.env_path, env_updates) if env_updates else None
            self._atomic_update(config_bytes, env_bytes)
            self._restart_required = True
            return self.config_snapshot()

    async def restart_bot(self) -> dict[str, Any]:
        if self._restart_in_progress:
            raise DashboardRestartInProgress("bot restart already in progress")
        self._restart_in_progress = True
        try:
            result = await asyncio.to_thread(self._signal_bot_main_pid)
            self._restart_required = False
            return result
        finally:
            self._restart_in_progress = False

    def _signal_bot_main_pid(self) -> dict[str, Any]:
        try:
            service_user = self._systemctl_value("User")
            pid_text = self._systemctl_value("MainPID")
            pid = int(pid_text, 10)
            if pid <= 1 or pid == os.getpid():
                raise DashboardRestartError("bot restart target is unavailable")
            self._verify_restart_identity(service_user, pid)
            os.kill(pid, signal.SIGTERM)
        except DashboardRestartError:
            raise
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            raise DashboardRestartError("bot restart is unavailable") from exc
        return {"status": "restarting", "service": self.service_name}

    def _systemctl_value(self, property_name: str) -> str:
        result = subprocess.run(
            ("systemctl", "show", self.service_name, f"--property={property_name}", "--value"),
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            raise DashboardRestartError("bot restart is unavailable")
        value = result.stdout.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise DashboardRestartError("bot restart target is unavailable")
        return value

    @staticmethod
    def _verify_restart_identity(service_user: str, pid: int) -> None:
        # The dashboard and Bot run as the same unprivileged account in production.
        # On Unix, verify both the account mapping and the live process UID before signaling.
        if os.name == "nt":
            if service_user != getpass.getuser():
                raise DashboardRestartError("bot restart permission mismatch")
            return
        try:
            import pwd
            service_uid = pwd.getpwnam(service_user).pw_uid
            current_uid = os.getuid()
            if service_uid != current_uid:
                raise DashboardRestartError("bot restart permission mismatch")
            status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            process_uid = int(uid_line.split()[1], 10)
            if process_uid != service_uid:
                raise DashboardRestartError("bot restart permission mismatch")
        except DashboardRestartError:
            raise
        except (ImportError, KeyError, OSError, StopIteration, ValueError, IndexError) as exc:
            raise DashboardRestartError("bot restart target is unavailable") from exc

    async def model_list(self, *, base_url: str | None = None,
                         api_key: str | None = None) -> list[str]:
        raw, _ = self._read_config()
        model = raw.get("model")
        if not isinstance(model, dict):
            raise DashboardConfigError("model configuration is invalid")
        target = _validate_model_base_url(base_url or str(model.get("baseUrl", "")))
        if api_key is None:
            env_name = _validate_env_name(str(model.get("apiKeyEnv", "")))
            api_key = _read_env_value(self.env_path, env_name) or os.environ.get(env_name, "")
        elif api_key:
            api_key = _validate_api_key(api_key)
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.get(target + "/models", headers=headers)
                if response.headers.get("content-length") and int(response.headers["content-length"]) > MAX_MODEL_RESPONSE_BYTES:
                    raise DashboardConfigError("model list response is too large")
                body = response.content
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if isinstance(exc, DashboardConfigError):
                raise
            raise DashboardConfigError("model provider unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise DashboardConfigError("model provider rejected the request")
        if len(body) > MAX_MODEL_RESPONSE_BYTES:
            raise DashboardConfigError("model list response is too large")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardConfigError("model provider returned invalid JSON") from exc
        candidates = decoded.get("data") if isinstance(decoded, dict) else decoded
        if not isinstance(candidates, list):
            raise DashboardConfigError("model provider returned an invalid model list")
        result: set[str] = set()
        for item in candidates:
            value = item.get("id") if isinstance(item, dict) else item
            if isinstance(value, str) and 0 < len(value.strip()) <= 256 and not any(ord(ch) < 32 for ch in value):
                result.add(value.strip())
        if not result:
            raise DashboardConfigError("model provider returned no usable models")
        return sorted(result)

    def _read_config(self) -> tuple[dict[str, Any], str]:
        path = self._require_config_path()
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise DashboardConfigError("configuration file must be a regular file")
            data = path.read_bytes()
            raw = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardConfigError("configuration file is unavailable") from exc
        if not isinstance(raw, dict):
            raise DashboardConfigError("configuration file must contain an object")
        return raw, hashlib.sha256(data).hexdigest()

    def _require_config_path(self) -> Path:
        if self.config_path is None:
            raise DashboardConfigError("dashboard configuration editing is unavailable")
        return self.config_path

    def _atomic_update(self, config_bytes: bytes, env_bytes: bytes | None) -> None:
        config_path = self._require_config_path()
        original_config = config_path.read_bytes()
        original_mode = stat.S_IMODE(config_path.stat().st_mode)
        env_path = self.env_path
        original_env = None
        original_env_mode = 0o600
        if env_bytes is not None and env_path is not None and env_path.exists():
            original_env = env_path.read_bytes()
            original_env_mode = stat.S_IMODE(env_path.stat().st_mode)
        try:
            _atomic_write(config_path, config_bytes, original_mode)
            if env_bytes is not None:
                if env_path is None:
                    raise DashboardConfigError("environment file path is unavailable")
                _atomic_write(env_path, env_bytes, original_env_mode)
        except Exception as exc:
            try:
                _atomic_write(config_path, original_config, original_mode)
                if env_bytes is not None and env_path is not None:
                    if original_env is None:
                        try:
                            env_path.unlink()
                        except FileNotFoundError:
                            pass
                    else:
                        _atomic_write(env_path, original_env, original_env_mode)
            except OSError:
                pass
            if isinstance(exc, DashboardConfigError):
                raise
            raise DashboardConfigError("configuration update failed") from exc


def _required_text(value: str, field_name: str, max_length: int) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > max_length or any(ord(ch) < 32 for ch in normalized):
        raise DashboardConfigError(f"{field_name} is invalid")
    return normalized


def _optional_text(value: Any, field_name: str, max_length: int) -> str:
    if value is None:
        raise DashboardConfigError(f"{field_name} is invalid")
    normalized = str(value).strip()
    if len(normalized) > max_length or any(ord(ch) < 32 for ch in normalized):
        raise DashboardConfigError(f"{field_name} is invalid")
    return normalized


def _validate_optional_env_name(value: Any) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    if not normalized:
        return ""
    return _validate_env_name(normalized)


def _validate_optional_base_url(value: Any, field_name: str) -> str:
    if value is None:
        raise DashboardConfigError(f"{field_name} is invalid")
    normalized = str(value).strip()
    if not normalized:
        return ""
    try:
        return _validate_model_base_url(normalized)
    except DashboardConfigError as exc:
        raise DashboardConfigError(f"{field_name} must be a public HTTPS URL") from exc


def _validate_endpoint_path(value: Any, field_name: str) -> str:
    normalized = _required_text(str(value), field_name, 256)
    if (
        not normalized.startswith("/")
        or normalized.startswith("//")
        or "\\" in normalized
        or "://" in normalized
        or ".." in normalized.split("/")
        or "?" in normalized
        or "#" in normalized
    ):
        raise DashboardConfigError(f"{field_name} is invalid")
    return normalized


def _normalize_speech_patch(patch: dict[str, Any]) -> None:
    if "apiMode" in patch:
        mode = _required_text(patch["apiMode"], "speech.apiMode", 64).lower()
        if mode not in {"audio_speech", "chat_completions_audio"}:
            raise DashboardConfigError("speech.apiMode is invalid")
        patch["apiMode"] = mode
    if "baseUrl" in patch:
        patch["baseUrl"] = _validate_optional_base_url(patch["baseUrl"], "speech.baseUrl")
    if "model" in patch:
        patch["model"] = _optional_text(patch["model"], "speech.model", 256)
    if "voice" in patch:
        patch["voice"] = _optional_text(patch["voice"], "speech.voice", 256)
    if "format" in patch:
        audio_format = _required_text(patch["format"], "speech.format", 32).lower()
        if audio_format not in {"mp3", "opus", "aac", "flac", "wav", "pcm", "pcm16"}:
            raise DashboardConfigError("speech.format is invalid")
        patch["format"] = audio_format


def _normalize_image_generation_patch(patch: dict[str, Any]) -> None:
    if "baseUrl" in patch:
        patch["baseUrl"] = _validate_optional_base_url(
            patch["baseUrl"], "imageGeneration.baseUrl"
        )
    if "model" in patch:
        patch["model"] = _optional_text(patch["model"], "imageGeneration.model", 256)
    if "generationEndpoint" in patch:
        patch["generationEndpoint"] = _validate_endpoint_path(
            patch["generationEndpoint"], "imageGeneration.generationEndpoint"
        )
    if "editEndpoint" in patch:
        patch["editEndpoint"] = _validate_endpoint_path(
            patch["editEndpoint"], "imageGeneration.editEndpoint"
        )


def _queue_env_update(updates: dict[str, str], value: Any, env_name: str, owner: str) -> None:
    if value is None or str(value) == "":
        return
    if not env_name:
        raise DashboardConfigError(f"{owner}.apiKeyEnv is required when apiKey is supplied")
    normalized = _validate_api_key(str(value))
    existing = updates.get(env_name)
    if existing is not None and existing != normalized:
        raise DashboardConfigError("multiple provider API keys target the same environment variable")
    updates[env_name] = normalized


def _text_chars_to_tokens(value: int) -> int:
    if not MIN_TEXT_CHARS <= value <= MAX_TEXT_CHARS:
        raise DashboardConfigError("maxTextChars is invalid")
    return max(1, (value + TEXT_CHARS_PER_TOKEN - 1) // TEXT_CHARS_PER_TOKEN)


def _tokens_to_text_chars(value: int) -> int:
    return max(MIN_TEXT_CHARS, min(MAX_TEXT_CHARS, value * TEXT_CHARS_PER_TOKEN))


def _validate_env_name(value: str) -> str:
    normalized = value.strip()
    if not ENV_NAME_RE.fullmatch(normalized):
        raise DashboardConfigError("apiKeyEnv is invalid")
    return normalized


def _validate_api_key(value: str) -> str:
    if not value or len(value) > 4096 or any(ch in value for ch in "\r\n"):
        raise DashboardConfigError("apiKey is invalid")
    return value


def _validate_model_base_url(value: str) -> str:
    normalized = str(value).strip().rstrip("/")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DashboardConfigError("baseUrl is invalid") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(ch) < 32 for ch in normalized)
        or (port is not None and not 1 <= port <= 65535)
        or hostname in {"localhost", "localhost.localdomain"}
        or hostname.endswith(".local")
    ):
        raise DashboardConfigError("baseUrl must be a public HTTPS URL")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_unspecified or address.is_reserved
    ):
        raise DashboardConfigError("baseUrl must not target a private address")
    return normalized


def _feature_value(raw: dict[str, Any], key: str) -> bool:
    block_name, field_name = FEATURE_PATHS[key]
    block = raw.get(block_name, {})
    return bool(block.get(field_name, FEATURE_DEFAULTS.get(key, False))) if isinstance(block, dict) else False


def _apply_feature_patch(raw: dict[str, Any], features: list[dict[str, Any]] | dict[str, Any] | None) -> None:
    if features is None:
        return
    values: dict[str, Any] = {}
    if isinstance(features, dict):
        values = features
    elif isinstance(features, list):
        for item in features:
            if not isinstance(item, dict) or set(item) - {"key", "enabled", "label", "description", "scope"} or "key" not in item or "enabled" not in item:
                raise DashboardConfigError("features contains an invalid item")
            key = item["key"]
            if key in values:
                raise DashboardConfigError("features contains a duplicate key")
            values[key] = item["enabled"]
    else:
        raise DashboardConfigError("features must be an object or array")
    for key, enabled in values.items():
        if key not in FEATURE_PATHS or type(enabled) is not bool:
            raise DashboardConfigError("features contains an invalid value")
        block_name, field_name = FEATURE_PATHS[key]
        block = raw.setdefault(block_name, {})
        if not isinstance(block, dict):
            raise DashboardConfigError(f"{block_name} configuration is invalid")
        block[field_name] = enabled


def _validated_config_bytes(raw: dict[str, Any], config_dir: Path) -> bytes:
    payload = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    # Keep the temporary file beside the repository config so relative persona paths
    # resolve exactly as they do for the live file.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config_dir, prefix=".qqbot-config-", suffix=".json", delete=False
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        load_config(temp_path)
        return payload.encode("utf-8")
    except DashboardConfigError:
        raise
    except Exception as exc:
        raise DashboardConfigError("configuration failed validation") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _read_env_value(path: Path | None, name: str) -> str:
    if path is None or not path.exists():
        return ""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
    return ""


def _updated_env_bytes(path: Path | None, name: str, value: str) -> bytes:
    return _updated_env_bytes_many(path, {name: value})


def _updated_env_bytes_many(path: Path | None, updates: dict[str, str]) -> bytes:
    if path is None:
        raise DashboardConfigError("environment file path is unavailable")
    if not updates:
        return path.read_bytes() if path.exists() else b""
    for name, value in updates.items():
        _validate_env_name(name)
        _validate_api_key(value)
    if path.exists():
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise DashboardConfigError("environment file must be a regular file")
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise DashboardConfigError("environment file is unavailable") from exc
    else:
        text = ""
    lines = text.splitlines(keepends=True)
    patterns = {
        name: re.compile(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=")
        for name in updates
    }
    replaced: set[str] = set()
    output: list[str] = []
    for line in lines:
        matching_name = next((name for name, pattern in patterns.items() if pattern.match(line)), None)
        if matching_name is not None:
            if matching_name not in replaced:
                value = updates[matching_name]
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                replacement = f"{matching_name}='{escaped}'"
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                output.append(replacement + ending)
                replaced.add(matching_name)
            continue
        output.append(line)
    missing = [name for name in updates if name not in replaced]
    if missing:
        if output and not output[-1].endswith(("\n", "\r")):
            output.append("\n")
        for name in missing:
            escaped = updates[name].replace("\\", "\\\\").replace("'", "\\'")
            output.append(f"{name}='{escaped}'\n")
    return "".join(output).encode("utf-8")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise DashboardConfigError("target file must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode & 0o777 or 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class _RuntimeDashboardMethods:
    def qr_png(self) -> bytes | None:
        if self._qr_expires_at is not None and time.time() >= self._qr_expires_at:
            return None
        try:
            info = self.qr_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size < 24
                    or info.st_size > MAX_QR_BYTES or not 0 <= time.time() - info.st_mtime <= QR_FRESH_SECONDS):
                return None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(self.qr_path, flags), "rb") as stream:
                current = os.fstat(stream.fileno())
                if current.st_ino != info.st_ino or current.st_dev != info.st_dev or not stat.S_ISREG(current.st_mode):
                    return None
                data = stream.read(MAX_QR_BYTES + 1)
            return data if len(data) <= MAX_QR_BYTES and data.startswith(b"\x89PNG\r\n\x1a\n") else None
        except OSError:
            return None

    async def refresh_qr(self) -> bool:
        async with self._refresh_lock:
            if time.monotonic() - self._last_refresh < 2:
                return self._last_refresh_success and self.qr_png() is not None
            return await self._refresh_llbot_qr()

    async def _refresh_llbot_qr(self) -> bool:
        self._last_refresh = time.monotonic()
        self._last_refresh_success = False
        try:
            token = _read_private_token(self.webui_token_path) if self.webui_token_path else ""
            if not token:
                return False
            webui_credential = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not 1 <= self.llbot_webui_port <= 65535:
                return False
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{self.llbot_webui_port}/api/login-qrcode",
                    headers={"X-Webui-Token": webui_credential},
                )
            if response.status_code != 200:
                return False
            payload = response.json()
            encoded = ((payload.get("data") or {}).get("pngBase64QrcodeData"))
            if not payload.get("success") or not isinstance(encoded, str):
                return False
            if encoded.startswith("data:image/png;base64,"):
                encoded = encoded.split(",", 1)[1]
            data = base64.b64decode(encoded, validate=True)
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_QR_BYTES:
                return False
            lifetime = (payload.get("data") or {}).get("expireTime", 120)
            if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not 0 < lifetime <= QR_FRESH_SECONDS:
                return False
            self._qr_expires_at = time.time() + lifetime
            _atomic_write(self.qr_path, data, 0o600)
            # Encode actual remaining lifetime in mtime so restart cannot extend it.
            created_at = self._qr_expires_at - QR_FRESH_SECONDS
            os.utime(self.qr_path, (created_at, created_at))
            self._last_refresh_success = True
            return True
        except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError, base64.binascii.Error):
            return False

    async def _llbot_status_webui(self) -> tuple[bool | None, bool | None] | None:
        """Read LLBot's authoritative login state before probing OneBot."""
        try:
            token = _read_private_token(self.webui_token_path) if self.webui_token_path else ""
            if not token or not 1 <= self.llbot_webui_port <= 65535:
                return None
            webui_credential = hashlib.sha256(token.encode("utf-8")).hexdigest()
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{self.llbot_webui_port}/api/login-info",
                    headers={"X-Webui-Token": webui_credential},
                )
                if response.status_code != 200:
                    return None
                result = response.json()
            data = result.get("data", {})
            online = data.get("online") if isinstance(data, dict) else None
            if not result.get("success") or not isinstance(online, bool):
                return None
            return online, online
        except (OSError, ValueError, TypeError, KeyError, AttributeError, httpx.HTTPError):
            return None

    async def _qq_status(self) -> tuple[bool | None, bool | None]:
        webui_status = await self._llbot_status_webui()
        if webui_status is not None:
            return webui_status
        try:
            port = self.onebot_http_port
            if port is None or not 1 <= port <= 65535 or self.onebot_token_path is None:
                return None, None
            token = _read_private_token(self.onebot_token_path)
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                response = await client.post(f"http://127.0.0.1:{port}/get_status", json={},
                                             headers={"Authorization": "Bearer " + token})
                result = response.json()
            if response.status_code != 200 or result.get("retcode") != 0:
                return None, None
            data = result.get("data", {})
            online, good = data.get("online"), data.get("good")
            return (online if isinstance(online, bool) else None,
                    good if isinstance(good, bool) else None)
        except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError, AttributeError):
            return None, None

    async def snapshot(self) -> dict[str, Any]:
        qq, resources, running = await asyncio.gather(
            self._qq_status(), asyncio.to_thread(self._resources), asyncio.to_thread(self._bot_running))
        online, good = qq
        return {
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "qq": {"online": online, "good": good, "state": "online" if online and good is not False else
                   "degraded" if online else
                   "login_required" if online is False or self.qr_png() is not None else "unavailable"},
            "bot": {"running": running}, "system": resources,
        }

    def _bot_running(self) -> bool:
        try:
            result = subprocess.run(("systemctl", "is-active", "qq-bot.service"),
                                    capture_output=True, text=True, timeout=3, check=False)
            if result.returncode != 0 or result.stdout.strip() != "active":
                return False
            with socket.create_connection(("127.0.0.1", self.bot_port), timeout=0.5):
                return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _resources(self) -> dict[str, int | float | None]:
        try:
            memory = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    memory[key] = int(value.split()[0]) * 1024
            total = memory["MemTotal"]
            used = total - memory["MemAvailable"]
            cpu = self._cpu_percent()
            disk = shutil.disk_usage(self.disk_path)
            return {"cpuPercent": cpu, "memoryTotal": total, "memoryUsed": used,
                    "diskTotal": disk.total, "diskUsed": disk.used}
        except (OSError, ValueError, KeyError):
            return {"cpuPercent": None, "memoryTotal": None, "memoryUsed": None,
                    "diskTotal": None, "diskUsed": None}

    @staticmethod
    def _cpu_percent() -> float | None:
        def sample() -> tuple[int, int]:
            columns = [int(item) for item in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            return sum(columns), columns[3] + columns[4]

        try:
            before_total, before_idle = sample()
            time.sleep(0.15)
            total, idle = sample()
            ticks = total - before_total
            return round(100 * (1 - (idle - before_idle) / ticks), 1) if ticks > 0 else None
        except (OSError, ValueError, IndexError, ZeroDivisionError):
            return None


SystemDashboardBackend.qr_png = _RuntimeDashboardMethods.qr_png
SystemDashboardBackend._refresh_llbot_qr = _RuntimeDashboardMethods._refresh_llbot_qr
SystemDashboardBackend._llbot_status_webui = _RuntimeDashboardMethods._llbot_status_webui
SystemDashboardBackend.refresh_qr = _RuntimeDashboardMethods.refresh_qr
SystemDashboardBackend._qq_status = _RuntimeDashboardMethods._qq_status
SystemDashboardBackend.snapshot = _RuntimeDashboardMethods.snapshot
SystemDashboardBackend._bot_running = _RuntimeDashboardMethods._bot_running
SystemDashboardBackend._resources = _RuntimeDashboardMethods._resources
SystemDashboardBackend._cpu_percent = staticmethod(_RuntimeDashboardMethods._cpu_percent)


def create_dashboard_app(*, origin: str, password_hash: str, backend: DashboardBackend) -> FastAPI:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query:
        raise ValueError("dashboard origin must be an HTTPS host")
    if not password_hash.startswith("scrypt-v1:"):
        raise ValueError("dashboard password hash missing")
    normalized_origin = f"https://{parsed.netloc}"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])
    app.mount("/static", StaticFiles(directory=ASSETS), name="static")
    sessions: dict[str, float] = {}
    failures: deque[float] = deque()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def authenticated(request: Request) -> str:
        token = request.cookies.get(COOKIE, "")
        key = hashlib.sha256(token.encode()).hexdigest() if token else ""
        expiry = sessions.get(key, 0)
        if not expiry or expiry < time.monotonic():
            sessions.pop(key, None)
            raise HTTPException(401, "login required")
        return key

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin == normalized_origin:
            return
        # Some embedded browsers omit Origin on same-origin POST requests while
        # still sending an exact same-origin Referer. Accept that narrow case;
        # any cross-origin value remains rejected.
        if not origin:
            referer = request.headers.get("referer", "")
            parsed_referer = urlsplit(referer)
            referer_origin = (
                f"{parsed_referer.scheme}://{parsed_referer.netloc}"
                if parsed_referer.scheme and parsed_referer.netloc
                else ""
            )
            if referer_origin == normalized_origin:
                return
        raise HTTPException(403, "same-origin request required")

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        try:
            authenticated(request)
            return RedirectResponse("/", status_code=303)
        except HTTPException:
            return FileResponse(ASSETS / "login.html", media_type="text/html")

    @app.get("/", include_in_schema=False)
    async def dashboard(request: Request):
        try:
            authenticated(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(ASSETS / "dashboard.html", media_type="text/html")

    @app.post("/api/login")
    async def login(request: Request, payload: LoginRequest):
        same_origin(request)
        now = time.monotonic()
        while failures and failures[0] < now - 600:
            failures.popleft()
        if len(failures) >= 5:
            raise HTTPException(429, "login temporarily unavailable")
        if not verify_password(payload.password, password_hash):
            failures.append(now)
            raise HTTPException(401, "invalid credentials")
        failures.clear()
        token = secrets.token_urlsafe(32)
        sessions[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_SECONDS
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True,
                            httponly=True, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        same_origin(request)
        sessions.pop(authenticated(request), None)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/status")
    async def status(request: Request):
        authenticated(request)
        return await backend.snapshot()

    @app.get("/api/qq/qr")
    async def qq_qr(request: Request):
        authenticated(request)
        if (await backend.snapshot())["qq"]["online"] is True:
            raise HTTPException(409, "QQ already online")
        data = backend.qr_png()
        if data is None:
            raise HTTPException(404, "no fresh QR available")
        return Response(data, media_type="image/png")

    @app.post("/api/qq/refresh")
    async def qq_refresh(request: Request):
        same_origin(request)
        authenticated(request)
        if (await backend.snapshot())["qq"]["online"] is True:
            raise HTTPException(409, "QQ already online")
        if not await backend.refresh_qr():
            raise HTTPException(503, "QR refresh unavailable")
        return JSONResponse({"refreshing": True}, status_code=202)

    @app.post("/api/bot/restart")
    async def bot_restart(request: Request):
        same_origin(request)
        authenticated(request)
        restarter = getattr(backend, "restart_bot", None)
        if restarter is None:
            raise HTTPException(503, "Bot restart unavailable")
        try:
            result = await restarter()
            return JSONResponse(result, status_code=202)
        except DashboardRestartInProgress as exc:
            raise HTTPException(409, str(exc)) from exc
        except DashboardRestartError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/config")
    async def config_get(request: Request):
        authenticated(request)
        reader = getattr(backend, "config_snapshot", None)
        if reader is None:
            raise HTTPException(503, "configuration editing unavailable")
        try:
            return JSONResponse(reader())
        except DashboardConfigError as exc:
            raise HTTPException(503, str(exc)) from exc

    async def config_save(request: Request, payload: ConfigUpdateRequest):
        same_origin(request)
        authenticated(request)
        writer = getattr(backend, "update_config", None)
        if writer is None:
            raise HTTPException(503, "configuration editing unavailable")
        try:
            return JSONResponse(await writer(payload))
        except DashboardConfigConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except DashboardConfigError as exc:
            raise HTTPException(400, str(exc)) from exc

    app.post("/api/config")(config_save)
    app.put("/api/config")(config_save)

    @app.post("/api/models")
    async def model_list(request: Request, payload: dict[str, Any] | None = None):
        same_origin(request)
        authenticated(request)
        fetcher = getattr(backend, "model_list", None)
        if fetcher is None:
            raise HTTPException(503, "model listing unavailable")
        payload = payload or {}
        if set(payload) - {"baseUrl", "apiKey"}:
            raise HTTPException(422, "unsupported model listing fields")
        base_url = payload.get("baseUrl")
        api_key = payload.get("apiKey")
        if base_url is not None and not isinstance(base_url, str):
            raise HTTPException(422, "baseUrl must be a string")
        if api_key is not None and not isinstance(api_key, str):
            raise HTTPException(422, "apiKey must be a string")
        try:
            models = await fetcher(base_url=base_url or None, api_key=api_key)
            return JSONResponse({"models": models})
        except DashboardConfigError as exc:
            raise HTTPException(502, str(exc)) from exc

    return app


def _read_private_token(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
        raise ValueError("private token file permissions are unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as stream:
        current = os.fstat(stream.fileno())
        if current.st_dev != info.st_dev or current.st_ino != info.st_ino or current.st_size > 4096:
            raise ValueError("private token file changed")
        token = stream.read(4097).strip()
    if not token or len(token) > 4096 or any(ch.isspace() for ch in token):
        raise ValueError("private token format is invalid")
    return token
