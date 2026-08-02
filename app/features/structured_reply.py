from __future__ import annotations

import math
from collections.abc import Sequence
from urllib.parse import urlsplit

from app.features.contracts import StructuredReply


STRUCTURED_INFORMATION_MAX_CHARS = 1000
STRUCTURED_EXTERNAL_URL_MAX_CHARS = 240
OVERSIZED_URL_CONTINUATION = "内容已截断；下一页：请按标题访问来源站点"


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
        return "不可用（链接过长）", True
    return raw, False


def build_structured_reply(
    *,
    header: str,
    blocks: Sequence[str],
    page: int = 1,
    page_size: int = 2,
    next_command: str | None = None,
    footer: str | None = None,
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
    messages: list[str] = []
    page_header = f"{header}（第 {page}/{total_pages} 页）" if total_pages > 1 else header
    if selected:
        messages.append(f"{page_header}\n{selected[0]}")
        messages.extend(selected[1:])
    else:
        messages.append(page_header)
    if footer:
        messages[-1] = f"{messages[-1]}\n{footer.strip()}"
    if page < total_pages:
        command = next_command or "下一页"
        messages.append(f"内容已截断；下一页：{command}")
    return StructuredReply(
        messages=tuple(messages),
        page=page,
        total_pages=total_pages,
    )
