from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.features.provider_health import classify_provider_error
from app.features.video_providers import load_video_proxy, video_proxy_route


DEFAULT_VIDEO_URLS = (
    "https://v.douyin.com/",
    "https://www.iesdouyin.com/",
    "https://www.bilibili.com/",
    "https://b23.tv/",
)


def probe_video_egress(
    urls: list[str],
    *,
    timeout_seconds: float,
    proxy_url: str | None,
) -> dict[str, Any]:
    route = video_proxy_route(proxy_url)
    results = [
        _probe_endpoint(url, timeout_seconds=timeout_seconds, proxy_url=proxy_url)
        for url in urls
    ]
    return {
        "schema_version": 1,
        "route": {
            "mode": route,
            "proxy_endpoint": _proxy_endpoint(proxy_url),
        },
        "targets": results,
        "ok": bool(results) and all(item["ok"] for item in results),
    }


def _probe_endpoint(
    url: str,
    *,
    timeout_seconds: float,
    proxy_url: str | None,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {
            "target": "invalid",
            "ok": False,
            "stages": {
                "input": {"ok": False, "category": "invalid_url", "duration_ms": 0}
            },
        }
    target_host = parsed.hostname.lower()
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    proxy = urlsplit(proxy_url) if proxy_url else None
    connect_host = proxy.hostname if proxy and proxy.hostname else target_host
    connect_port = (
        proxy.port
        if proxy and proxy.port
        else (1080 if proxy and proxy.scheme.startswith("socks") else 8080)
        if proxy
        else target_port
    )
    stages: dict[str, dict[str, Any]] = {}
    stages["dns"] = _timed_stage(lambda: _resolve_host(connect_host))
    stages["tcp"] = _timed_stage(
        lambda: _tcp_connect(connect_host, connect_port, timeout_seconds)
    )
    if proxy_url:
        stages["tls"] = {
            "ok": None,
            "category": "verified_by_routed_http",
            "duration_ms": 0,
        }
    elif parsed.scheme == "https":
        stages["tls"] = _timed_stage(
            lambda: _tls_connect(target_host, target_port, timeout_seconds)
        )
    else:
        stages["tls"] = {"ok": True, "category": "not_required", "duration_ms": 0}
    stages["http"] = _timed_stage(
        lambda: _http_probe(url, timeout_seconds=timeout_seconds, proxy_url=proxy_url)
    )
    if proxy_url and stages["http"]["ok"] and parsed.scheme == "https":
        stages["tls"] = {
            "ok": True,
            "category": "verified_by_routed_http",
            "duration_ms": stages["http"]["duration_ms"],
        }
    if stages["http"]["ok"]:
        for stage_name in ("dns", "tcp"):
            stages[stage_name] = _verified_by_http(
                stages[stage_name],
                duration_ms=stages["http"]["duration_ms"],
            )
        if parsed.scheme == "https":
            stages["tls"] = _verified_by_http(
                stages["tls"],
                duration_ms=stages["http"]["duration_ms"],
            )
    required = [stages["dns"], stages["tcp"], stages["http"]]
    if stages["tls"]["ok"] is not None:
        required.append(stages["tls"])
    return {
        "target": f"{target_host}:{target_port}",
        "ok": all(bool(stage["ok"]) for stage in required),
        "stages": stages,
    }


def _timed_stage(operation) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        detail = operation()
    except Exception as exc:
        return {
            "ok": False,
            "category": classify_provider_error(exc),
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
    result = {
        "ok": True,
        "category": "ok",
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }
    if isinstance(detail, dict):
        result.update(detail)
    return result


def _verified_by_http(
    stage: dict[str, Any],
    *,
    duration_ms: int,
) -> dict[str, Any]:
    if stage.get("ok") is not False:
        return stage
    return {
        "ok": True,
        "category": "verified_by_http",
        "duration_ms": duration_ms,
        "independent_probe": stage,
    }


def _resolve_host(host: str) -> None:
    socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)


def _tcp_connect(host: str, port: int, timeout_seconds: float) -> None:
    with socket.create_connection((host, port), timeout=timeout_seconds):
        return


def _tls_connect(host: str, port: int, timeout_seconds: float) -> None:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout_seconds) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secured:
            secured.getpeercert()


def _http_probe(
    url: str,
    *,
    timeout_seconds: float,
    proxy_url: str | None,
) -> dict[str, Any]:
    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        proxy=proxy_url,
        follow_redirects=False,
        headers={"User-Agent": "QQBotVideoPreflight/1.0", "Range": "bytes=0-0"},
    ) as client:
        response = client.get(url)
    return {"http_status": response.status_code}


def _proxy_endpoint(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    default_port = 1080 if parsed.scheme.startswith("socks") else 8080
    return f"{parsed.hostname}:{parsed.port or default_port}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preflight DNS, TCP, TLS and HTTP egress for video providers."
    )
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--url", action="append", dest="urls")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--require-ok", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    config = load_config(args.config)
    proxy = load_video_proxy(
        http_proxy_env=config.video.http_proxy_env,
        socks_proxy_env=config.video.socks_proxy_env,
    )
    result = probe_video_egress(
        args.urls or list(DEFAULT_VIDEO_URLS),
        timeout_seconds=args.timeout,
        proxy_url=proxy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.require_ok and not result["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
