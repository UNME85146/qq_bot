from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_SENTENCE_PATTERN = re.compile(r"[^。！？!?\n]+[。！？!?]+|[^。！？!?\n]+")
_FENCED_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_CODE_LIKE_PATTERN = re.compile(
    r"\b(def|class|import|from|return|async|await|function|const|let|var|SELECT|INSERT|UPDATE)\b|[{};]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReplyParseResult:
    text: str
    reply_mode: str = "short"
    send_sticker: bool = False
    sticker_intent: str = ""


class ReplyParser:
    def parse(self, text: str) -> ReplyParseResult:
        return parse_model_reply(text)


class ReplyFormatter:
    def __init__(self, max_length: int) -> None:
        self._max_length = max_length

    def format(self, text: str) -> str:
        cleaned = clean_reply_text(text)
        return truncate_naturally(cleaned, self._max_length)

    def format_unlimited(self, text: str) -> str:
        return clean_reply_text(text)


def parse_model_reply(text: str) -> ReplyParseResult:
    original = text or ""
    payload = _try_load_json_payload(original)
    reply_mode = "short"
    send_sticker = False
    sticker_intent = ""
    if isinstance(payload, dict):
        original = _payload_text(payload)
        reply_mode = _normalize_reply_mode(str(payload.get("reply_mode", "") or ""))
        send_sticker = _truthy(payload.get("send_sticker"))
        sticker_intent = str(payload.get("sticker_intent", "") or "").strip()
    cleaned = clean_reply_text(original)
    cleaned = _strip_labeled_prefix(cleaned)
    cleaned = _strip_orphan_fences(cleaned)
    cleaned = clean_reply_text(cleaned)
    if not cleaned:
        cleaned = "我刚刚没组织好，重说一下。"
    if reply_mode == "short":
        reply_mode = detect_reply_mode(cleaned)
    return ReplyParseResult(
        text=cleaned,
        reply_mode=reply_mode,
        send_sticker=send_sticker,
        sticker_intent=sticker_intent,
    )


def detect_reply_mode(text: str) -> str:
    if has_fenced_code_block(text) or _looks_like_standalone_code(text):
        return "code_block"
    compact = "".join(text.split())
    if len(compact) > 360 or any(
        marker in compact
        for marker in (
            "步骤",
            "方案",
            "原因是",
            "可以这样",
            "排查",
            "实现",
        )
    ):
        return "long_text"
    return "short"


def has_fenced_code_block(text: str) -> bool:
    return bool(_FENCED_BLOCK_PATTERN.search(text))


def clean_reply_text(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    for prefix in ("作为AI语言模型，", "作为 AI 语言模型，"):
        if cleaned.startswith(prefix):
            cleaned = cleaned.removeprefix(prefix)
    return cleaned.strip()


def split_reply_messages(text: str, *, reply_mode: str = "short") -> list[str]:
    text = _clean_code_text(text) if reply_mode == "code_block" else clean_reply_text(text)
    if not text:
        return []
    if reply_mode in {"long_text", "code_block"}:
        return _split_long_or_code_text(text)
    messages: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for match in _SENTENCE_PATTERN.findall(line):
            message = match.strip()
            if message and message != "```":
                messages.append(message)
    return messages or [text]


def truncate_naturally(text: str, max_length: int, *, reply_mode: str = "short") -> str:
    if len(text) <= max_length:
        return text
    if reply_mode in {"long_text", "code_block"}:
        return text[:max_length]
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


def _try_load_json_payload(text: str) -> Any:
    candidate = text.strip()
    if not candidate:
        return None
    fenced_match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, re.IGNORECASE)
    if fenced_match:
        candidate = fenced_match.group(1).strip()
    if not (candidate.startswith("{") and candidate.endswith("}")):
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _payload_text(payload: dict[str, Any]) -> str:
    for key in ("reply_text", "text", "content", "reply"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_reply_mode(value: str) -> str:
    value = value.strip().lower()
    if value in {"long", "long_text", "detail", "detailed"}:
        return "long_text"
    if value in {"code", "code_block"}:
        return "code_block"
    return "short"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "要", "发送"}
    return False


def _strip_labeled_prefix(text: str) -> str:
    return re.sub(
        r"^\s*(?:reply_text|reply|content|text|回复|正文)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _strip_orphan_fences(text: str) -> str:
    if _FENCED_BLOCK_PATTERN.search(text):
        return text
    lines = text.splitlines()
    if len(lines) == 1:
        return "" if lines[0].strip() == "```" else text
    while lines and lines[0].strip() == "```":
        lines.pop(0)
    while lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def _looks_like_standalone_code(text: str) -> bool:
    if "\n" not in text and len(text) < 80:
        return bool(_CODE_LIKE_PATTERN.search(text)) and not _looks_like_chat_text(text)
    return bool(_CODE_LIKE_PATTERN.search(text))


def _looks_like_chat_text(text: str) -> bool:
    return any(ch in text for ch in "吗呢吧呀啊哦哈")


def _split_long_or_code_text(text: str) -> list[str]:
    parts: list[str] = []
    cursor = 0
    for match in _FENCED_BLOCK_PATTERN.finditer(text):
        before = text[cursor : match.start()].strip()
        if before:
            parts.extend(_split_paragraphs(before))
        block = match.group(0).strip()
        if block and block != "```":
            parts.append(block)
        cursor = match.end()
    tail = text[cursor:].strip()
    if tail:
        parts.extend(_split_paragraphs(tail))
    return [part for part in parts if part.strip() and part.strip() != "```"] or [text]


def _split_paragraphs(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    return [text.strip()]


def _clean_code_text(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return _strip_orphan_fences(cleaned)
