from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_SENTENCE_PATTERN = re.compile(r"[^。！？!?\n]+[。！？!?]+|[^。！？!?\n]+")
_FENCED_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_FENCED_BLOCK_DETAIL_PATTERN = re.compile(
    r"```[ \t]*(?P<label>[A-Za-z0-9_+#.-]*)[ \t]*\n(?P<body>[\s\S]*?)\n?```",
    re.MULTILINE,
)
_CODE_LIKE_PATTERN = re.compile(
    r"\b(def|class|import|from|return|async|await|function|const|let|var|SELECT|INSERT|UPDATE)\b|[{};]",
    re.IGNORECASE,
)
_FAKE_MEDIA_ACTION_PATTERN = re.compile(
    r"^[（(]\s*"
    r"(?=[^）)]*(?:发送|发|附上|贴|来|整|给你发))"
    r"(?=[^）)]*(?:表情包|表情|图片|图|语音|音频|record|image))"
    r"[^）)]*[）)]\s*",
    re.IGNORECASE,
)
_VOICE_STATUS_PATTERNS = (
    "正在语音回复中",
    "语音回复中",
    "现在来一段语音",
    "现在来一句语音",
    "现在发一段语音",
    "给你发一段语音",
    "给你发一句语音",
    "这就发语音",
    "马上发语音",
    "念给你听：",
    "念给你听:",
    "读给你听：",
    "读给你听:",
    "我念一句：",
    "我念一句:",
    "我念叨一句",
    "读完了",
    "念完了",
)
_VOICE_STATUS_PREFIX_PATTERN = re.compile(
    r"^(?:行|好|好的|可以|嗯|ok|OK|那我|我这就|这就)?[\s，,。.!！?？]*"
    r"(?:"
    r"(?:正在语音回复中|语音回复中|念给你听[:：]?|读给你听[:：]?"
    r"|我念一句[:：]?|我念叨一句|读完了|念完了)"
    r"|(?:(?:现在)?(?:给你)?(?:来|发|回|整)(?:一?句|一?段|个)?语音(?:回复)?)"
    r"|(?:给你发(?:一?句|一?段|个)?语音)"
    r"|(?:马上(?:发|回|来)(?:一?句|一?段|个)?语音)"
    r")"
    r"[\s：:，,。.!！?？]*"
)
_EMPTY_REPLY_FALLBACKS = ("卡了", "刚才那句算了", "当我没说")


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
    cleaned = _normalize_code_blocks(cleaned)
    if _is_empty_or_meaningless_reply(cleaned):
        cleaned = _empty_reply_fallback(original)
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
    cleaned = _strip_fake_media_actions(cleaned)
    cleaned = _strip_voice_status_text(cleaned)
    for prefix in ("作为AI语言模型，", "作为 AI 语言模型，"):
        if cleaned.startswith(prefix):
            cleaned = cleaned.removeprefix(prefix)
    return cleaned.strip()


def split_reply_messages(text: str, *, reply_mode: str = "short") -> list[str]:
    text = _clean_code_text(text) if reply_mode == "code_block" else clean_reply_text(text)
    if _is_empty_or_meaningless_reply(text):
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


def _strip_fake_media_actions(text: str) -> str:
    cleaned = text.strip()
    while True:
        next_text = _FAKE_MEDIA_ACTION_PATTERN.sub("", cleaned).strip()
        if next_text == cleaned:
            return cleaned
        cleaned = next_text


def _strip_voice_status_text(text: str) -> str:
    cleaned = text.strip()
    while True:
        next_text = _VOICE_STATUS_PREFIX_PATTERN.sub("", cleaned).strip()
        if next_text == cleaned:
            break
        cleaned = next_text
    for prefix in _VOICE_STATUS_PATTERNS:
        if cleaned.startswith(prefix):
            cleaned = cleaned.removeprefix(prefix).strip(" ：:，,。.!！?？")
    return cleaned


def _empty_reply_fallback(text: str) -> str:
    seed = sum(ord(char) for char in str(text or ""))
    return _EMPTY_REPLY_FALLBACKS[seed % len(_EMPTY_REPLY_FALLBACKS)]


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
    cleaned = _strip_orphan_fences(cleaned)
    return _normalize_code_blocks(cleaned)


def _is_empty_or_meaningless_reply(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return True
    without_fences = re.sub(r"`+", "", compact).strip()
    if not without_fences:
        return True
    for match in _FENCED_BLOCK_DETAIL_PATTERN.finditer(compact):
        body = _unwrap_quoted_code(match.group("body"))
        if body.strip():
            return False
    if _FENCED_BLOCK_PATTERN.fullmatch(compact):
        return True
    return False


def _normalize_code_blocks(text: str) -> str:
    if not text:
        return text
    if _FENCED_BLOCK_DETAIL_PATTERN.search(text):
        return _FENCED_BLOCK_DETAIL_PATTERN.sub(_normalize_fenced_code_match, text)
    if _looks_like_standalone_code(text):
        return _format_code_body(_unwrap_quoted_code(text), _guess_code_language(text))
    return text


def _normalize_fenced_code_match(match: re.Match[str]) -> str:
    label = match.group("label").strip().lower()
    body = _unwrap_quoted_code(match.group("body").strip())
    formatted = _format_code_body(body, label)
    return f"```\n{formatted}\n```"


def _unwrap_quoted_code(text: str) -> str:
    candidate = text.strip()
    for quote in ('"', "'"):
        if candidate.startswith(quote) and candidate.endswith(quote) and len(candidate) >= 2:
            candidate = candidate[1:-1]
            break
    return (
        candidate.replace("\\n", "\n")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .strip()
    )


def _guess_code_language(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\b(def|import|from|elif|lambda|print)\b", lowered):
        return "python"
    if re.search(r"#include|std::|int\s+main|void\s+\w+\s*\(|\{|\}", text):
        return "cpp"
    return ""


def _format_code_body(body: str, language: str = "") -> str:
    language = (language or _guess_code_language(body)).lower()
    body = body.strip()
    if not body:
        return body
    if language in {"py", "python"}:
        return _format_python_like_code(body)
    if language in {"c", "cc", "cpp", "c++", "h", "hpp"} or _guess_code_language(body) == "cpp":
        return _format_c_like_code(body)
    return _normalize_code_indentation(body)


def _format_python_like_code(body: str) -> str:
    lines = _normalize_code_indentation(body).splitlines()
    formatted: list[str] = []
    indent = 0
    dedent_prefixes = ("elif ", "else:", "except", "finally:")
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(dedent_prefixes):
            indent = max(0, indent - 1)
        formatted.append("    " * indent + line)
        if line.endswith(":") and not line.startswith("#"):
            indent += 1
        if line.startswith(("return", "raise", "break", "continue")):
            indent = max(0, indent - 1)
    return "\n".join(formatted)


def _format_c_like_code(body: str) -> str:
    raw_lines = _c_like_statement_lines(body)
    formatted: list[str] = []
    indent = 0
    for line in raw_lines:
        if line.startswith("}"):
            indent = max(0, indent - 1)
        formatted.append("    " * indent + line)
        opens = line.count("{")
        closes = line.count("}")
        indent = max(0, indent + opens - closes)
    return "\n".join(formatted)


def _c_like_statement_lines(body: str) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    quote = ""
    escape = False
    paren_depth = 0

    def flush() -> None:
        line = re.sub(r"\s+", " ", "".join(current)).strip()
        current.clear()
        if line:
            lines.append(line)

    for ch in body.strip():
        if quote:
            current.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in {'"', "'"}:
            quote = ch
            current.append(ch)
            continue
        if ch == "(":
            paren_depth += 1
            current.append(ch)
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            current.append(ch)
            continue
        if ch in "\r\n":
            flush()
            continue
        if ch == "{":
            flush()
            lines.append("{")
            continue
        if ch == "}":
            flush()
            lines.append("}")
            continue
        if ch == ";" and paren_depth == 0:
            current.append(ch)
            flush()
            continue
        current.append(" " if ch == "\t" else ch)
    flush()
    return lines


def _normalize_code_indentation(body: str) -> str:
    lines = [line.rstrip() for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return ""
    indents = [len(line) - len(line.lstrip(" ")) for line in non_empty if line.startswith(" ")]
    common = min(indents) if indents else 0
    if common <= 0:
        return "\n".join(line.strip() for line in lines)
    return "\n".join(line[common:] if line.startswith(" " * common) else line.strip() for line in lines)
