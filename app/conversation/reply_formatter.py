from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_FENCED_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_FENCED_BLOCK_DETAIL_PATTERN = re.compile(
    r"```[ \t]*(?P<label>[A-Za-z0-9_+#.-]*)[ \t]*\n(?P<body>[\s\S]*?)\n?```",
    re.MULTILINE,
)
_CODE_LIKE_PATTERN = re.compile(
    r"\b(def|class|import|from|return|async|await|function|const|let|var|SELECT|INSERT|UPDATE)\b|[{};]",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_LINE_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$")
_WHOLE_LINE_MARKDOWN_WRAPPER_PATTERNS = (
    re.compile(r"^\s*\*\*(.+?)\*\*\s*$"),
    re.compile(r"^\s*__(.+?)__\s*$"),
    re.compile(r"^\s*\*(.+?)\*\s*$"),
    re.compile(r"^\s*_(.+?)_\s*$"),
)
_SECTION_HEADING_MARKER_PATTERN = re.compile(
    r"^(?:"
    r"[一二三四五六七八九十百千万零〇两]{1,6}[、.．]"
    r"|第[一二三四五六七八九十百千万零〇两]{1,6}[章节篇]"
    r"|[（(][一二三四五六七八九十百千万零〇两0-9]{1,6}[）)]"
    r"|[0-9]{1,3}[、.．)]"
    r"|[①②③④⑤⑥⑦⑧⑨⑩]"
    r")\s*\S+"
)
_TITLE_SENTENCE_ENDINGS = "。！？!?；;"
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
_QUALITY_EMPTY_FALLBACK = "我先不展开。"
_QUALITY_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_QUALITY_ZERO_WIDTH_PATTERN = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_QUALITY_HTML_PATTERN = re.compile(r"</?[^>]{1,40}>")
_QUALITY_ROLE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:assistant|system|user|tool|回复|正文|回答|系统提示)\s*[:：]?\s*",
    re.IGNORECASE,
)
_QUALITY_MARKDOWN_ROLE_PATTERN = re.compile(
    r"^\s*\*{1,2}(?:assistant|system|user|tool)\s*:\s*\*{1,2}\s*",
    re.IGNORECASE,
)
_QUALITY_BEARER_PATTERN = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_QUALITY_API_KEY_PATTERN = re.compile(
    r"\b(api[_-]?key|token|authorization)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_QUALITY_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_QUALITY_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_QUALITY_IP_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_QUALITY_TRACE_PATTERN = re.compile(r"\b(?:trace|trace_id|request_id)=?[0-9a-f]{32}\b", re.IGNORECASE)
_QUALITY_PATH_PATTERN = re.compile(r"(?<!\w)(?:[A-Za-z]:\\|/home/|/opt/)[^\s]+")
_QUALITY_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{7,}(?!\d)")
_QUALITY_REPEAT_PUNCTUATION_PATTERN = re.compile(r"([!?！？])\1{1,}")
_QUALITY_REPEAT_SENTENCE_PATTERN = re.compile(
    r"(?P<sentence>[^\n。！？!?；;]{1,40}[。！？!?；;])(?P=sentence)+"
)
_QUALITY_REPEAT_LINE_PATTERN = re.compile(r"^(?P<line>.+)\n(?P=line)$", re.MULTILINE)


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
        cleaned = clean_reply_text(text, apply_quality=False)
        return truncate_naturally(cleaned, self._max_length)

    def format_unlimited(self, text: str, *, reply_mode: str = "short") -> str:
        if reply_mode == "code_block":
            return _clean_code_text(text)
        return _clean_reply_text_for_mode(text, reply_mode=reply_mode)


def parse_model_reply(text: str, *, apply_quality: bool = False) -> ReplyParseResult:
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
    cleaned = clean_reply_text(original, apply_quality=apply_quality)
    cleaned = _strip_labeled_prefix(cleaned)
    cleaned = _strip_orphan_fences(cleaned)
    cleaned = clean_reply_text(cleaned, apply_quality=apply_quality)
    cleaned = _normalize_code_blocks(cleaned)
    if _is_empty_or_meaningless_reply(cleaned):
        cleaned = _empty_reply_fallback(original)
    if reply_mode == "short":
        reply_mode = detect_reply_mode(cleaned)
    if reply_mode in {"long_text", "single_message_long"}:
        cleaned = _clean_reply_text_for_mode(cleaned, reply_mode=reply_mode)
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


def clean_reply_text(text: str, *, apply_quality: bool = True) -> str:
    cleaned = str(text or "").strip()
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    if apply_quality:
        cleaned = _repair_recent_group_quality(cleaned)
    cleaned = _strip_fake_media_actions(cleaned)
    cleaned = _strip_voice_status_text(cleaned)
    for prefix in ("作为AI语言模型，", "作为 AI 语言模型，"):
        if cleaned.startswith(prefix):
            cleaned = cleaned.removeprefix(prefix)
    return cleaned.strip()


def _repair_recent_group_quality(text: str) -> str:
    """Apply deterministic, privacy-safe cleanup before a reply reaches QQ."""
    cleaned = _QUALITY_CONTROL_PATTERN.sub("", str(text or ""))
    cleaned = _QUALITY_ZERO_WIDTH_PATTERN.sub("", cleaned)
    cleaned = _QUALITY_HTML_PATTERN.sub("", cleaned)
    cleaned = _QUALITY_MARKDOWN_ROLE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"^(?:系统提示词要求我回复|我需要遵循系统提示词回复)\s*", "", cleaned)
    cleaned = _QUALITY_ROLE_PREFIX_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"^我是\s*AI(?:模型|助手)?\s*[,，：:]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = _unwrap_quality_payload(cleaned)
    if not cleaned:
        return _QUALITY_EMPTY_FALLBACK

    cleaned = _QUALITY_BEARER_PATTERN.sub("[redacted]", cleaned)
    cleaned = _QUALITY_API_KEY_PATTERN.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        cleaned,
    )
    cleaned = _QUALITY_PHONE_PATTERN.sub("[phone]", cleaned)
    cleaned = _QUALITY_EMAIL_PATTERN.sub("[email]", cleaned)
    cleaned = _QUALITY_IP_PATTERN.sub("[ip]", cleaned)
    cleaned = _QUALITY_TRACE_PATTERN.sub("trace=[trace]", cleaned)
    cleaned = _QUALITY_PATH_PATTERN.sub("[path]", cleaned)
    cleaned = _QUALITY_LONG_NUMBER_PATTERN.sub("[number]", cleaned)

    if _contains_unsafe_group_claim(cleaned):
        return _unsafe_group_claim_replacement(cleaned)

    cleaned = re.sub(r"哈哈{2,}", "哈哈", cleaned)
    cleaned = re.sub(r"😂{2,}", "😂", cleaned)
    cleaned = re.sub(r"(?:早睡){2,}", "早睡", cleaned)
    cleaned = re.sub(r"(?:对不起){2,}", "对不起", cleaned)
    cleaned = re.sub(r"(?:收到){2,}", "收到", cleaned)
    cleaned = re.sub(r"(?:有什么想问的吗[？?]){2,}", "有什么想问的吗？", cleaned)
    cleaned = _QUALITY_REPEAT_SENTENCE_PATTERN.sub(r"\g<sentence>", cleaned)
    cleaned = _QUALITY_REPEAT_LINE_PATTERN.sub(r"\g<line>", cleaned)
    cleaned = _QUALITY_REPEAT_PUNCTUATION_PATTERN.sub(r"\1", cleaned)
    cleaned = re.sub(r"[？?！!]\s+", lambda match: match.group(0).strip(), cleaned)
    cleaned = cleaned.replace(",", "，").replace("!", "！")
    cleaned = re.sub(r"([，。！？；：])\s+", r"\1", cleaned)
    cleaned = re.sub(r"[？?]{2,}", "？", cleaned)
    cleaned = re.sub(r"[！!]{2,}", "！", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = cleaned.strip()

    if not cleaned or cleaned.lower() in {
        "null",
        "none",
        "undefined",
        "nan",
        "[object object]",
        "[redacted]",
    }:
        return _QUALITY_EMPTY_FALLBACK
    if len(cleaned) > 160 and _is_single_repeated_fragment(cleaned):
        return _QUALITY_EMPTY_FALLBACK
    return cleaned


def _unwrap_quality_payload(text: str) -> str:
    candidate = text.strip()
    if not candidate or candidate[0] not in "[{" or candidate[-1] not in "]}":
        return candidate
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate
    if isinstance(payload, dict):
        if str(payload.get("role", "")).lower() == "system":
            return ""
        for key in ("reply_text", "text", "content", "reply"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        if "reply_text" in payload:
            return "卡了"
        return ""
    return ""


def _contains_unsafe_group_claim(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "男同",
            "诈骗犯",
            "犯罪分子",
            "露骨色情",
            "色情描写",
            "黄段子",
            "打死",
            "杀了",
            "弄死",
            "住址",
            "住在",
            "在哪上班",
            "工作地点",
            "上班",
            "查了下聊天记录",
            "看了群记录",
            "数据库里",
            "群记录",
        )
    )


def _unsafe_group_claim_replacement(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("住址", "住在")):
        return "我不传播他人的住址或位置。"
    if any(marker in lowered for marker in ("在哪上班", "工作地点", "上班")):
        return "我不根据群聊猜测位置。"
    if any(marker in lowered for marker in ("打死", "杀了", "弄死")):
        return "别上升到伤害别人。"
    if any(marker in lowered for marker in ("露骨色情", "色情描写", "黄段子")):
        return "这个话题我不展开。"
    if any(marker in lowered for marker in ("诈骗犯", "犯罪分子")):
        return "我不替群成员下结论。"
    suffix = "。" if text.rstrip().endswith(tuple("。！？!?")) else ""
    return f"我不对群成员做标签{suffix}"


def _is_single_repeated_fragment(text: str) -> bool:
    compact = text.strip()
    for size in range(2, min(80, len(compact) // 2 + 1)):
        if len(compact) % size == 0 and compact == compact[:size] * (len(compact) // size):
            return True
    return False


def _clean_reply_text_for_mode(text: str, *, reply_mode: str) -> str:
    if reply_mode not in {"long_text", "single_message_long"}:
        return clean_reply_text(text, apply_quality=False)
    cleaned = str(text or "").strip()
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = _strip_fake_media_actions(cleaned)
    cleaned = _strip_voice_status_text(cleaned)
    for prefix in ("作为AI语言模型，", "作为 AI 语言模型，"):
        if cleaned.startswith(prefix):
            cleaned = cleaned.removeprefix(prefix)
    cleaned = _normalize_long_text_markdown_structure(cleaned)
    return cleaned.strip()


def split_reply_messages(text: str, *, reply_mode: str = "short") -> list[str]:
    text = (
        _clean_code_text(text)
        if reply_mode == "code_block"
        else _clean_reply_text_for_mode(text, reply_mode=reply_mode)
    )
    if _is_empty_or_meaningless_reply(text):
        return []
    if reply_mode == "single_message_long":
        return [text]
    if reply_mode in {"long_text", "code_block"}:
        return _split_long_or_code_text(text)
    messages: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in _split_short_line(line):
            message = part.strip()
            if message and message != "```":
                messages.append(message)
    return messages or [text]


def truncate_naturally(text: str, max_length: int, *, reply_mode: str = "short") -> str:
    if len(text) <= max_length:
        return text
    if reply_mode in {"long_text", "code_block"}:
        pieces = split_reply_messages(text, reply_mode=reply_mode)
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
    if value in {"single_message", "single_message_long", "technical"}:
        return "single_message_long"
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


def _normalize_long_text_markdown_structure(text: str) -> str:
    if not text:
        return text
    parts: list[str] = []
    cursor = 0
    for match in _FENCED_BLOCK_PATTERN.finditer(text):
        before = text[cursor : match.start()]
        if before:
            normalized_before = _normalize_markdown_prose_block(before)
            if normalized_before:
                parts.append(normalized_before)
        parts.append(match.group(0).strip())
        cursor = match.end()
    tail = text[cursor:]
    if tail:
        normalized_tail = _normalize_markdown_prose_block(tail)
        if normalized_tail:
            parts.append(normalized_tail)
    return "\n".join(part for part in parts if part.strip()).strip()


def _normalize_markdown_prose_block(text: str) -> str:
    lines = [_strip_markdown_heading_line(line) for line in text.splitlines()]
    lines = _drop_initial_title_lines(lines)
    lines = _merge_section_heading_lines(lines)
    return "\n".join(line for line, _was_markdown_heading in lines if line.strip()).strip()


def _strip_markdown_heading_line(line: str) -> tuple[str, bool]:
    stripped = line.strip()
    was_markdown_heading = False
    heading_match = _MARKDOWN_HEADING_LINE_PATTERN.fullmatch(stripped)
    if heading_match:
        stripped = heading_match.group(1).strip()
        was_markdown_heading = True
    stripped, was_wrapped = _strip_whole_line_markdown_wrappers(stripped)
    was_markdown_heading = was_markdown_heading or was_wrapped
    heading_match = _MARKDOWN_HEADING_LINE_PATTERN.fullmatch(stripped)
    if heading_match:
        stripped = heading_match.group(1).strip()
        was_markdown_heading = True
    return stripped.strip(), was_markdown_heading


def _strip_whole_line_markdown_wrappers(text: str) -> tuple[str, bool]:
    stripped = text.strip()
    was_wrapped = False
    changed = True
    while changed:
        changed = False
        for pattern in _WHOLE_LINE_MARKDOWN_WRAPPER_PATTERNS:
            match = pattern.fullmatch(stripped)
            if not match:
                continue
            inner = match.group(1).strip()
            if not inner:
                return "", True
            stripped = inner
            changed = True
            was_wrapped = True
            break
    return stripped, was_wrapped


def _drop_initial_title_lines(lines: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    result = [(line.strip(), was_markdown_heading) for line, was_markdown_heading in lines if line.strip()]
    while len(result) >= 2 and _looks_like_drop_only_title(result[0], result[1:]):
        result.pop(0)
    return result


def _looks_like_drop_only_title(
    line: tuple[str, bool],
    following_lines: list[tuple[str, bool]],
) -> bool:
    stripped, was_markdown_heading = line
    if not was_markdown_heading:
        return False
    if not stripped or _is_section_heading_line(stripped):
        return False
    if _ends_like_sentence(stripped):
        return False
    if len(stripped) > 24:
        return False
    if not any(next_line.strip() for next_line, _was_markdown_heading in following_lines):
        return False
    return True


def _merge_section_heading_lines(lines: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    result: list[tuple[str, bool]] = []
    index = 0
    while index < len(lines):
        line, was_markdown_heading = lines[index]
        line = line.strip()
        if not line:
            index += 1
            continue
        if _is_section_heading_line(line) and index + 1 < len(lines):
            next_line, next_was_markdown_heading = lines[index + 1]
            next_line = next_line.strip()
            if next_line and not _is_section_heading_line(next_line):
                result.append((
                    _join_section_heading_with_body(line, next_line),
                    was_markdown_heading or next_was_markdown_heading,
                ))
                index += 2
                continue
        result.append((line, was_markdown_heading))
        index += 1
    return result


def _is_section_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _ends_like_sentence(stripped):
        return False
    return bool(_SECTION_HEADING_MARKER_PATTERN.match(stripped))


def _join_section_heading_with_body(heading: str, body: str) -> str:
    heading = heading.strip()
    body = body.strip()
    if not heading:
        return body
    if not body:
        return heading
    if heading.endswith(("：", ":")):
        return f"{heading}{body}"
    return f"{heading}：{body}"


def _ends_like_sentence(text: str) -> bool:
    return text.rstrip().endswith(tuple(_TITLE_SENTENCE_ENDINGS))


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
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    if not paragraphs:
        return []
    parts: list[str] = []
    for paragraph in paragraphs:
        parts.extend(_split_long_paragraph(paragraph))
    return parts


def _split_long_paragraph(text: str, max_chars: int = 420) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for part in _split_short_line(text):
        sentence = part.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(
                sentence[index : index + max_chars]
                for index in range(0, len(sentence), max_chars)
            )
            continue
        if current and len(current) + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts or [text]


def _split_short_line(text: str) -> list[str]:
    protected_separators = _protected_separator_indexes(text)
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        is_terminal = (
            char in "。！？!?；;." and index not in protected_separators
        )
        if not is_terminal:
            index += 1
            continue
        end = index + 1
        while end < len(text):
            next_char = text[end]
            if (
                next_char in "。！？!?；;."
                and end not in protected_separators
            ):
                end += 1
                continue
            break
        part = text[start:end].strip()
        if part:
            parts.append(part)
        start = end
        index = end
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts or ([text] if text else [])


def _protected_separator_indexes(text: str) -> set[int]:
    protected: set[int] = set()
    for match in re.finditer(r"https?://[^\s<>\"']+", text, re.IGNORECASE):
        protected_end = match.end()
        while protected_end > match.start() and text[protected_end - 1] in ".!?;,":
            protected_end -= 1
        protected.update(
            index
            for index in range(match.start(), protected_end)
            if text[index] in ".!?;"
        )
    for match in re.finditer(r"(?:[A-Za-z]\.){2,}", text):
        protected.update(
            index
            for index in range(match.start(), match.end())
            if text[index] == "."
        )
    for match in re.finditer(
        r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|e\.g|i\.e)\.",
        text,
        re.IGNORECASE,
    ):
        protected.update(
            index
            for index in range(match.start(), match.end())
            if text[index] == "."
        )
    for index, char in enumerate(text):
        if char != "." or index == 0 or index + 1 >= len(text):
            continue
        if text[index - 1].isdigit() and text[index + 1].isdigit():
            protected.add(index)
        elif text[index - 1].isascii() and text[index + 1].isascii():
            if text[index - 1].isalnum() and text[index + 1].isalnum():
                protected.add(index)
    return protected


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
