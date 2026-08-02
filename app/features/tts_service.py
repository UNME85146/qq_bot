from __future__ import annotations

import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.models import GeneratedReply, NormalizedMessage, SpeechConfig

RecordSystemEvent = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class VoiceReplyDecision:
    selected: bool
    reason: str
    speech_text: str = ""
    window_id: int | None = None
    window_index: int | None = None


class VoiceReplyDecider:
    def __init__(
        self,
        *,
        window_size: int = 80,
        min_selected: int = 8,
        max_selected: int = 12,
        rng: random.Random | None = None,
    ) -> None:
        self._window_size = window_size
        self._min_selected = min_selected
        self._max_selected = max_selected
        self._rng = rng or random.Random()
        self._window_id = 0
        self._window_index = 0
        self._selected_indexes: set[int] = set()

    async def decide_random(
        self,
        message: NormalizedMessage,
        reply: GeneratedReply,
        *,
        config: SpeechConfig,
        record_system_event: RecordSystemEvent,
    ) -> VoiceReplyDecision:
        speech_text = prepare_tts_speech_text(reply.text)
        skip_reason = tts_candidate_skip_reason(config, reply, scope_type=message.scope_type)
        if skip_reason is not None:
            return VoiceReplyDecision(
                selected=False,
                reason=skip_reason,
                speech_text=speech_text,
            )

        window_id, window_index = self._next_window_position()
        selected = window_index in self._selected_indexes
        profile_id = _profile_id(config)
        await record_system_event(
            level="INFO",
            event="tts_selected_random" if selected else "tts_skipped_random",
            detail=(
                f"scope={message.scope_type}; window={window_id}; index={window_index}; "
                f"profile={profile_id}; chars={len(speech_text)}"
            ),
            trace_id=message.trace_id,
        )
        return VoiceReplyDecision(
            selected=selected,
            reason="random" if selected else "random_not_selected",
            speech_text=speech_text,
            window_id=window_id,
            window_index=window_index,
        )

    def _next_window_position(self) -> tuple[int, int]:
        if self._window_index <= 0 or self._window_index >= self._window_size:
            self._start_window()
        self._window_index += 1
        return self._window_id, self._window_index

    def _start_window(self) -> None:
        self._window_id += 1
        self._window_index = 0
        count = self._rng.randint(self._min_selected, self._max_selected)
        count = max(0, min(count, self._window_size))
        self._selected_indexes = set(
            self._rng.sample(range(1, self._window_size + 1), count)
        )


DEFAULT_VOICE_REPLY_DECIDER = VoiceReplyDecider()


def prepare_tts_speech_text(text: str, *, exact_short: bool = False) -> str:
    cleaned = _normalize_speech_text(text)
    if not cleaned:
        return ""
    if _contains_voice_artifact(cleaned):
        quoted = _extract_artifact_quote(cleaned)
        if quoted:
            return prepare_tts_speech_text(quoted, exact_short=exact_short)
        cleaned = _cut_before_voice_artifact(cleaned)
        cleaned = _remove_voice_artifact_phrases(cleaned)
    cleaned = _collapse_dirty_short_repetition(cleaned)
    cleaned = _normalize_speech_text(cleaned)
    if exact_short and _is_short_read_text(cleaned):
        return cleaned
    return _normalize_tts_punctuation(cleaned)


def extract_explicit_voice_read_text(
    message: NormalizedMessage,
    *,
    allow_group_without_at: bool = False,
) -> str | None:
    if message.scope_type == "group" and not message.is_at_self:
        return None
    text = str(message.text or "").strip()
    if not text:
        return None
    match = _EXPLICIT_READ_RE.search(text)
    if match is None:
        return None
    content = str(match.group("content") or "").strip()
    if not content:
        return None
    speech_text = prepare_tts_speech_text(content, exact_short=True)
    if _is_voice_reply_trailing_particle(speech_text):
        return None
    return speech_text or None


def is_explicit_voice_reply_request(
    message: NormalizedMessage,
    *,
    allow_group_without_at: bool = False,
) -> bool:
    if message.scope_type == "group" and not message.is_at_self:
        return False
    text = str(message.text or "").strip()
    if not text:
        return False
    if (
        extract_explicit_voice_read_text(
            message,
            allow_group_without_at=allow_group_without_at,
        )
        is not None
    ):
        return False
    return _VOICE_REPLY_REQUEST_RE.search(text) is not None


def tts_scope_disabled_reason(config: SpeechConfig, scope_type: str) -> str | None:
    if not config.enabled:
        return "disabled"
    if scope_type == "private" and not config.private_enabled:
        return "private_disabled"
    if scope_type == "group" and not config.group_enabled:
        return "group_disabled"
    if scope_type not in {"private", "group"}:
        return "scope_unsupported"
    return None


def tts_candidate_skip_reason(
    config: SpeechConfig,
    reply: GeneratedReply,
    *,
    scope_type: str,
) -> str | None:
    if not config.enabled:
        return "disabled"
    if scope_type == "private" and not config.private_enabled:
        return "private_disabled"
    if scope_type == "group" and not config.group_enabled:
        return "group_disabled"
    if reply.safety_level != "pass":
        return "safety_not_pass"
    if reply.reply_mode != "short":
        return "reply_mode_not_short"
    if reply.model_name in {"fallback", "local", "rate_limiter", "safety"}:
        return "not_model_reply"
    speech_text = prepare_tts_speech_text(str(reply.text or ""))
    if not speech_text:
        return "empty_speech_text"
    if len(speech_text) > config.max_chars:
        return "too_long"
    return None


def forced_voice_tts_skip_reason(
    config: SpeechConfig,
    reply: GeneratedReply,
    *,
    scope_type: str,
) -> str | None:
    skip_reason = tts_candidate_skip_reason(config, reply, scope_type=scope_type)
    if skip_reason != "not_model_reply" or reply.model_name != "fallback":
        return skip_reason
    speech_text = prepare_tts_speech_text(str(reply.text or ""))
    if not speech_text:
        return "empty_speech_text"
    if len(speech_text) > config.max_chars:
        return "too_long"
    return None


def tts_enabled_for_scope(config: SpeechConfig, scope_type: str) -> bool:
    return tts_scope_disabled_reason(config, scope_type) is None


async def record_explicit_voice_selected(
    message: NormalizedMessage,
    *,
    config: SpeechConfig,
    chars: int,
    record_system_event: RecordSystemEvent,
) -> None:
    await record_system_event(
        level="INFO",
        event="tts_selected_explicit",
        detail=(
            f"scope={message.scope_type}; profile={_profile_id(config)}; chars={chars}"
        ),
        trace_id=message.trace_id,
    )


async def record_tts_fallback_text_sent(
    message: NormalizedMessage,
    *,
    reason: str,
    record_system_event: RecordSystemEvent,
) -> None:
    await record_system_event(
        level="INFO",
        event="tts_fallback_text_sent",
        detail=f"scope={message.scope_type}; reason={reason}",
        trace_id=message.trace_id,
    )


def _profile_id(config: SpeechConfig) -> str:
    return config.voice or "unconfigured"


_VOICE_ARTIFACT_MARKERS = (
    "没语音功能",
    "没有语音功能",
    "语音功能",
    "发不了语音",
    "发不出语音",
    "不能发语音",
    "没法发语音",
    "语音发不出",
    "语音暂时",
    "文字给你念",
    "文字念",
    "文字代替",
    "脑补",
    "将就听",
    "硬件不支持",
    "语音模块",
    "念完了",
    "读完了",
    "朗读完了",
)
_VOICE_CUT_MARKERS = (
    "念完了",
    "读完了",
    "朗读完了",
    "没语音功能",
    "没有语音功能",
    "发不了语音",
    "发不出语音",
    "不能发语音",
    "没法发语音",
    "语音发不出",
    "硬件不支持",
    "语音模块",
    "脑补",
)
_VOICE_ARTIFACT_PHRASES = (
    "没语音功能",
    "没有语音功能",
    "发不了语音",
    "发不出语音",
    "不能发语音",
    "没法发语音",
    "语音发不出",
    "你可以脑补",
    "脑补一下",
    "将就听吧",
    "将就听",
    "念完了",
    "读完了",
    "朗读完了",
)
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]+\]")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_QUOTE_RE = re.compile(r"[“\"']([^“”\"']{1,180})[”\"']")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\ufe0f]+")
_CJK_SINGLE_CHAR_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[了着过吗呢吧啊呀哦呗嘛])")
_SPACE_RE = re.compile(r"\s+")
_EXPLICIT_READ_RE = re.compile(
    r"(?:^|[\s，,。.!！?？])"
    r"(?:再|继续|还)?"
    r"(?:"
    r"(?:用语音|语音)?(?:给我|帮我|替我)?(?:读|念|朗读)"
    r"|(?:发|来|整)(?:一?(?:句|段)|个|条)?语音"
    r"|(?:用语音|语音)(?:说|讲|骂)"
    r")"
    r"(?:一下|一遍|出来|下)?"
    r"[\s：:，,]*"
    r"(?P<content>.+)$"
)
_VOICE_REPLY_REQUEST_RE = re.compile(
    r"(?:^|[\s，,。.!！?？])"
    r"(?:再|继续|还)?"
    r"(?:"
    r"(?:给我|帮我)?(?:发|来|整)(?:一?(?:句|段)|个|条)?语音"
    r"|(?:回|回复|随口说|随便说|说)(?:一?(?:句|段)|个|条)?语音"
    r"|(?:用语音|语音)(?:回|回复)"
    r")"
    r"(?:我|一下|一段|一句|下)?"
    r"(?:吧|呗|嘛|啊|呀|哦|呢|吗|么)?"
    r"[\s，,。.!！?？]*$"
)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;]+")
_TTS_SEGMENT_BOUNDARY_RE = re.compile(r"([^，,、。！？!?；;：:\n]+[，,、。！？!?；;：:\n]*)")
_DUP_PUNCT_RE = re.compile(r"([，。！？!?；;、])\1+")


def _contains_voice_artifact(text: str) -> bool:
    return any(marker in text for marker in _VOICE_ARTIFACT_MARKERS)


def _extract_artifact_quote(text: str) -> str:
    matches = [match.group(1).strip() for match in _QUOTE_RE.finditer(text)]
    matches = [match for match in matches if match]
    if not matches:
        return ""
    return matches[-1]


def _cut_before_voice_artifact(text: str) -> str:
    indexes = [text.find(marker) for marker in _VOICE_CUT_MARKERS if marker in text]
    indexes = [index for index in indexes if index >= 0]
    if not indexes:
        return text
    return text[: min(indexes)]


def _remove_voice_artifact_phrases(text: str) -> str:
    cleaned = text
    for phrase in _VOICE_ARTIFACT_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    return cleaned


def _collapse_dirty_short_repetition(text: str) -> str:
    cleaned = _normalize_speech_text(text)
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(cleaned) if part.strip()]
    if len(parts) < 2:
        return cleaned
    first = parts[0]
    if not _is_short_read_text(first):
        return cleaned
    rest = "".join(parts[1:])
    if first in rest:
        return first
    if len(first) >= 2 and first[-2:] in rest and len(rest) <= len(first) + 4:
        return first
    return cleaned


def _is_short_read_text(text: str) -> bool:
    compact = re.sub(r"[\s，。！？!?；;、：:]+", "", str(text or ""))
    return 2 <= len(compact) <= 8


def _is_voice_reply_trailing_particle(text: str) -> bool:
    return re.sub(r"[\s，。！？!?；;、：:]+", "", str(text or "")) in {
        "吧",
        "呗",
        "嘛",
        "啊",
        "呀",
        "哦",
        "呢",
    }


def _normalize_tts_punctuation(text: str) -> str:
    cleaned = _DUP_PUNCT_RE.sub(r"\1", str(text or ""))
    cleaned = cleaned.replace("——", "，")
    cleaned = cleaned.replace("—", "，")
    cleaned = cleaned.replace("……", "。")
    cleaned = cleaned.replace("…", "。")
    cleaned = re.sub(r"\s+([，。！？!?；;、：:])", r"\1", cleaned)
    cleaned = re.sub(r"([，。！？!?；;、：:])\s+", r"\1", cleaned)
    return cleaned.strip(" \t\r\n，。！？!?~～、；;：:")

def _split_speech_chunks(text: str) -> list[str]:
    chunks = [match.group(1).strip() for match in _TTS_SEGMENT_BOUNDARY_RE.finditer(text)]
    chunks = [chunk for chunk in chunks if chunk]
    return chunks or [text]


def _pack_tts_segments(chunks: list[str], *, max_chars: int) -> list[str]:
    segments: list[str] = []
    current = ""
    for chunk in chunks:
        if len(chunk) > max_chars:
            if current:
                segments.append(_trim_tts_segment(current))
                current = ""
            segments.extend(_hard_split_tts_chunk(chunk, max_chars=max_chars))
            continue
        candidate = current + chunk if current else chunk
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            segments.append(_trim_tts_segment(current))
        current = chunk
    if current:
        segments.append(_trim_tts_segment(current))
    return [segment for segment in segments if segment]


def _hard_split_tts_chunk(chunk: str, *, max_chars: int) -> list[str]:
    return [
        _trim_tts_segment(chunk[index : index + max_chars])
        for index in range(0, len(chunk), max_chars)
        if _trim_tts_segment(chunk[index : index + max_chars])
    ]


def _trim_tts_segment(text: str) -> str:
    return str(text or "").strip(" \t\r\n，,、；;：:")


def _normalize_speech_text(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    cleaned = _CQ_CODE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = cleaned.replace("```", "")
    cleaned = cleaned.replace("`", "")
    cleaned = cleaned.replace("*", "")
    cleaned = cleaned.replace("_", "")
    cleaned = _SPACE_RE.sub(" ", cleaned)
    cleaned = _CJK_SINGLE_CHAR_SPACE_RE.sub("", cleaned)
    return cleaned.strip(" \t\r\n，。！？!?~～、；;：:")
