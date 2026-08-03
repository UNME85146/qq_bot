from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.features.contracts import VideoAsset


class VideoExtractorError(RuntimeError):
    def __init__(self, category: str, *, retryable: bool) -> None:
        self.category = category
        self.retryable = retryable
        super().__init__(category)


@dataclass
class _ExtractorLockEntry:
    lock: asyncio.Lock
    users: int = 0


class YtDlpVideoExtractor:
    def __init__(
        self,
        cache_dir: str | Path,
        *,
        http_proxy_env: str = "QQ_BOT_VIDEO_HTTP_PROXY",
        socks_proxy_env: str = "QQ_BOT_VIDEO_SOCKS_PROXY",
        cookie_file_env: str = "QQ_BOT_VIDEO_COOKIE_FILE",
        environ: Mapping[str, str] | None = None,
        redirect_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._locks: dict[str, _ExtractorLockEntry] = {}
        self._locks_guard = asyncio.Lock()
        self._proxy_url = load_video_proxy(
            http_proxy_env=http_proxy_env,
            socks_proxy_env=socks_proxy_env,
            environ=environ,
        )
        self._cookie_file = load_video_cookie_file(
            cookie_file_env=cookie_file_env,
            environ=environ,
        )
        self._redirect_transport = redirect_transport

    @property
    def proxy_route(self) -> str:
        return video_proxy_route(self._proxy_url)

    async def resolve_url(self, source_url: str) -> str:
        hostname = (urlsplit(source_url).hostname or "").lower()
        if hostname not in {"v.douyin.com", "b23.tv"}:
            return source_url
        if self.proxy_route == "socks":
            return await self._resolve_with_ytdlp(source_url)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(20.0),
                proxy=self._proxy_url,
                transport=self._redirect_transport,
                headers={"User-Agent": _user_agent_for(source_url)},
            ) as client:
                response = await client.get(source_url)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise VideoExtractorError("network_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise VideoExtractorError("network_error", retryable=True) from exc
        canonical = str(response.url)
        return canonical if canonical.startswith(("http://", "https://")) else source_url

    async def extract(self, source_url: str) -> VideoAsset:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        async with self._source_lock(source_url):
            prefix = self._cache_dir / _url_key(source_url)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "yt_dlp",
                *_proxy_arguments(self._proxy_url),
                *_cookie_arguments(self._cookie_file),
                *_impersonation_arguments(source_url),
                "--no-playlist",
                "--no-progress",
                "--no-warnings",
                "--continue",
                "--retries",
                "0",
                "--fragment-retries",
                "0",
                "--extractor-retries",
                "0",
                "--socket-timeout",
                "30",
                "--format",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "--output",
                f"{prefix}.%(ext)s",
                "--print",
                'after_move:{"filepath":%(filepath)j,"title":%(title)j}',
                source_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await process.communicate()
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            if process.returncode != 0:
                raise _map_process_error(stderr.decode("utf-8", errors="replace"))
            payload = _last_json_object(stdout.decode("utf-8", errors="replace"))
            file_path = Path(str(payload.get("filepath", "")))
            if not file_path.is_file():
                raise VideoExtractorError("download_output_missing", retryable=True)
            return VideoAsset(
                source_url=source_url,
                file_path=str(file_path),
                title=_clean_title(payload.get("title")),
                provider=_provider_label(source_url),
            )

    @asynccontextmanager
    async def _source_lock(self, source_url: str) -> AsyncIterator[None]:
        async with self._locks_guard:
            entry = self._locks.get(source_url)
            if entry is None:
                entry = _ExtractorLockEntry(asyncio.Lock())
                self._locks[source_url] = entry
            entry.users += 1
        try:
            await entry.lock.acquire()
        except BaseException:
            async with self._locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._locks.get(source_url) is entry:
                    self._locks.pop(source_url, None)
            raise
        try:
            yield
        finally:
            entry.lock.release()
            async with self._locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._locks.get(source_url) is entry:
                    self._locks.pop(source_url, None)

    async def _resolve_with_ytdlp(self, source_url: str) -> str:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yt_dlp",
            *_proxy_arguments(self._proxy_url),
            *_cookie_arguments(self._cookie_file),
            *_impersonation_arguments(source_url),
            "--simulate",
            "--no-warnings",
            "--socket-timeout",
            "20",
            "--print",
            "webpage_url",
            source_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            raise _map_process_error(stderr.decode("utf-8", errors="replace"))
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            candidate = line.strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
        raise VideoExtractorError("canonical_url_missing", retryable=True)

    async def cleanup(self, source_url: str) -> None:
        prefix = _url_key(source_url)
        await asyncio.to_thread(_remove_matching_files, self._cache_dir, prefix)


def _last_json_object(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("filepath"):
            return payload
    raise VideoExtractorError("download_metadata_missing", retryable=True)


def _map_process_error(stderr: str) -> VideoExtractorError:
    normalized = " ".join(stderr.lower().split())
    if "no module named yt_dlp" in normalized:
        return VideoExtractorError("dependency_missing", retryable=False)
    if "unsupported url" in normalized:
        return VideoExtractorError("unsupported", retryable=False)
    cookie_markers = (
        "fresh cookies",
        "cookies are needed",
        "cookies are required",
    )
    if any(marker in normalized for marker in cookie_markers):
        return VideoExtractorError("cookies_required", retryable=False)
    timeout_markers = (
        "timed out",
        "timeout",
        "connection timeout",
        "read operation timed out",
    )
    if any(marker in normalized for marker in timeout_markers):
        return VideoExtractorError("network_timeout", retryable=True)
    network_markers = (
        "network is unreachable",
        "connection refused",
        "temporary failure in name resolution",
        "name or service not known",
        "connection reset",
    )
    if any(marker in normalized for marker in network_markers):
        return VideoExtractorError("network_error", retryable=True)
    permanent_markers = (
        "drm protected",
        "login required",
        "sign in to confirm",
        "private video",
        "members-only",
        "premium-only",
        "not available in your country",
    )
    if any(marker in normalized for marker in permanent_markers):
        return VideoExtractorError("access_denied", retryable=False)
    return VideoExtractorError("download_failed", retryable=True)


def load_video_proxy(
    *,
    http_proxy_env: str,
    socks_proxy_env: str,
    environ: dict[str, str] | None = None,
) -> str | None:
    values = os.environ if environ is None else environ
    socks_proxy = str(values.get(socks_proxy_env, "") or "").strip()
    http_proxy = str(values.get(http_proxy_env, "") or "").strip()
    selected = socks_proxy or http_proxy
    if not selected:
        return None
    parsed = urlsplit(selected)
    supported = {"socks4", "socks4a", "socks5", "socks5h"} if socks_proxy else {"http", "https"}
    if parsed.scheme.lower() not in supported or not parsed.hostname:
        raise ValueError("video proxy URL is invalid or uses an unsupported scheme")
    return selected


def load_video_cookie_file(
    *,
    cookie_file_env: str,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    values = os.environ if environ is None else environ
    configured = str(values.get(cookie_file_env, "") or "").strip()
    if not configured:
        return None

    try:
        candidate = Path(configured).expanduser()
        metadata = candidate.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "video cookie file must be a readable private regular file"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("video cookie file must be a readable private regular file")
    if os.name == "posix" and not _is_private_cookie_mode(metadata.st_mode):
        raise ValueError("video cookie file must be a readable private regular file")
    if not os.access(candidate, os.R_OK):
        raise ValueError("video cookie file must be a readable private regular file")
    return candidate


def _is_private_cookie_mode(mode: int) -> bool:
    return bool(mode & stat.S_IRUSR) and not bool(
        mode & (stat.S_IRWXG | stat.S_IRWXO)
    )


def video_proxy_route(proxy_url: str | None) -> str:
    if not proxy_url:
        return "direct"
    return "socks" if urlsplit(proxy_url).scheme.lower().startswith("socks") else "http"


def _proxy_arguments(proxy_url: str | None) -> tuple[str, ...]:
    return ("--proxy", proxy_url) if proxy_url else ()


def _cookie_arguments(cookie_file: Path | None) -> tuple[str, ...]:
    return ("--cookies", os.fspath(cookie_file)) if cookie_file else ()


def _impersonation_arguments(source_url: str) -> tuple[str, ...]:
    hostname = (urlsplit(source_url).hostname or "").lower()
    douyin_hosts = ("douyin.com", "iesdouyin.com")
    if any(
        hostname == host or hostname.endswith(f".{host}") for host in douyin_hosts
    ):
        return ("--impersonate", "chrome")
    return ()


def _user_agent_for(source_url: str) -> str:
    hostname = (urlsplit(source_url).hostname or "").lower()
    if "douyin" in hostname:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
    return "Mozilla/5.0 QQBotVideo/1.0"


def _url_key(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:24]


def _remove_matching_files(cache_dir: Path, prefix: str) -> None:
    if not cache_dir.exists():
        return
    for path in cache_dir.glob(f"{prefix}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _provider_label(source_url: str) -> str:
    return "抖音" if "douyin" in source_url.lower() else "B站"


def _clean_title(value) -> str | None:
    title = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:120] or None
