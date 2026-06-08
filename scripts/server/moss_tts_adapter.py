#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from starlette.responses import JSONResponse


class VoiceProfile(BaseModel):
    id: str
    voice: str
    language: str = "zh"
    gender: str = "neutral"
    promptAudioPath: str | None = None
    enabled: bool = True


class TTSRequest(BaseModel):
    text: str
    voiceProfileId: str
    format: str = "wav"
    traceId: str | None = None
    exactText: bool = False


@dataclass(frozen=True)
class TTSGenerationProfile:
    name: str
    sample_mode: str
    do_sample: bool
    max_new_frames: int
    enable_normalize_tts_text: bool
    audio_temperature: float
    audio_top_p: float
    audio_top_k: int
    audio_repetition_penalty: float


class AdapterState:
    def __init__(
        self,
        *,
        repo_dir: Path,
        model_dir: Path,
        output_dir: Path,
        execution_provider: str,
        cpu_threads: int,
        max_new_frames: int,
        voice_clone_max_text_tokens: int,
        profiles: list[VoiceProfile],
    ) -> None:
        self.repo_dir = repo_dir
        self.model_dir = model_dir
        self.output_dir = output_dir
        self.execution_provider = execution_provider
        self.cpu_threads = cpu_threads
        self.max_new_frames = max_new_frames
        self.voice_clone_max_text_tokens = voice_clone_max_text_tokens
        self.profiles = {profile.id: profile for profile in profiles if profile.enabled}
        self._runtime = None
        self.synthesis_lock = threading.Lock()

    @property
    def runtime(self):
        if self._runtime is None:
            if str(self.repo_dir) not in sys.path:
                sys.path.insert(0, str(self.repo_dir))
            _install_optional_torch_stubs()
            from onnx_tts_runtime import (
                OnnxTtsRuntime,
                _download_default_browser_onnx_assets,
                _find_manifest_path,
            )

            if _find_manifest_path(self.model_dir) is None:
                _download_default_browser_onnx_assets(self.model_dir)
            self._runtime = OnnxTtsRuntime(
                model_dir=self.model_dir,
                thread_count=self.cpu_threads,
                max_new_frames=self.max_new_frames,
                execution_provider=self.execution_provider,
                output_dir=self.output_dir,
            )
            self.execution_provider = str(self._runtime.execution_provider)
        return self._runtime

    def profile_for(self, profile_id: str) -> VoiceProfile:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="voice profile not found")
        return profile


def create_app(state: AdapterState) -> FastAPI:
    app = FastAPI(title="QQ Bot MOSS-TTS-Nano Adapter")

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "provider": "moss_tts_nano",
                "backend": "onnx",
                "executionProvider": state.execution_provider,
                "repoDir": str(state.repo_dir),
                "modelDir": str(state.model_dir),
                "outputDir": str(state.output_dir),
                "outputDirWritable": _writable(state.output_dir),
                "voiceProfileCount": len(state.profiles),
                "stabilityMode": True,
                "generationProfiles": _generation_profile_summary(state.max_new_frames),
            }
        )

    @app.get("/voices")
    def voices() -> JSONResponse:
        builtin: list[dict[str, Any]] = []
        try:
            builtin = _list_builtin_voices(state.runtime)
        except Exception:
            builtin = []
        return JSONResponse(
            {
                "profiles": [profile.dict(exclude={"promptAudioPath"}) for profile in state.profiles.values()],
                "builtinVoices": builtin,
            }
        )

    @app.post("/tts")
    def tts(request: TTSRequest) -> JSONResponse:
        text = request.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        if not _compact_tts_text(text):
            raise HTTPException(status_code=400, detail="text has no speakable content")
        if request.format.lower() != "wav":
            raise HTTPException(status_code=400, detail="only wav is supported in v1")
        profile = state.profile_for(request.voiceProfileId)
        started_at = time.perf_counter()
        output_path = state.output_dir / _output_name(text, profile.id)
        with state.synthesis_lock:
            result, generation_profile, retry_count = _synthesize_with_stability_guard(
                state,
                text=text,
                profile=profile,
                output_path=output_path,
                exact_text=request.exactText,
            )
        final_path = Path(str(result["final_audio_path"]))
        sample_rate, channels, duration_ms = _wav_info(final_path)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        duration_guard_ms = _duration_guard_ms(text, exact_text=request.exactText)
        return JSONResponse(
            {
                "audio_path": str(final_path),
                "duration_ms": duration_ms,
                "sample_rate": sample_rate,
                "channels": channels,
                "execution_provider": state.execution_provider,
                "voice_profile_id": profile.id,
                "elapsed_ms": elapsed_ms,
                "generation_profile": generation_profile.name,
                "max_new_frames": generation_profile.max_new_frames,
                "retry_count": retry_count,
                "duration_guard_ms": duration_guard_ms,
            }
        )

    return app


def _output_name(text: str, profile_id: str) -> str:
    digest = hashlib.sha256(f"{profile_id}\0{text}".encode("utf-8")).hexdigest()[:24]
    return f"moss-tts-{profile_id}-{digest}.wav"


def _max_new_frames_for_text(text: str, configured_max: int) -> int:
    return _generation_profile_for_text(text, configured_max).max_new_frames


def _compact_tts_text(text: str) -> str:
    compact = "".join(str(text or "").split())
    punctuation = set(
        "\uff0c,\u3002.!\uff01?\uff1f\u3001\uff1a:;\"'\u201c\u201d"
        "\u2018\u2019\uff08\uff09()[]\u3010\u3011<>\u300a\u300b"
    )
    return "".join(character for character in compact if character not in punctuation)


def _generation_profile_for_text(
    text: str,
    configured_max: int,
    *,
    exact_text: bool = False,
    rescue: bool = False,
) -> TTSGenerationProfile:
    compact = _compact_tts_text(text)
    length = len(compact)
    cap = max(1, int(configured_max))
    if length <= 0:
        max_new_frames = cap
        profile = TTSGenerationProfile(
            name="default_empty",
            sample_mode="fixed",
            do_sample=True,
            max_new_frames=max_new_frames,
            enable_normalize_tts_text=True,
            audio_temperature=0.65,
            audio_top_p=0.85,
            audio_top_k=16,
            audio_repetition_penalty=1.3,
        )
        return _rescue_profile(profile) if rescue else profile
    if length <= 4:
        profile = TTSGenerationProfile(
            name="short_1_4_exact" if exact_text else "short_1_4",
            sample_mode="greedy",
            do_sample=False,
            max_new_frames=min(cap, 24),
            enable_normalize_tts_text=False,
            audio_temperature=0.55,
            audio_top_p=0.75,
            audio_top_k=10,
            audio_repetition_penalty=1.5,
        )
        return _rescue_profile(profile) if rescue else profile
    if length <= 8:
        profile = TTSGenerationProfile(
            name="short_5_8_exact" if exact_text else "short_5_8",
            sample_mode="greedy",
            do_sample=False,
            max_new_frames=min(cap, 32),
            enable_normalize_tts_text=False,
            audio_temperature=0.55,
            audio_top_p=0.75,
            audio_top_k=10,
            audio_repetition_penalty=1.5,
        )
        return _rescue_profile(profile) if rescue else profile
    if length <= 16:
        profile = TTSGenerationProfile(
            name="medium_9_16_exact" if exact_text else "medium_9_16",
            sample_mode="greedy",
            do_sample=False,
            max_new_frames=min(cap, 56),
            enable_normalize_tts_text=not exact_text,
            audio_temperature=0.6,
            audio_top_p=0.8,
            audio_top_k=12,
            audio_repetition_penalty=1.45,
        )
        return _rescue_profile(profile) if rescue else profile
    if length <= 32:
        profile = TTSGenerationProfile(
            name="medium_17_32_exact" if exact_text else "medium_17_32",
            sample_mode="greedy",
            do_sample=False,
            max_new_frames=min(cap, 96),
            enable_normalize_tts_text=not exact_text,
            audio_temperature=0.6,
            audio_top_p=0.82,
            audio_top_k=14,
            audio_repetition_penalty=1.4,
        )
        return _rescue_profile(profile) if rescue else profile
    if length <= 80:
        profile = TTSGenerationProfile(
            name="long_33_80_exact" if exact_text else "long_33_80",
            sample_mode="greedy",
            do_sample=False,
            max_new_frames=min(cap, 160),
            enable_normalize_tts_text=not exact_text,
            audio_temperature=0.62,
            audio_top_p=0.85,
            audio_top_k=16,
            audio_repetition_penalty=1.35,
        )
        return _rescue_profile(profile) if rescue else profile
    profile = TTSGenerationProfile(
        name="long_80_plus_exact" if exact_text else "long_80_plus",
        sample_mode="greedy" if exact_text else "fixed",
        do_sample=not exact_text,
        max_new_frames=cap if exact_text else min(cap, 240),
        enable_normalize_tts_text=not exact_text,
        audio_temperature=0.65,
        audio_top_p=0.85,
        audio_top_k=16,
        audio_repetition_penalty=1.3,
    )
    return _rescue_profile(profile) if rescue else profile


def _rescue_profile(profile: TTSGenerationProfile) -> TTSGenerationProfile:
    return TTSGenerationProfile(
        name=f"{profile.name}_rescue",
        sample_mode="greedy",
        do_sample=False,
        max_new_frames=max(12, int(profile.max_new_frames * 0.75)),
        enable_normalize_tts_text=profile.enable_normalize_tts_text,
        audio_temperature=min(profile.audio_temperature, 0.55),
        audio_top_p=min(profile.audio_top_p, 0.75),
        audio_top_k=min(profile.audio_top_k, 10),
        audio_repetition_penalty=1.65,
    )


def _apply_generation_defaults(runtime: Any, profile: TTSGenerationProfile) -> dict[str, Any]:
    defaults = runtime.manifest["generation_defaults"]
    snapshot = dict(defaults)
    defaults["max_new_frames"] = int(profile.max_new_frames)
    defaults["sample_mode"] = profile.sample_mode
    defaults["do_sample"] = bool(profile.do_sample)
    defaults["audio_temperature"] = float(profile.audio_temperature)
    defaults["audio_top_p"] = float(profile.audio_top_p)
    defaults["audio_top_k"] = int(profile.audio_top_k)
    defaults["audio_repetition_penalty"] = float(profile.audio_repetition_penalty)
    return snapshot


def _restore_generation_defaults(runtime: Any, snapshot: dict[str, Any]) -> None:
    defaults = runtime.manifest["generation_defaults"]
    defaults.clear()
    defaults.update(snapshot)


def _duration_guard_ms(text: str, *, exact_text: bool = False) -> int:
    length = len(_compact_tts_text(text))
    if length <= 0:
        return 0
    if length <= 4:
        return 2200
    if length <= 8:
        return 3200
    if length <= 16:
        return 5200
    if length <= 32:
        if exact_text:
            return 12800
        return 8000
    if exact_text:
        return min(120000, 3000 + length * 600)
    return min(18000, 1600 + length * 220)


def _duration_out_of_bounds(
    text: str,
    duration_ms: int | None,
    *,
    exact_text: bool = False,
) -> bool:
    guard = _duration_guard_ms(text, exact_text=exact_text)
    return guard > 0 and duration_ms is not None and duration_ms > guard


def _generation_profile_summary(configured_max: int) -> list[dict[str, Any]]:
    examples = [
        ("1-4", "aaaa"),
        ("5-8", "aaaaa"),
        ("9-16", "aaaaaaaaa"),
        ("17-32", "a" * 18),
        ("33-80", "a" * 33),
        (">80", "a" * 81),
    ]
    rows = []
    for label, example in examples:
        profile = _generation_profile_for_text(example, configured_max)
        rows.append(
            {
                "range": label,
                "profile": profile.name,
                "sampleMode": profile.sample_mode,
                "doSample": profile.do_sample,
                "maxNewFrames": profile.max_new_frames,
                "audioRepetitionPenalty": profile.audio_repetition_penalty,
            }
        )
    return rows


def _synthesize_once(
    state: AdapterState,
    *,
    text: str,
    profile: VoiceProfile,
    output_path: Path,
    generation_profile: TTSGenerationProfile,
) -> dict[str, Any]:
    runtime = state.runtime
    snapshot = _apply_generation_defaults(runtime, generation_profile)
    try:
        result = runtime.synthesize(
            text=text,
            voice=profile.voice,
            prompt_audio_path=profile.promptAudioPath,
            output_audio_path=output_path,
            sample_mode=generation_profile.sample_mode,
            do_sample=generation_profile.do_sample,
            streaming=False,
            max_new_frames=generation_profile.max_new_frames,
            voice_clone_max_text_tokens=state.voice_clone_max_text_tokens,
            enable_wetext=False,
            enable_normalize_tts_text=generation_profile.enable_normalize_tts_text,
        )
    finally:
        _restore_generation_defaults(runtime, snapshot)
    generated_path = Path(str(result["audio_path"]))
    final_path = _normalize_wav(generated_path, output_path)
    result["final_audio_path"] = str(final_path)
    return result


def _synthesize_with_stability_guard(
    state: AdapterState,
    *,
    text: str,
    profile: VoiceProfile,
    output_path: Path,
    exact_text: bool,
) -> tuple[dict[str, Any], TTSGenerationProfile, int]:
    generation_profile = _generation_profile_for_text(
        text,
        state.max_new_frames,
        exact_text=exact_text,
    )
    result = _synthesize_once(
        state,
        text=text,
        profile=profile,
        output_path=output_path,
        generation_profile=generation_profile,
    )
    _, _, duration_ms = _wav_info(Path(str(result["final_audio_path"])))
    if not _duration_out_of_bounds(text, duration_ms, exact_text=exact_text):
        return result, generation_profile, 0

    rescue_profile = _generation_profile_for_text(
        text,
        state.max_new_frames,
        exact_text=exact_text,
        rescue=True,
    )
    rescue_output_path = output_path.with_name(f"{output_path.stem}.rescue.wav")
    rescue_result = _synthesize_once(
        state,
        text=text,
        profile=profile,
        output_path=rescue_output_path,
        generation_profile=rescue_profile,
    )
    _, _, rescue_duration_ms = _wav_info(Path(str(rescue_result["final_audio_path"])))
    if _duration_out_of_bounds(text, rescue_duration_ms, exact_text=exact_text):
        raise HTTPException(status_code=500, detail="duration_out_of_bounds")
    final_path = Path(str(rescue_result["final_audio_path"]))
    if final_path != output_path:
        final_path.replace(output_path)
        rescue_result["final_audio_path"] = str(output_path)
        rescue_result["audio_path"] = str(output_path)
    return rescue_result, rescue_profile, 1


def _normalize_wav(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = os.getenv("QQ_BOT_TTS_FFMPEG", "ffmpeg")
    ffmpeg_target = (
        target.with_name(f"{target.stem}.normalized.tmp.wav")
        if source.resolve() == target.resolve()
        else target
    )
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-ar",
        "24000",
        "-ac",
        "1",
        str(ffmpeg_target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        _assert_wav_shape(ffmpeg_target, sample_rate=24000, channels=1)
        if ffmpeg_target != target:
            ffmpeg_target.replace(target)
        return target
    except Exception as exc:
        if ffmpeg_target != target:
            ffmpeg_target.unlink(missing_ok=True)
        try:
            _assert_wav_shape(source, sample_rate=24000, channels=1)
        except Exception as shape_exc:
            raise HTTPException(
                status_code=500,
                detail=f"failed to normalize wav: {type(exc).__name__}",
            ) from shape_exc
        if source.resolve() != target.resolve():
            target.write_bytes(source.read_bytes())
        return target


def _wav_info(path: Path) -> tuple[int, int, int | None]:
    try:
        import wave

        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            channels = wav.getnchannels()
        return rate, channels, int(frames / rate * 1000) if rate else None
    except Exception:
        raise HTTPException(status_code=500, detail="invalid wav output") from None


def _assert_wav_shape(path: Path, *, sample_rate: int, channels: int) -> None:
    actual_rate, actual_channels, _ = _wav_info(path)
    if actual_rate != sample_rate or actual_channels != channels:
        raise ValueError(
            f"wav shape mismatch: sample_rate={actual_rate}; channels={actual_channels}"
        )


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _list_builtin_voices(runtime: Any) -> list[dict[str, Any]]:
    list_fn = getattr(runtime, "list_builtin_voices", None)
    if callable(list_fn):
        return _safe_builtin_voice_rows(list_fn())
    return []


def _safe_builtin_voice_rows(values: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(values, list):
        values = list(values)
    for item in values:
        if not isinstance(item, dict):
            continue
        voice = str(item.get("voice", "")).strip()
        if not voice:
            continue
        rows.append(
            {
                "voice": voice,
                "display_name": str(item.get("display_name", "")).strip() or "-",
                "group": str(item.get("group", "")).strip() or "-",
            }
        )
    return rows


def _install_optional_torch_stubs() -> None:
    """Allow built-in ONNX voices without installing PyTorch.

    The official ONNX runtime imports torch/torchaudio at module import time,
    but uses them only when promptAudioPath is provided. The bot's first
    service version uses built-in voice profiles, so reference-audio cloning can
    fail explicitly without forcing the whole service to install PyTorch.
    """

    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")
        torch_stub.float32 = "float32"
        sys.modules["torch"] = torch_stub
    if "torchaudio" not in sys.modules:
        torchaudio_stub = types.ModuleType("torchaudio")

        def _missing_torchaudio(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "promptAudioPath requires torch and torchaudio; use a built-in "
                "voice profile or install the full PyTorch runtime"
            )

        torchaudio_stub.load = _missing_torchaudio
        functional = types.SimpleNamespace(resample=_missing_torchaudio)
        torchaudio_stub.functional = functional
        sys.modules["torchaudio"] = torchaudio_stub


def _profiles_from_config(config_path: Path) -> list[VoiceProfile]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    tts = raw.get("tts", {})
    profiles = tts.get("voiceProfiles", [])
    if not isinstance(profiles, list):
        return []
    values = []
    for item in profiles:
        if not isinstance(item, dict):
            continue
        try:
            values.append(VoiceProfile(**item))
        except Exception:
            continue
    return values


def _profiles_from_env(config_path: Path) -> list[VoiceProfile]:
    raw = os.getenv("QQ_BOT_TTS_PROFILES", "").strip()
    if raw:
        values = json.loads(raw)
        return [VoiceProfile(**item) for item in values]
    profiles = _profiles_from_config(config_path)
    if profiles:
        return profiles
    return [
        VoiceProfile(
            id=os.getenv("QQ_BOT_TTS_PROFILE_ID", "xiaohuang_default"),
            voice=os.getenv("QQ_BOT_TTS_VOICE", "Junhao"),
            language=os.getenv("QQ_BOT_TTS_LANGUAGE", "zh"),
            gender=os.getenv("QQ_BOT_TTS_GENDER", "neutral"),
            promptAudioPath=os.getenv("QQ_BOT_TTS_PROMPT_AUDIO") or None,
            enabled=True,
        )
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the QQ bot MOSS-TTS-Nano adapter.")
    parser.add_argument("--host", default=os.getenv("QQ_BOT_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QQ_BOT_TTS_PORT", "18100")))
    parser.add_argument("--config-path", default=os.getenv("QQ_BOT_CONFIG_PATH", "/opt/qq_bot/config/config.json"))
    parser.add_argument("--model-dir", default=os.getenv("QQ_BOT_TTS_MODEL_DIR", "/opt/moss_tts_nano/models"))
    parser.add_argument("--repo-dir", default=os.getenv("QQ_BOT_TTS_REPO_DIR", "/opt/moss_tts_nano/MOSS-TTS-Nano"))
    parser.add_argument("--output-dir", default=os.getenv("QQ_BOT_TTS_OUTPUT_DIR", "/opt/qq_bot/data/tts/cache"))
    parser.add_argument("--execution-provider", choices=("cpu", "cuda"), default=os.getenv("QQ_BOT_TTS_EXECUTION_PROVIDER", "cuda"))
    parser.add_argument("--cpu-threads", type=int, default=int(os.getenv("QQ_BOT_TTS_CPU_THREADS", "4")))
    parser.add_argument("--max-new-frames", type=int, default=int(os.getenv("QQ_BOT_TTS_MAX_NEW_FRAMES", "375")))
    parser.add_argument("--voice-clone-max-text-tokens", type=int, default=int(os.getenv("QQ_BOT_TTS_VOICE_CLONE_MAX_TEXT_TOKENS", "75")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = AdapterState(
        repo_dir=Path(args.repo_dir).expanduser().resolve(),
        model_dir=Path(args.model_dir).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        execution_provider=args.execution_provider,
        cpu_threads=args.cpu_threads,
        max_new_frames=args.max_new_frames,
        voice_clone_max_text_tokens=args.voice_clone_max_text_tokens,
        profiles=_profiles_from_env(Path(args.config_path).expanduser().resolve()),
    )
    app = create_app(state)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
