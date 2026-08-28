from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from loguru import logger

from app.models import CodexRunwayConfig


class _PacificFallbackTimeZone(tzinfo):
    """Small DST-aware fallback for Windows environments without tzdata."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=-7 if self._is_dst(dt) else -8)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1 if self._is_dst(dt) else 0)

    def tzname(self, dt: datetime | None) -> str:
        return "PDT" if self._is_dst(dt) else "PST"

    @staticmethod
    def _is_dst(dt: datetime | None) -> bool:
        if dt is None:
            return False
        year = dt.year
        march_first = datetime(year, 3, 1)
        march_second_sunday = 8 + (6 - march_first.weekday()) % 7
        november_first = datetime(year, 11, 1)
        november_first_sunday = 1 + (6 - november_first.weekday()) % 7
        start = datetime(year, 3, march_second_sunday, 2)
        end = datetime(year, 11, november_first_sunday, 2)
        naive = dt.replace(tzinfo=None)
        return start <= naive < end


def _load_tibo_time_zone() -> tzinfo:
    try:
        return ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        return _PacificFallbackTimeZone()


MAX_FEED_BYTES = 1_048_576
STALE_AFTER = timedelta(hours=30)
TIBO_TIME_ZONE = _load_tibo_time_zone()
DISPLAY_TIME_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
TRUSTED_SOURCE_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
VISIBLE_KINDS = {"reset_completed", "reset_scheduled"}
DISCLAIMER = "非官方监测，仅供参考。"


async def fetch_codex_runway_feed(
    config: CodexRunwayConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        follow_redirects=False,
        timeout=config.request_timeout_seconds,
        headers={"User-Agent": "QQBot-CodexRunwayMonitor/1.0"},
    )
    try:
        response = await active_client.get(
            config.status_url,
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise ValueError("codex runway response status is not 200")
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            raise ValueError("codex runway response is not JSON")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_FEED_BYTES:
                    raise ValueError("codex runway response is too large")
            except ValueError as exc:
                if str(exc) == "codex runway response is too large":
                    raise
        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError("codex runway response is too large")
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("codex runway response contains invalid JSON") from exc
        _validate_feed(payload)
        return payload
    finally:
        if owned_client:
            await active_client.aclose()


def build_codex_runway_summary(
    feed: dict[str, Any],
    config: CodexRunwayConfig,
    *,
    now: datetime | None = None,
) -> str:
    message, _, _ = _build_summary_details(feed, config, now=now)
    return message


async def run_codex_runway_monitor_once(
    bot: Any,
    config: CodexRunwayConfig,
    *,
    record_system_event: Callable[..., Awaitable[None]],
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    if not config.enabled:
        return False
    current = _aware_utc(now)
    try:
        feed = await fetch_codex_runway_feed(config, client=client)
        message, item_count, reset_state = _build_summary_details(
            feed,
            config,
            now=current,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Codex Runway fetch failed: {}", type(exc).__name__)
        await record_system_event(
            level="ERROR",
            event="codex_runway_fetch_failed",
            detail=f"category={_error_category(exc)}",
        )
        return False
    try:
        result = await bot.send_private_msg(
            user_id=int(config.recipient_user_id),
            message=message,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Codex Runway summary send failed: {}", type(exc).__name__)
        await record_system_event(
            level="ERROR",
            event="codex_runway_send_failed",
            detail=f"category={_error_category(exc)}",
        )
        return False
    await record_system_event(
        level="INFO",
        event="codex_runway_summary_sent",
        detail=(
            f"items={item_count}; reset={reset_state}; "
            f"message_id_reported={_message_id_reported(result)}"
        ),
    )
    return True


async def codex_runway_worker(
    bot: Any,
    config: CodexRunwayConfig,
    *,
    record_system_event: Callable[..., Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if not config.enabled:
        return
    while True:
        try:
            await run_codex_runway_monitor_once(
                bot,
                config,
                record_system_event=record_system_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Codex Runway worker iteration failed: {}", type(exc).__name__)
            await record_system_event(
                level="ERROR",
                event="codex_runway_worker_failed",
                detail=f"category={_error_category(exc)}",
            )
        await sleep(float(config.interval_seconds))


def _build_summary_details(
    feed: dict[str, Any],
    config: CodexRunwayConfig,
    *,
    now: datetime | None,
) -> tuple[str, int, str]:
    current = _aware_utc(now)
    available = _monitor_available(feed, current)
    reset_types = _completed_reset_types_today(feed, current) if available else set()
    if not available:
        reset_line = "未知（监测数据不可用或已过期）"
        reset_state = "unknown"
    elif reset_types:
        reset_line = f"是（{_reset_types_label(reset_types)}）"
        reset_state = "yes"
    else:
        reset_line = "否"
        reset_state = "no"

    all_recent = _recent_events(feed, current, config.interval_seconds)
    selected = all_recent[: config.max_items]
    last_check = _parse_datetime(feed.get("lastSuccessfulCheckAt"))
    checked_text = (
        last_check.astimezone(DISPLAY_TIME_ZONE).strftime("%Y-%m-%d %H:%M")
        if last_check
        else "未知"
    )
    lines = [
        "【Codex Runway 四小时汇总】",
        f"今日是否已重置（Tibo 时区）：{reset_line}",
        f"监测截至：{checked_text}（北京时间）",
    ]
    if selected:
        suffix = (
            f"（最多显示{config.max_items}条）"
            if len(all_recent) > config.max_items
            else ""
        )
        lines.append(f"近4小时新监测消息：{len(selected)}条{suffix}")
        lines.extend(
            _format_event(index, event, config.excerpt_chars)
            for index, event in enumerate(selected, start=1)
        )
    else:
        lines.append("近4小时无新监测消息")
    lines.append(DISCLAIMER)
    message = "\n".join(lines)
    if len(message) > config.max_message_chars:
        marker = "\n内容超出上限，已截断。\n" + DISCLAIMER
        message = message[: config.max_message_chars - len(marker)].rstrip() + marker
    return message, len(selected), reset_state


def _validate_feed(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("codex runway response schema is unsupported")
    if not isinstance(payload.get("events"), list):
        raise ValueError("codex runway events must be an array")
    monitor = payload.get("monitor")
    if not isinstance(monitor, dict) or not isinstance(monitor.get("status"), str):
        raise ValueError("codex runway monitor status is invalid")
    if _parse_datetime(payload.get("lastSuccessfulCheckAt")) is None:
        raise ValueError("codex runway last successful check is invalid")


def _monitor_available(feed: dict[str, Any], now: datetime) -> bool:
    monitor = feed.get("monitor")
    if not isinstance(monitor, dict) or monitor.get("status") != "ok":
        return False
    last_check = _parse_datetime(feed.get("lastSuccessfulCheckAt"))
    return last_check is not None and now - last_check <= STALE_AFTER


def _completed_reset_types_today(feed: dict[str, Any], now: datetime) -> set[str]:
    today = now.astimezone(TIBO_TIME_ZONE).date()
    result: set[str] = set()
    for event in feed.get("events", []):
        if not isinstance(event, dict) or event.get("kind") != "reset_completed":
            continue
        occurred = _parse_datetime(event.get("effectiveAt") or event.get("announcedAt"))
        if occurred is None or occurred > now:
            continue
        if occurred.astimezone(TIBO_TIME_ZONE).date() != today:
            continue
        reset_type = str(event.get("resetType") or "global")
        if reset_type == "global_and_banked":
            result.update(("global", "banked"))
        elif reset_type in {"global", "banked"}:
            result.add(reset_type)
    return result


def _recent_events(
    feed: dict[str, Any],
    now: datetime,
    interval_seconds: int,
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(seconds=interval_seconds)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    seen: set[str] = set()
    for event in feed.get("events", []):
        if not isinstance(event, dict) or event.get("kind") not in VISIBLE_KINDS:
            continue
        announced = _parse_datetime(event.get("announcedAt"))
        if announced is None or announced < cutoff or announced > now:
            continue
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        identity = str(source.get("postId") or "").strip()
        if not identity:
            identity = "|".join(
                (
                    str(event.get("kind") or ""),
                    announced.isoformat(),
                    str(event.get("text") or ""),
                )
            )
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append((announced, event))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [event for _, event in candidates]


def _format_event(index: int, event: dict[str, Any], excerpt_chars: int) -> str:
    announced = _parse_datetime(event.get("announcedAt"))
    when = (
        announced.astimezone(DISPLAY_TIME_ZONE).strftime("%m-%d %H:%M")
        if announced
        else "时间未知"
    )
    label = _event_label(event)
    excerpt = _compact_excerpt(event.get("text"), excerpt_chars)
    lines = [f"{index}. {when} {label}"]
    if excerpt:
        lines.append(f"   {excerpt}")
    source_url = _trusted_source_url(event)
    if source_url:
        lines.append(f"   来源：{source_url}")
    return "\n".join(lines)


def _event_label(event: dict[str, Any]) -> str:
    reset_type = str(event.get("resetType") or "global")
    type_label = {
        "global": "全局重置",
        "banked": "重置银行",
        "global_and_banked": "全局重置 + 重置银行",
    }.get(reset_type, "重置信号")
    if event.get("kind") == "reset_scheduled":
        return f"{type_label}计划"
    return f"{type_label}已完成"


def _compact_excerpt(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _trusted_source_url(event: dict[str, Any]) -> str:
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    value = str(source.get("url") or "").strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in TRUSTED_SOURCE_HOSTS
        or parsed.username
        or parsed.password
    ):
        return ""
    return value


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(UTC)


def _reset_types_label(reset_types: set[str]) -> str:
    if reset_types == {"global", "banked"}:
        return "全局重置 + 重置银行"
    if "global" in reset_types:
        return "全局重置"
    return "重置银行"


def _error_category(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    if isinstance(exc, ValueError):
        return "invalid_response"
    return type(exc).__name__


def _message_id_reported(result: Any) -> str:
    if isinstance(result, dict) and result.get("message_id") is not None:
        return "true"
    return "false"
