from __future__ import annotations

import math
from collections.abc import Sequence
from urllib.parse import urlsplit

from app.features.contracts import StructuredReply


STRUCTURED_EXTERNAL_URL_MAX_CHARS = 2048


def format_structured_external_url(value: str | None) -> tuple[str, bool]:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any(char in raw for char in "\r\n\t")
    ):
        return "不可用", False
    if len(raw) > STRUCTURED_EXTERNAL_URL_MAX_CHARS:
        return "不可用（链接异常过长）", True
    return raw, False


def build_structured_reply(
    *,
    header: str,
    blocks: Sequence[str],
    page: int = 1,
    page_size: int = 2,
    next_command: str | None = None,
    footer: str | None = None,
    fallback_message: str | None = None,
) -> StructuredReply:
    if page <= 0:
        raise ValueError("page must be positive")
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    total_pages = max(1, math.ceil(len(blocks) / page_size))
    if page > total_pages:
        return StructuredReply(
            messages=(f"没有第 {page} 页，共 {total_pages} 页",),
            page=total_pages,
            total_pages=total_pages,
        )

    start = (page - 1) * page_size
    selected = [block.strip() for block in blocks[start : start + page_size] if block.strip()]
    page_header = f"{header}（第 {page}/{total_pages} 页）" if total_pages > 1 else header
    message = page_header
    if selected:
        message = f"{message}\n" + "\n\n".join(selected)
    if footer:
        message = f"{message}\n\n{footer.strip()}"
    if page < total_pages:
        command = next_command or "下一页"
        message = f"{message}\n\n内容已截断；下一页：{command}"
    return StructuredReply(
        messages=(message,),
        page=page,
        total_pages=total_pages,
        fallback_messages=(fallback_message.strip(),) if fallback_message else (),
    )


def is_message_too_long_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    markers = (
        "消息过长",
        "消息长度",
        "字数限制",
        "message too long",
        "message length",
        "too many characters",
        "content too long",
    )
    return any(marker in text for marker in markers)


def build_compact_brief(
    *,
    header: str,
    blocks: Sequence[str],
    footer: str,
) -> str:
    lines = [header]
    for block in blocks:
        block_lines = block.splitlines()
        title = block_lines[0] if block_lines else "无标题"
        source = next(
            (line.removeprefix("来源：") for line in block_lines if line.startswith("来源：")),
            "未知来源",
        )
        lines.append(f"{title}（来源：{source}）")
    lines.append(footer)
    return "\n".join(lines)
