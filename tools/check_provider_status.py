from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import feedparser
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.features.market_providers import (
    SinaMarketProvider,
)
from app.features.provider_health import (
    DEFAULT_PROVIDER_STATUS_PATH,
    ProviderHealthRegistry,
    classify_provider_error,
    sanitize_provider_target,
)
from app.features.search_providers import (
    BraveSearchProvider,
    SearxngSearchProvider,
    WikipediaSearchProvider,
)
from app.features.video_providers import load_video_proxy


DEFAULT_VIDEO_TARGETS = (
    "https://v.douyin.com/",
    "https://www.douyin.com/",
    "https://www.iesdouyin.com/",
    "https://www.bilibili.com/",
    "https://b23.tv/",
)

@dataclass(frozen=True)
class ProviderProbeSpec:
    kind: str
    provider: str
    target: str
    stage: str
    timeout_seconds: float
    operation: Callable[[], Awaitable[None]]


class ProviderProbeError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def build_provider_probe_specs(
    config,
    *,
    timeout_seconds: float,
    environ: Mapping[str, str] | None = None,
) -> list[ProviderProbeSpec]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    values = os.environ if environ is None else environ
    specs: list[ProviderProbeSpec] = []

    if config.news.enabled:
        for category, urls in config.news.feeds.items():
            for url in dict.fromkeys(urls):
                specs.append(
                    ProviderProbeSpec(
                        kind="news_feed",
                        provider="rss",
                        target=url,
                        stage="fetch_parse",
                        timeout_seconds=timeout_seconds,
                        operation=_rss_probe_operation(url, timeout_seconds),
                    )
                )

    if config.markets.enabled:
        market_configs = (
            ("a_share", config.markets.a_share),
            *(("a_share", item) for item in config.markets.a_share_fallbacks),
            ("us_share", config.markets.us_share),
        )
        for market, provider_config in market_configs:
            name = str(provider_config.provider or "").strip().lower()
            if not name:
                continue
            target = _market_target(name, str(provider_config.base_url or ""))
            specs.append(
                ProviderProbeSpec(
                    kind="market",
                    provider=name,
                    target=target,
                    stage="quote",
                    timeout_seconds=timeout_seconds,
                    operation=_market_probe_operation(
                        market,
                        name,
                        str(provider_config.base_url or ""),
                        timeout_seconds,
                    ),
                )
            )

    if config.search.enabled:
        provider = str(config.search.provider or "").strip().lower()
        base_url = str(config.search.base_url or "").strip()
        if provider:
            specs.append(
                ProviderProbeSpec(
                    kind="search",
                    provider=provider,
                    target=base_url or f"{provider}://configured",
                    stage="search",
                    timeout_seconds=timeout_seconds,
                    operation=_search_probe_operation(
                        provider,
                        base_url,
                        str(config.search.api_key_env or ""),
                        timeout_seconds,
                        values,
                    ),
                )
            )

    if config.video.enabled:
        proxy_url = load_video_proxy(
            http_proxy_env=config.video.http_proxy_env,
            socks_proxy_env=config.video.socks_proxy_env,
            environ=dict(values),
        )
        for target in DEFAULT_VIDEO_TARGETS:
            specs.append(
                ProviderProbeSpec(
                    kind="video",
                    provider="video",
                    target=target,
                    stage="http_egress",
                    timeout_seconds=timeout_seconds,
                    operation=_http_probe_operation(
                        target,
                        timeout_seconds,
                        proxy_url=proxy_url,
                        user_agent="QQBotProviderProbe/1.0",
                    ),
                )
            )

    unique: dict[tuple[str, str, str], ProviderProbeSpec] = {}
    for spec in specs:
        key = (spec.kind, spec.provider, sanitize_provider_target(spec.target))
        unique.setdefault(key, spec)
    return list(unique.values())


async def run_provider_probes(
    specs: list[ProviderProbeSpec],
    *,
    status_path: str | Path,
) -> dict[str, Any]:
    results = await asyncio.gather(*(_run_provider_probe(spec) for spec in specs))
    providers = {key: value for key, value in sorted(results)}
    healthy = bool(providers) and all(
        item["state"] == "healthy" for item in providers.values()
    )
    return ProviderHealthRegistry(status_path).replace_snapshot(
        providers,
        mode="active_probe",
        healthy=healthy,
    )


async def _run_provider_probe(
    spec: ProviderProbeSpec,
) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    error_category = None
    try:
        await asyncio.wait_for(spec.operation(), timeout=spec.timeout_seconds)
    except Exception as exc:
        error_category = _classify_probe_error(exc)
    duration_ms = round((time.perf_counter() - started) * 1000)
    safe_kind = _safe_label(spec.kind)
    safe_provider = _safe_label(spec.provider)
    safe_target = sanitize_provider_target(spec.target)
    key = provider_probe_key(spec)
    success = error_category is None
    item = {
        "kind": safe_kind,
        "provider": safe_provider,
        "target": safe_target,
        "state": "healthy" if success else "degraded",
        "circuit_state": "closed",
        "stage": _safe_label(spec.stage),
        "attempts": 1,
        "success_count": int(success),
        "failure_count": int(not success),
        "error_category": error_category,
        "last_error_category": error_category,
        "error_counts": ({error_category: 1} if error_category else {}),
        "duration_ms": max(0, duration_ms),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return key, item


def _rss_probe_operation(
    url: str,
    timeout_seconds: float,
) -> Callable[[], Awaitable[None]]:
    async def operation() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": "QQBotProviderProbe/1.0"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ProviderProbeError("invalid_response")

    return operation


def _market_probe_operation(
    market: str,
    provider: str,
    base_url: str,
    timeout_seconds: float,
) -> Callable[[], Awaitable[None]]:
    if provider in {"akshare", "yfinance"}:
        symbol = "000001.SZ" if provider == "akshare" else "^GSPC"

        async def isolated_operation() -> None:
            await asyncio.to_thread(
                _run_isolated_market_probe,
                {
                    "market": market,
                    "provider": provider,
                    "symbol": symbol,
                },
                timeout_seconds,
            )

        return isolated_operation

    async def operation() -> None:
        if provider == "sina":
            client = SinaMarketProvider(
                base_url or "https://hq.sinajs.cn",
                timeout_seconds=timeout_seconds,
            )
            symbol = "000001.SZ"
        else:
            raise ProviderProbeError("provider_unconfigured")
        await client.quote(market, symbol)

    return operation


def _run_isolated_market_probe(
    payload: dict[str, str],
    timeout_seconds: float,
) -> None:
    child_timeout = max(0.001, timeout_seconds * 0.8)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.features.market_worker"],
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            cwd=PROJECT_ROOT,
            timeout=child_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderProbeError("network_timeout") from exc
    try:
        output_lines = [line for line in result.stdout.splitlines() if line]
        response = json.loads(output_lines[-1]) if output_lines else {}
    except json.JSONDecodeError as exc:
        raise ProviderProbeError("provider_error") from exc
    if result.returncode != 0 or not response.get("ok"):
        category = _safe_label(str(response.get("category") or "provider_error"))
        raise ProviderProbeError(category)


def _search_probe_operation(
    provider: str,
    base_url: str,
    api_key_env: str,
    timeout_seconds: float,
    environ: Mapping[str, str],
) -> Callable[[], Awaitable[None]]:
    async def operation() -> None:
        if not base_url:
            raise ProviderProbeError("provider_unconfigured")
        if provider == "searxng":
            client = SearxngSearchProvider(base_url, timeout_seconds=timeout_seconds)
        elif provider == "wikipedia":
            client = WikipediaSearchProvider(base_url, timeout_seconds=timeout_seconds)
        elif provider == "brave":
            api_key = str(environ.get(api_key_env, "") or "").strip()
            if not api_key:
                raise ProviderProbeError("credential_unavailable")
            client = BraveSearchProvider(
                base_url,
                api_key,
                timeout_seconds=timeout_seconds,
            )
        else:
            raise ProviderProbeError("provider_unconfigured")
        await client.search("OpenAI")

    return operation


def _http_probe_operation(
    url: str,
    timeout_seconds: float,
    *,
    proxy_url: str | None,
    user_agent: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Callable[[], Awaitable[None]]:
    async def operation() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            proxy=proxy_url,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Range": "bytes=0-0"},
        ) as client:
            await client.get(url)

    return operation


def _market_target(provider: str, base_url: str) -> str:
    if base_url:
        return base_url
    return {
        "akshare": "akshare://eastmoney",
        "sina": "https://hq.sinajs.cn",
        "yfinance": "yfinance://yahoo",
    }.get(provider, f"{provider}://configured")


def _classify_probe_error(exc: Exception) -> str:
    if isinstance(exc, ProviderProbeError):
        return _safe_label(exc.category)
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return "dependency_missing"
    return classify_provider_error(exc)


def _snapshot_is_current_and_healthy(
    snapshot: dict[str, Any],
    *,
    max_age_seconds: float,
    expected_keys: set[str] | None = None,
) -> bool:
    providers = snapshot.get("providers")
    if not isinstance(providers, dict) or not providers:
        return False
    selected_keys = set(providers) if expected_keys is None else expected_keys
    if not selected_keys or not selected_keys.issubset(providers):
        return False
    now = datetime.now(UTC)
    for key in selected_keys:
        item = providers[key]
        if not isinstance(item, dict) or item.get("state") != "healthy":
            return False
        raw_timestamp = item.get("updated_at") or snapshot.get("updated_at")
        try:
            updated_at = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        age = (now - updated_at.astimezone(UTC)).total_seconds()
        if age < -60 or age > max_age_seconds:
            return False
    return True


def provider_probe_key(spec: ProviderProbeSpec) -> str:
    return ":".join(
        (
            _safe_label(spec.kind),
            _safe_label(spec.provider),
            sanitize_provider_target(spec.target),
        )
    )


def _safe_label(value: str) -> str:
    cleaned = "".join(
        char
        for char in str(value or "").strip().lower()
        if char.isalnum() or char in "_-.:#"
    )
    return cleaned[:80] or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Actively probe configured QQ bot providers and print redacted JSON."
    )
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument(
        "--path",
        default=os.getenv(
            "QQ_BOT_PROVIDER_STATUS_PATH",
            DEFAULT_PROVIDER_STATUS_PATH,
        ),
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="Read existing provider telemetry without making active requests.",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=300.0,
        help="Maximum accepted snapshot age with --snapshot-only --require-healthy.",
    )
    parser.add_argument(
        "--require-healthy",
        action="store_true",
        help="Exit non-zero unless all current configured provider probes are healthy.",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.max_age_seconds <= 0:
        parser.error("--max-age-seconds must be positive")

    if args.snapshot_only:
        config = load_config(args.config)
        specs = build_provider_probe_specs(
            config,
            timeout_seconds=args.timeout,
        )
        snapshot = ProviderHealthRegistry(args.path).snapshot()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        healthy = _snapshot_is_current_and_healthy(
            snapshot,
            max_age_seconds=args.max_age_seconds,
            expected_keys={provider_probe_key(spec) for spec in specs},
        )
        return 1 if args.require_healthy and not healthy else 0

    config = load_config(args.config)
    specs = build_provider_probe_specs(
        config,
        timeout_seconds=args.timeout,
    )
    report = asyncio.run(run_provider_probes(specs, status_path=args.path))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.require_healthy and not report["healthy"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
