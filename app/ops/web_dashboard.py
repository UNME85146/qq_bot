"""Private, loopback-only operator dashboard. Never expose OneBot or NapCat directly."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
import httpx


ASSETS = Path(__file__).with_name("dashboard_assets")
COOKIE = "__Host-qqbot-admin"
SESSION_SECONDS = 8 * 3600
MAX_QR_BYTES = 1024 * 1024
QR_FRESH_SECONDS = 180


class DashboardBackend(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...
    def qr_png(self) -> bytes | None: ...
    async def refresh_qr(self) -> bool: ...


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
    def __init__(self, *, qr_path: Path, napcat_config_dir: Path,
                 bot_port: int, disk_path: Path = Path("/"),
                 webui_token_path: Path | None = None,
                 onebot_token_path: Path | None = None,
                 onebot_http_port: int | None = None) -> None:
        self.qr_path = qr_path
        self.napcat_config_dir = napcat_config_dir
        self.bot_port = bot_port
        self.disk_path = disk_path
        self.webui_token_path = webui_token_path
        self.onebot_token_path = onebot_token_path
        self.onebot_http_port = onebot_http_port
        self._refresh_lock = asyncio.Lock()
        self._last_refresh = float("-inf")
        self._last_refresh_success = False

    def qr_png(self) -> bytes | None:
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
            now = time.monotonic()
            if now - self._last_refresh < 60:
                return self._last_refresh_success and self.qr_png() is not None
            self._last_refresh = now
            self._last_refresh_success = False
            try:
                if self.webui_token_path is not None:
                    token = _read_private_token(self.webui_token_path)
                else:
                    config_path = self.napcat_config_dir / "webui.json"
                    info = config_path.lstat()
                    if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
                        return False
                    token = json.loads(config_path.read_text(encoding="utf-8"))["token"]
                if not isinstance(token, str) or len(token) < 12:
                    return False
                password_hash = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
                original = self.qr_png()
                async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                    login = await client.post("http://127.0.0.1:6099/api/auth/login", json={"hash": password_hash})
                    body = login.json()
                    if login.status_code != 200 or body.get("code") != 0:
                        return False
                    credential = body.get("data", {}).get("Credential")
                    if not isinstance(credential, str) or not credential:
                        return False
                    response = await client.post("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode",
                                                 json={}, headers={"Authorization": "Bearer " + credential})
                    if response.status_code != 200 or response.json().get("code") != 0:
                        return False
                for _ in range(30):
                    latest = self.qr_png()
                    if latest is not None and latest != original:
                        self._last_refresh_success = True
                        return True
                    await asyncio.sleep(1)
                return False
            except (OSError, KeyError, TypeError, ValueError, AttributeError, httpx.HTTPError):
                return False

    async def _qq_status(self) -> tuple[bool | None, bool | None]:
        try:
            if self.onebot_token_path is not None:
                port = self.onebot_http_port
                token = _read_private_token(self.onebot_token_path)
            else:
                configs = list(self.napcat_config_dir.glob("onebot11*.json"))
                if len(configs) != 1:
                    return None, None
                config = json.loads(configs[0].read_text(encoding="utf-8"))
                servers = [item for item in config.get("network", {}).get("httpServers", [])
                           if item.get("enable") and item.get("host") in {"127.0.0.1", "localhost"}]
                if len(servers) != 1:
                    return None, None
                port = int(servers[0]["port"])
                token = servers[0].get("token") or ""
            if not 1 <= port <= 65535:
                return None, None
            headers = {"Authorization": "Bearer " + token} if token else {}
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                response = await client.post(f"http://127.0.0.1:{port}/get_status", json={}, headers=headers)
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
        if request.headers.get("origin") != normalized_origin:
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
