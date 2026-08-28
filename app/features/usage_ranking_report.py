from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from app.models import UsageRankingReportConfig
from app.plugins.send_helper import send_private_image_direct


REQUIRED_USER_FIELDS = {
    "user_id",
    "email",
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "total_tokens",
    "actual_cost",
    "cost",
    "account_cost",
}
IMAGE_WIDTH = 1600
ROW_HEIGHT = 58
SHANGHAI_TIME_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def seconds_until_next_usage_report(
    now: datetime,
    *,
    send_time: str,
    timezone_name: str,
) -> float:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = _time_zone(timezone_name)
    local_now = now.astimezone(zone)
    hour, minute = (int(part) for part in send_time.split(":"))
    scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now >= scheduled:
        scheduled += timedelta(days=1)
    return (scheduled.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


async def fetch_usage_ranking(
    config: UsageRankingReportConfig,
    *,
    report_date: date,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    token_path = Path(config.refresh_token_path)
    refresh_token = _read_refresh_token(token_path)
    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=config.request_timeout_seconds,
        follow_redirects=False,
        headers={"User-Agent": "QQBot-UsageRankingReport/1.0"},
    )
    try:
        refresh_response = await active_client.post(
            f"{config.base_url}/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
        )
        refresh_data = _response_data(refresh_response, operation="refresh")
        access_token = _validated_token(
            refresh_data.get("access_token"),
            name="access token",
        )
        new_refresh_token = _validated_token(
            refresh_data.get("refresh_token"),
            name="refresh token",
        )
        _write_refresh_token_atomic(token_path, new_refresh_token)

        day = report_date.isoformat()
        ranking_response = await active_client.get(
            f"{config.base_url}/api/v1/admin/dashboard/user-breakdown",
            params={
                "start_date": day,
                "end_date": day,
                "sort_by": "total_tokens",
                "limit": config.limit,
                "timezone": config.timezone,
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Admin-UI-Request": "1",
            },
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
        )
        ranking_data = _response_data(ranking_response, operation="ranking")
        if ranking_data.get("start_date") != day or ranking_data.get("end_date") != day:
            raise ValueError("ranking date range does not match the requested day")
        users = ranking_data.get("users")
        if not isinstance(users, list):
            raise ValueError("ranking users must be an array")
        normalized = [_validate_user_row(row) for row in users]
        normalized.sort(key=lambda row: row["total_tokens"], reverse=True)
        return normalized[: config.limit]
    finally:
        if owned_client:
            await active_client.aclose()


def render_usage_ranking_png(
    rows: list[dict[str, Any]],
    config: UsageRankingReportConfig,
    *,
    report_date: date,
    generated_at: datetime,
) -> Path:
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    font_path = Path(config.font_path)
    if font_path.is_symlink() or not font_path.is_file():
        raise ValueError("usage ranking font file is unavailable")
    output_dir = Path(config.output_dir)
    if output_dir.is_symlink():
        raise ValueError("usage ranking output directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)

    visible_rows = rows[: config.limit]
    row_count = max(1, len(visible_rows))
    table_top = 154
    footer_height = 84
    image_height = max(560, table_top + ROW_HEIGHT + row_count * ROW_HEIGHT + footer_height)

    image = Image.new("RGB", (IMAGE_WIDTH, image_height), "#f5f7fb")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(font_path), 42)
    subtitle_font = ImageFont.truetype(str(font_path), 23)
    header_font = ImageFont.truetype(str(font_path), 22)
    row_font = ImageFont.truetype(str(font_path), 21)
    footer_font = ImageFont.truetype(str(font_path), 18)

    draw.rounded_rectangle((28, 24, IMAGE_WIDTH - 28, 126), radius=12, fill="#ffffff")
    draw.text((54, 38), "Crazy Thursday · 用户排行", font=title_font, fill="#162033")
    generated_local = generated_at.astimezone(_time_zone(config.timezone))
    subtitle = (
        f"时间范围：今天（{report_date.isoformat()}）  "
        f"生成时间：{generated_local:%Y-%m-%d %H:%M}  "
        f"共 {len(visible_rows)} 名用户"
    )
    draw.text((56, 94), subtitle, font=subtitle_font, fill="#667085")

    columns = (
        ("#", 70, "center"),
        ("用户", 370, "left"),
        ("请求数", 130, "right"),
        ("输入 Token", 190, "right"),
        ("输出 Token", 190, "right"),
        ("缓存 Token", 190, "right"),
        ("总 Token ↓", 200, "right"),
        ("费用", 180, "right"),
    )
    left = 40
    table_width = sum(width for _, width, _ in columns)
    draw.rounded_rectangle(
        (left, table_top, left + table_width, table_top + ROW_HEIGHT),
        radius=10,
        fill="#1f2937",
    )
    x = left
    for label, width, alignment in columns:
        _draw_cell_text(
            draw,
            label,
            x,
            table_top,
            width,
            ROW_HEIGHT,
            header_font,
            fill="#ffffff",
            alignment=alignment,
        )
        x += width

    if visible_rows:
        for index, row in enumerate(visible_rows, start=1):
            y = table_top + ROW_HEIGHT * index
            fill = "#ffffff" if index % 2 else "#eef2f7"
            draw.rectangle((left, y, left + table_width, y + ROW_HEIGHT), fill=fill)
            values = (
                str(index),
                str(row["email"]),
                f"{int(row['requests']):,}",
                _compact_number(int(row["input_tokens"])),
                _compact_number(int(row["output_tokens"])),
                _compact_number(int(row["cache_tokens"])),
                _compact_number(int(row["total_tokens"])),
                f"${float(row['actual_cost']):,.4f}",
            )
            x = left
            for value, (_, width, alignment) in zip(values, columns, strict=True):
                _draw_cell_text(
                    draw,
                    value,
                    x,
                    y,
                    width,
                    ROW_HEIGHT,
                    row_font,
                    fill="#202939",
                    alignment=alignment,
                )
                x += width
            draw.line(
                (left, y + ROW_HEIGHT - 1, left + table_width, y + ROW_HEIGHT - 1),
                fill="#d8dee8",
                width=1,
            )
    else:
        y = table_top + ROW_HEIGHT
        draw.rectangle((left, y, left + table_width, y + ROW_HEIGHT), fill="#ffffff")
        _draw_cell_text(
            draw,
            "今天暂无用户排行数据",
            left,
            y,
            table_width,
            ROW_HEIGHT,
            row_font,
            fill="#667085",
            alignment="center",
        )

    footer_y = table_top + ROW_HEIGHT + row_count * ROW_HEIGHT + 24
    draw.text(
        (48, footer_y),
        "数据来源：管理后台用户排行接口 · 仅供内部使用",
        font=footer_font,
        fill="#7a8495",
    )

    output = output_dir / f"usage-ranking-{report_date:%Y%m%d}.png"
    fd, temporary_name = tempfile.mkstemp(prefix=".usage-ranking.", dir=output_dir)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, output)
    finally:
        image.close()
        temporary.unlink(missing_ok=True)
    return output


async def run_usage_ranking_report_once(
    bot: Any,
    config: UsageRankingReportConfig,
    *,
    record_system_event: Callable[..., Awaitable[None]],
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    if not config.enabled:
        return False
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    report_date = current.astimezone(_time_zone(config.timezone)).date()
    try:
        rows = await fetch_usage_ranking(
            config,
            report_date=report_date,
            client=client,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Usage ranking fetch failed: {}", type(exc).__name__)
        await record_system_event(
            level="ERROR",
            event="usage_ranking_report_fetch_failed",
            detail=f"category={_error_category(exc)}",
        )
        return False

    image_path: Path | None = None
    try:
        image_path = await asyncio.to_thread(
            render_usage_ranking_png,
            rows,
            config,
            report_date=report_date,
            generated_at=current,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Usage ranking render failed: {}", type(exc).__name__)
        await record_system_event(
            level="ERROR",
            event="usage_ranking_report_render_failed",
            detail=f"category={_error_category(exc)}",
        )
        return False

    try:
        result = await send_private_image_direct(
            bot,
            user_id=config.recipient_user_id,
            file_path=str(image_path),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Usage ranking send failed: {}", type(exc).__name__)
        await record_system_event(
            level="ERROR",
            event="usage_ranking_report_send_failed",
            detail=f"category={_error_category(exc)}",
        )
        return False
    finally:
        image_path.unlink(missing_ok=True)

    await record_system_event(
        level="INFO",
        event="usage_ranking_report_sent",
        detail=(
            f"rows={len(rows)}; report_date={report_date.isoformat()}; "
            f"message_id_reported={_message_id_reported(result)}"
        ),
    )
    return True


async def usage_ranking_report_worker(
    bot: Any,
    config: UsageRankingReportConfig,
    *,
    record_system_event: Callable[..., Awaitable[None]],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if not config.enabled:
        return
    while True:
        delay = seconds_until_next_usage_report(
            now(),
            send_time=config.send_time,
            timezone_name=config.timezone,
        )
        await sleep(delay)
        try:
            await run_usage_ranking_report_once(
                bot,
                config,
                record_system_event=record_system_event,
                now=now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Usage ranking worker failed: {}", type(exc).__name__)
            await record_system_event(
                level="ERROR",
                event="usage_ranking_report_worker_failed",
                detail=f"category={_error_category(exc)}",
            )


def _read_refresh_token(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("refresh token file must not be a symlink")
    if not path.is_file():
        raise ValueError("refresh token file is unavailable")
    file_stat = path.stat()
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ValueError("refresh token file permissions are too broad")
    token = path.read_text(encoding="utf-8").strip()
    return _validated_token(token, name="refresh token")


def _write_refresh_token_atomic(path: Path, token: str) -> None:
    old_stat = path.stat()
    fd, temporary_name = tempfile.mkstemp(prefix=".usage-refresh-token.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if os.name == "posix":
            os.chown(temporary, old_stat.st_uid, old_stat.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _response_data(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise ValueError(f"{operation} response status is not 200")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"{operation} response is not JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("code") != 0
        or not isinstance(payload.get("data"), dict)
    ):
        raise ValueError(f"{operation} response envelope is invalid")
    return payload["data"]


def _validated_token(value: Any, *, name: str) -> str:
    token = str(value or "").strip()
    if len(token) < 20 or re.search(r"\s", token):
        raise ValueError(f"{name} is invalid")
    return token


def _validate_user_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not REQUIRED_USER_FIELDS.issubset(value):
        raise ValueError("ranking user row is incomplete")
    integer_fields = (
        "user_id",
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_tokens",
        "total_tokens",
    )
    if any(
        isinstance(value[field], bool) or not isinstance(value[field], int)
        for field in integer_fields
    ):
        raise ValueError("ranking user row has invalid integer fields")
    if not isinstance(value["email"], str) or not value["email"].strip():
        raise ValueError("ranking user row email is invalid")
    for field in ("actual_cost", "cost", "account_cost"):
        if isinstance(value[field], bool) or not isinstance(value[field], (int, float)):
            raise ValueError("ranking user row has invalid cost fields")
    return {field: value[field] for field in REQUIRED_USER_FIELDS}


def _draw_cell_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    width: int,
    height: int,
    font: ImageFont.FreeTypeFont,
    *,
    fill: str,
    alignment: str,
) -> None:
    padding = 14
    fitted = _fit_text(draw, text, font, max(1, width - padding * 2))
    bbox = draw.textbbox((0, 0), fitted, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    if alignment == "right":
        text_x = x + width - padding - text_width
    elif alignment == "center":
        text_x = x + (width - text_width) / 2
    else:
        text_x = x + padding
    text_y = y + (height - text_height) / 2 - bbox[1]
    draw.text((text_x, text_y), fitted, font=font, fill=fill)


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    fitted = text
    while fitted and draw.textlength(fitted + ellipsis, font=font) > max_width:
        fitted = fitted[:-1]
    return fitted.rstrip() + ellipsis


def _compact_number(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:.2f}{suffix}"
    return f"{value:,}"


def _error_category(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    if isinstance(exc, ValueError):
        return "invalid_data"
    return type(exc).__name__


def _message_id_reported(result: Any) -> str:
    if isinstance(result, dict) and result.get("message_id") is not None:
        return "true"
    return "false"


def _time_zone(name: str):
    if name != "Asia/Shanghai":
        raise ValueError("unsupported usage ranking timezone")
    return SHANGHAI_TIME_ZONE
