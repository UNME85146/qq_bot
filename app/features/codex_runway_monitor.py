from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from loguru import logger

from app.features.structured_reply import is_message_too_long_error
from app.model.llm_client import LlmClient
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
TRANSLATION_MAX_TOKENS = 16_000
TRANSPORT_CHUNK_CHARS = 3_500
TIBO_TIME_ZONE = _load_tibo_time_zone()
DISPLAY_TIME_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
TRUSTED_SOURCE_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
VISIBLE_KINDS = {"reset_completed", "reset_scheduled"}


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


def seconds_until_next_codex_runway(
    now: datetime,
    *,
    send_times: Sequence[str],
    timezone_name: str = "Asia/Shanghai",
) -> float:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = _runway_time_zone(timezone_name)
    parsed_times = tuple(_parse_hhmm(value) for value in send_times)
    if not parsed_times:
        raise ValueError("send_times must not be empty")
    local_now = now.astimezone(zone)
    for day_offset in range(2):
        scheduled_date = local_now.date() + timedelta(days=day_offset)
        for scheduled_time in parsed_times:
            scheduled = datetime.combine(
                scheduled_date,
                scheduled_time,
                tzinfo=zone,
            )
            if scheduled <= local_now:
                continue
            return max(
                0.0,
                (scheduled.astimezone(UTC) - now.astimezone(UTC)).total_seconds(),
            )
    raise RuntimeError("could not find the next Codex Runway slot")


def build_codex_runway_summary(
    feed: dict[str, Any],
    config: CodexRunwayConfig,
    *,
    now: datetime | None = None,
    event_texts: Mapping[str, str] | None = None,
) -> str:
    message, _, _ = _build_summary_details(
        feed,
        config,
        now=now,
        event_texts=event_texts,
    )
    return message


async def run_codex_runway_monitor_once(
    bot: Any,
    config: CodexRunwayConfig,
    *,
    record_system_event: Callable[..., Awaitable[None]],
    model_client: LlmClient | None = None,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    if not config.enabled:
        return False
    current = _aware_utc(now)
    try:
        feed = await fetch_codex_runway_feed(config, client=client)
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

    selected = _selected_events(feed, config, current)
    try:
        translated = await _translate_trusted_events(model_client, selected)
        message, item_count, reset_state = _build_summary_details(
            feed,
            config,
            now=current,
            event_texts=translated,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Codex Runway translation failed: {}", type(exc).__name__)
        await record_system_event(
            level="ERROR",
            event="codex_runway_translation_failed",
            detail=f"category={_error_category(exc)}",
        )
        return False

    try:
        result = await _send_complete_private_message(
            bot,
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
    model_client: LlmClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if not config.enabled:
        return
    while True:
        delay = seconds_until_next_codex_runway(
            now(),
            send_times=config.send_times,
            timezone_name=config.timezone,
        )
        await sleep(delay)
        try:
            await run_codex_runway_monitor_once(
                bot,
                config,
                record_system_event=record_system_event,
                model_client=model_client,
                now=now(),
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


def _build_summary_details(
    feed: dict[str, Any],
    config: CodexRunwayConfig,
    *,
    now: datetime | None,
    event_texts: Mapping[str, str] | None = None,
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

    all_recent = _recent_events(feed, current, config.lookback_seconds)
    selected = (
        all_recent
        if config.max_items <= 0
        else all_recent[: config.max_items]
    )
    last_check = _parse_datetime(feed.get("lastSuccessfulCheckAt"))
    checked_text = (
        last_check.astimezone(DISPLAY_TIME_ZONE).strftime("%Y-%m-%d %H:%M")
        if last_check
        else "未知"
    )
    lines = [
        f"今日是否已重置（Tibo 时区）：{reset_line}",
        f"监测截至：{checked_text}（北京时间）",
    ]
    if selected:
        suffix = (
            f"（最多显示{config.max_items}条）"
            if config.max_items > 0 and len(all_recent) > config.max_items
            else ""
        )
        lines.append(f"近期新监测消息：{len(selected)}条{suffix}")
        lines.extend(
            _format_event(
                index,
                event,
                text_override=(event_texts or {}).get(_event_identity(event)),
            )
            for index, event in enumerate(selected, start=1)
        )
    else:
        lines.append("近期无新监测消息")
    return "\n".join(lines), len(selected), reset_state


def _selected_events(
    feed: dict[str, Any],
    config: CodexRunwayConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    events = _recent_events(feed, now, config.lookback_seconds)
    if config.max_items <= 0:
        return events
    return events[: config.max_items]


async def _translate_trusted_events(
    model_client: LlmClient | None,
    events: Sequence[dict[str, Any]],
) -> dict[str, str]:
    translatable: list[tuple[str, str]] = []
    translated: dict[str, str] = {}
    for event in events:
        text = _clean_event_text(event.get("text"))
        if not text or not _trusted_source_url(event):
            continue
        identity = _event_identity(event)
        if not _contains_non_chinese_text(text):
            translated[identity] = text
        else:
            translatable.append((identity, text))
    if not translatable:
        return translated
    if model_client is None:
        raise ValueError("translation model is unavailable")

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Codex Runway 推文中文翻译器。把每条 sourceText 完整翻译成简体中文，"
                "不得摘要、删减、改写事实或输出解释。保留数字、时间和事实关系。"
                "title 之外的正文值不得出现拉丁字母；只返回 JSON，格式为 "
                "{\"items\":[{\"id\":\"...\",\"text\":\"完整中文正文\"}]}，"
                "id 顺序必须与输入一致。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "items": [
                        {"id": identity, "sourceText": text}
                        for identity, text in translatable
                    ]
                },
                ensure_ascii=False,
            ),
        },
    ]
    generate_with_options = getattr(model_client, "generate_with_options", None)
    if callable(generate_with_options):
        generated = await generate_with_options(
            messages,
            max_tokens=TRANSLATION_MAX_TOKENS,
            reasoning_effort="low",
        )
    else:
        generated = await model_client.generate(messages)
    raw = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        str(generated.text or "").strip(),
        flags=re.IGNORECASE,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("translation response is not JSON") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) != len(translatable):
        raise ValueError("translation response item count is invalid")
    expected_ids = [identity for identity, _text in translatable]
    actual_ids = [str(item.get("id")) for item in items if isinstance(item, dict)]
    if actual_ids != expected_ids:
        raise ValueError("translation response ids are invalid")
    for item in items:
        text = _clean_event_text(item.get("text"))
        if not text or _contains_non_chinese_text(text):
            raise ValueError("translation response is incomplete")
        translated[str(item["id"])] = text
    return translated


async def _send_complete_private_message(
    bot: Any,
    *,
    user_id: int,
    message: str,
) -> Any:
    try:
        return await bot.send_private_msg(user_id=user_id, message=message)
    except Exception as exc:
        if not is_message_too_long_error(exc):
            raise
        result: Any = None
        for chunk in _split_transport_chunks(message):
            result = await bot.send_private_msg(user_id=user_id, message=chunk)
        return result


def _split_transport_chunks(text: str) -> list[str]:
    if len(text) <= TRANSPORT_CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TRANSPORT_CHUNK_CHARS:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, TRANSPORT_CHUNK_CHARS + 1)
        if cut <= 0:
            cut = TRANSPORT_CHUNK_CHARS
        else:
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


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
    lookback_seconds: int,
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(seconds=lookback_seconds)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    seen: set[str] = set()
    for event in feed.get("events", []):
        if not isinstance(event, dict) or event.get("kind") not in VISIBLE_KINDS:
            continue
        announced = _parse_datetime(event.get("announcedAt"))
        if announced is None or announced < cutoff or announced > now:
            continue
        identity = _event_identity(event, announced=announced)
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append((announced, event))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [event for _, event in candidates]


def _event_identity(
    event: dict[str, Any],
    *,
    announced: datetime | None = None,
) -> str:
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    identity = str(source.get("postId") or "").strip()
    if identity:
        return identity
    occurred = announced or _parse_datetime(event.get("announcedAt"))
    return "|".join(
        (
            str(event.get("kind") or ""),
            occurred.isoformat() if occurred else "",
            str(event.get("text") or ""),
        )
    )


def _format_event(
    index: int,
    event: dict[str, Any],
    *,
    text_override: str | None = None,
) -> str:
    announced = _parse_datetime(event.get("announcedAt"))
    when = (
        announced.astimezone(DISPLAY_TIME_ZONE).strftime("%m-%d %H:%M")
        if announced
        else "时间未知"
    )
    label = _event_label(event)
    text = _clean_event_text(
        text_override if text_override is not None else event.get("text")
    )
    lines = [f"{index}. {when} {label}"]
    if text:
        lines.append(f"   {text}")
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


def _clean_event_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _contains_non_chinese_text(value: str) -> bool:
    return any(character.isalpha() and not _is_cjk(character) for character in value)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


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


def _runway_time_zone(name: str) -> tzinfo:
    if name != "Asia/Shanghai":
        raise ValueError("Codex Runway timezone must be Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return DISPLAY_TIME_ZONE


def _parse_hhmm(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("Codex Runway send times must use HH:MM")
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("Codex Runway send times must use HH:MM")
    return time(hour=hour, minute=minute)


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
