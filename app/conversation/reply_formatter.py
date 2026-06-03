from __future__ import annotations

import re

_SENTENCE_PATTERN = re.compile(r"[^。！？!?\n]+[。！？!?]+|[^。！？!?\n]+")


class ReplyFormatter:
    def __init__(self, max_length: int) -> None:
        self._max_length = max_length

    def format(self, text: str) -> str:
        cleaned = clean_reply_text(text)
        return truncate_naturally(cleaned, self._max_length)

    def format_unlimited(self, text: str) -> str:
        return clean_reply_text(text)


def clean_reply_text(text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        for prefix in ("作为AI语言模型，", "作为 AI 语言模型，"):
            if cleaned.startswith(prefix):
                cleaned = cleaned.removeprefix(prefix)
        return cleaned


def split_reply_messages(text: str) -> list[str]:
    messages: list[str] = []
    for line in text.splitlines():
        for match in _SENTENCE_PATTERN.findall(line):
            message = match.strip()
            if message:
                messages.append(message)
    return messages or [text.strip()] if text.strip() else []


def truncate_naturally(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    pieces = split_reply_messages(text)
    selected: list[str] = []
    total = 0
    for piece in pieces:
        extra = len(piece) + (1 if selected else 0)
        if total + extra > max_length:
            break
        selected.append(piece)
        total += extra
    if selected:
        return "\n".join(selected)
    return text[:max_length]
