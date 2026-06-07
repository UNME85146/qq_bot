from __future__ import annotations

import re
import random
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.models import GeneratedReply, NormalizedMessage, TTSConfig, TTSVoiceProfileConfig

RecordSystemEvent = Callable[..., Awaitable[None]]
TTS_SEGMENT_MAX_CHARS = 60


@dataclass(frozen=True)
class TTSGenerationResult:
    audio_path: str
    duration_ms: int | None
    sample_rate: int | None
    channels: int | None
    execution_provider: str
    voice_profile_id: str
    generation_profile: str = ""
    max_new_frames: int | None = None
    retry_count: int = 0
    duration_guard_ms: int | None = None


@dataclass(frozen=True)
class VoiceReplyDecision:
    selected: bool
    reason: str
    speech_text: str = ""
    window_id: int | None = None
    window_index: int | None = None


class TTSService:
    def __init__(
        self,
        config: TTSConfig,
        *,
        record_system_event: RecordSystemEvent,
        now: Callable[[], float] | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._record_system_event = record_system_event
        self._now = now or time.monotonic
        self._client_factory = client_factory
        self._last_attempt_at: dict[str, float] = {}

    async def generate_for_reply(
        self,
        message: NormalizedMessage,
        reply: GeneratedReply,
    ) -> TTSGenerationResult | None:
        skip_reason = self.skip_reason(reply, scope_type=message.scope_type)
        if skip_reason is not None:
            return None
        profile = _current_profile(self._config)
        profile_id = profile.id if profile is not None else self._config.default_voice_profile_id
        speech_text = prepare_tts_speech_text(reply.text)
        return await self.generate_for_text(
            message,
            speech_text,
            voice_profile_id=profile_id,
        )

    async def generate_for_text(
        self,
        message: NormalizedMessage,
        text: str,
        *,
        voice_profile_id: str | None = None,
        exact_short: bool = False,
        ignore_cooldown: bool = False,
        segment_max_chars: int | None = None,
    ) -> TTSGenerationResult | None:
        if segment_max_chars is not None:
            return await self._generate_segmented_for_text(
                message,
                text,
                voice_profile_id=voice_profile_id,
                exact_short=exact_short,
                ignore_cooldown=ignore_cooldown,
                segment_max_chars=segment_max_chars,
            )
        return await self._generate_single_for_text(
            message,
            text,
            voice_profile_id=voice_profile_id,
            exact_short=exact_short,
            ignore_cooldown=ignore_cooldown,
        )

    async def _generate_segmented_for_text(
        self,
        message: NormalizedMessage,
        text: str,
        *,
        voice_profile_id: str | None,
        exact_short: bool,
        ignore_cooldown: bool,
        segment_max_chars: int,
    ) -> TTSGenerationResult | None:
        segments = split_tts_speech_text(
            text,
            exact_short=exact_short,
            max_chars=segment_max_chars,
        )
        if not segments:
            return None
        if len(segments) == 1:
            return await self._generate_single_for_text(
                message,
                segments[0],
                voice_profile_id=voice_profile_id,
                exact_short=exact_short,
                ignore_cooldown=ignore_cooldown,
            )
        await self._record(
            "INFO",
            "tts_segments_selected",
            (
                f"scope={message.scope_type}; segments={len(segments)}; "
                f"chars={sum(len(segment) for segment in segments)}; "
                f"max_segment_chars={max(1, int(segment_max_chars))}; emit=single_record"
            ),
            message.trace_id,
        )
        results: list[TTSGenerationResult] = []
        for index, segment in enumerate(segments):
            result = await self._generate_single_for_text(
                message,
                segment,
                voice_profile_id=voice_profile_id,
                exact_short=exact_short,
                ignore_cooldown=ignore_cooldown or index > 0,
            )
            if result is None:
                return None
            results.append(result)
        return await self._merge_segment_results(message, results)

    async def _generate_single_for_text(
        self,
        message: NormalizedMessage,
        text: str,
        *,
        voice_profile_id: str | None = None,
        exact_short: bool = False,
        ignore_cooldown: bool = False,
    ) -> TTSGenerationResult | None:
        scope_type = message.scope_type
        speech_text = prepare_tts_speech_text(text, exact_short=exact_short)
        if not speech_text:
            return None

        key = _cooldown_key(message)
        cooldown_seconds = (
            self._config.group_cooldown_seconds
            if scope_type == "group"
            else self._config.private_cooldown_seconds
        )
        now = self._now()
        last_attempt_at = self._last_attempt_at.get(key)
        if (
            not ignore_cooldown
            and last_attempt_at is not None
            and now - last_attempt_at < cooldown_seconds
        ):
            await self._record(
                "INFO",
                "tts_rate_limited",
                f"scope={scope_type}; key={key}; cooldown_seconds={cooldown_seconds:g}",
                message.trace_id,
            )
            return None

        profile = _current_profile(self._config)
        profile_id = (
            voice_profile_id
            or (profile.id if profile is not None else self._config.default_voice_profile_id)
        )
        self._last_attempt_at[key] = now
        await self._record(
            "INFO",
            "tts_generate_started",
            (
                f"scope={scope_type}; profile={profile_id}; chars={len(speech_text)}; "
                f"format={self._config.format}; execution_provider={self._config.execution_provider}"
            ),
            message.trace_id,
        )
        started_at = self._now()
        try:
            async with self._client_factory(
                timeout=self._config.request_timeout_seconds,
            ) as client:
                response = await client.post(
                    self._config.endpoint,
                    json=_tts_request_payload(
                        text=speech_text,
                        voiceProfileId=profile_id,
                        format=self._config.format,
                        traceId=message.trace_id,
                        exact_text=exact_short,
                    ),
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            await self._record(
                "ERROR",
                "tts_generate_failed",
                _failure_detail(scope_type, profile_id, exc),
                message.trace_id,
            )
            return None

        audio_path = str(payload.get("audio_path") or "").strip()
        if not audio_path:
            await self._record(
                "ERROR",
                "tts_generate_failed",
                f"scope={scope_type}; profile={profile_id}; reason=missing_audio_path",
                message.trace_id,
            )
            return None

        elapsed_ms = int((self._now() - started_at) * 1000)
        duration_ms = _optional_int(payload.get("duration_ms"))
        sample_rate = _optional_int(payload.get("sample_rate"))
        channels = _optional_int(payload.get("channels"))
        execution_provider = str(
            payload.get("execution_provider") or self._config.execution_provider
        )
        result_profile_id = str(payload.get("voice_profile_id") or profile_id)
        generation_profile = str(payload.get("generation_profile") or "")
        max_new_frames = _optional_int(payload.get("max_new_frames"))
        retry_count = _optional_int(payload.get("retry_count")) or 0
        duration_guard_ms = _optional_int(payload.get("duration_guard_ms"))
        await self._record(
            "INFO",
            "tts_generate_finished",
            (
                f"scope={scope_type}; profile={result_profile_id}; elapsed_ms={elapsed_ms}; "
                f"duration_ms={duration_ms if duration_ms is not None else 'unknown'}; "
                f"execution_provider={execution_provider}; generation_profile={generation_profile or 'unknown'}; "
                f"max_new_frames={max_new_frames if max_new_frames is not None else 'unknown'}; "
                f"retry_count={retry_count}; "
                f"duration_guard_ms={duration_guard_ms if duration_guard_ms is not None else 'unknown'}"
            ),
            message.trace_id,
        )
        return TTSGenerationResult(
            audio_path=audio_path,
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
            execution_provider=execution_provider,
            voice_profile_id=result_profile_id,
            generation_profile=generation_profile,
            max_new_frames=max_new_frames,
            retry_count=retry_count,
            duration_guard_ms=duration_guard_ms,
        )

    async def _merge_segment_results(
        self,
        message: NormalizedMessage,
        results: list[TTSGenerationResult],
    ) -> TTSGenerationResult | None:
        if not results:
            return None
        if self._config.format.lower() != "wav":
            await self._record(
                "ERROR",
                "tts_merge_failed",
                f"scope={message.scope_type}; reason=unsupported_format; format={self._config.format}",
                message.trace_id,
            )
            return None
        output_path = _merged_audio_path(
            self._config.cache_dir,
            trace_id=message.trace_id,
            input_paths=[result.audio_path for result in results],
        )
        try:
            merge_info = _merge_wav_files(
                [Path(result.audio_path) for result in results],
                output_path,
            )
        except Exception as exc:
            await self._record(
                "ERROR",
                "tts_merge_failed",
                (
                    f"scope={message.scope_type}; segments={len(results)}; "
                    f"reason={type(exc).__name__}; detail={str(exc)[:120]}"
                ),
                message.trace_id,
            )
            return None
        first = results[0]
        retry_count = sum(result.retry_count for result in results)
        await self._record(
            "INFO",
            "tts_segments_merged",
            (
                f"scope={message.scope_type}; segments={len(results)}; "
                f"duration_ms={merge_info['duration_ms']}; "
                f"sample_rate={merge_info['sample_rate']}; channels={merge_info['channels']}"
            ),
            message.trace_id,
        )
        return TTSGenerationResult(
            audio_path=str(output_path),
            duration_ms=merge_info["duration_ms"],
            sample_rate=merge_info["sample_rate"],
            channels=merge_info["channels"],
            execution_provider=first.execution_provider,
            voice_profile_id=first.voice_profile_id,
            generation_profile="merged_segments",
            max_new_frames=sum(
                result.max_new_frames or 0
                for result in results
                if result.max_new_frames is not None
            )
            or None,
            retry_count=retry_count,
            duration_guard_ms=None,
        )

    def skip_reason(self, reply: GeneratedReply, *, scope_type: str) -> str | None:
        return tts_candidate_skip_reason(self._config, reply, scope_type=scope_type)

    async def _record(
        self,
        level: str,
        event: str,
        detail: str,
        trace_id: str | None,
    ) -> None:
        await self._record_system_event(
            level=level,
            event=event,
            detail=detail,
            trace_id=trace_id,
        )


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
        config: TTSConfig,
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


def format_tts_status(config: TTSConfig) -> str:
    profile = _current_profile(config)
    profile_text = "none"
    if profile is not None:
        profile_text = (
            f"{profile.id}/{profile.voice}/{profile.language}/{profile.gender}"
        )
    return "\n".join(
        [
            f"tts_enabled={_on_off(config.enabled)}",
            f"tts_private={_on_off(config.private_enabled)} tts_group={_on_off(config.group_enabled)}",
            f"tts_provider={config.provider} backend={config.backend} executionProvider={config.execution_provider}",
            f"tts_endpoint={config.endpoint}",
            f"tts_format={config.format} maxChars={config.max_chars}",
            f"tts_profile={profile_text}",
        ]
    )


def tts_profile_public_text(profile: TTSVoiceProfileConfig, *, current: bool) -> str:
    prefix = "*" if current else "-"
    status = "on" if profile.enabled else "off"
    return (
        f"{prefix} {profile.id} voice={profile.voice} "
        f"language={profile.language} gender={profile.gender} enabled={status}"
    )


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


def split_tts_speech_text(
    text: str,
    *,
    exact_short: bool = False,
    max_chars: int = TTS_SEGMENT_MAX_CHARS,
) -> list[str]:
    speech_text = prepare_tts_speech_text(text, exact_short=exact_short)
    if not speech_text:
        return []
    max_chars = max(1, int(max_chars))
    if len(speech_text) <= max_chars:
        return [speech_text]
    return _pack_tts_segments(_split_speech_chunks(speech_text), max_chars=max_chars)


def extract_explicit_voice_read_text(
    message: NormalizedMessage,
    *,
    allow_group_without_at: bool = False,
) -> str | None:
    if (
        message.scope_type == "group"
        and not message.is_at_self
        and not allow_group_without_at
    ):
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
    if (
        message.scope_type == "group"
        and not message.is_at_self
        and not allow_group_without_at
    ):
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


def tts_scope_disabled_reason(config: TTSConfig, scope_type: str) -> str | None:
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
    config: TTSConfig,
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
    config: TTSConfig,
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


def tts_enabled_for_scope(config: TTSConfig, scope_type: str) -> bool:
    return tts_scope_disabled_reason(config, scope_type) is None


async def record_explicit_voice_selected(
    message: NormalizedMessage,
    *,
    config: TTSConfig,
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


def _current_profile(config: TTSConfig) -> TTSVoiceProfileConfig | None:
    return config.current_profile()


def _profile_id(config: TTSConfig) -> str:
    profile = _current_profile(config)
    return profile.id if profile is not None else config.default_voice_profile_id


def _cooldown_key(message: NormalizedMessage) -> str:
    if message.scope_type == "group":
        return f"group:{message.group_id or message.scope_id}"
    return f"private:{message.user_id}"


def _failure_detail(scope_type: str, profile_id: str, exc: Exception) -> str:
    return (
        f"scope={scope_type}; profile={profile_id}; "
        f"reason={type(exc).__name__}; detail={str(exc)[:120]}"
    )


def _tts_request_payload(
    *,
    text: str,
    voiceProfileId: str,
    format: str,
    traceId: str | None,
    exact_text: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "voiceProfileId": voiceProfileId,
        "format": format,
        "traceId": traceId,
    }
    if exact_text:
        payload["exactText"] = True
    return payload


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merged_audio_path(
    cache_dir: str,
    *,
    trace_id: str | None,
    input_paths: list[str],
) -> Path:
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    safe_trace_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", trace_id or "no-trace").strip("-")
    source_key = abs(hash(tuple(input_paths)))
    return cache_path / f"tts-merged-{safe_trace_id}-{source_key:x}.wav"


def _merge_wav_files(input_paths: list[Path], output_path: Path) -> dict[str, int]:
    if not input_paths:
        raise ValueError("missing input wav files")
    params = None
    total_frames = 0
    frame_chunks: list[bytes] = []
    for input_path in input_paths:
        with wave.open(str(input_path), "rb") as wav:
            current_params = wav.getparams()
            if params is None:
                params = current_params
            elif _wav_format_key(current_params) != _wav_format_key(params):
                raise ValueError(
                    "wav params mismatch: "
                    f"{input_path} has {_wav_format_key(current_params)} "
                    f"expected {_wav_format_key(params)}"
                )
            frames = wav.readframes(wav.getnframes())
            frame_chunks.append(frames)
            total_frames += wav.getnframes()
    if params is None:
        raise ValueError("missing wav params")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        for frames in frame_chunks:
            output.writeframes(frames)
    duration_ms = int(total_frames * 1000 / params.framerate)
    return {
        "duration_ms": duration_ms,
        "sample_rate": int(params.framerate),
        "channels": int(params.nchannels),
    }


def _wav_format_key(params) -> tuple[int, int, int, str, str]:
    return (
        int(params.nchannels),
        int(params.sampwidth),
        int(params.framerate),
        str(params.comptype),
        str(params.compname),
    )


def _on_off(value: bool) -> str:
    return "on" if value else "off"


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
    r"|(?:用语音|语音)(?:说|讲)"
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
