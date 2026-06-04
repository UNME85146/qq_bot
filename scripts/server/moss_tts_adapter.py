#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import types
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
        if request.format.lower() != "wav":
            raise HTTPException(status_code=400, detail="only wav is supported in v1")
        profile = state.profile_for(request.voiceProfileId)
        started_at = time.perf_counter()
        output_path = state.output_dir / _output_name(text, profile.id)
        result = state.runtime.synthesize(
            text=text,
            voice=profile.voice,
            prompt_audio_path=profile.promptAudioPath,
            output_audio_path=output_path,
            sample_mode="fixed",
            do_sample=True,
            streaming=False,
            max_new_frames=_max_new_frames_for_text(text, state.max_new_frames),
            voice_clone_max_text_tokens=state.voice_clone_max_text_tokens,
            enable_wetext=False,
            enable_normalize_tts_text=True,
        )
        generated_path = Path(str(result["audio_path"]))
        final_path = _normalize_wav(generated_path, output_path)
        sample_rate, channels, duration_ms = _wav_info(final_path)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        return JSONResponse(
            {
                "audio_path": str(final_path),
                "duration_ms": duration_ms,
                "sample_rate": sample_rate,
                "channels": channels,
                "execution_provider": state.execution_provider,
                "voice_profile_id": profile.id,
                "elapsed_ms": elapsed_ms,
            }
        )

    return app


def _output_name(text: str, profile_id: str) -> str:
    digest = hashlib.sha256(f"{profile_id}\0{text}".encode("utf-8")).hexdigest()[:24]
    return f"moss-tts-{profile_id}-{digest}.wav"


def _max_new_frames_for_text(text: str, configured_max: int) -> int:
    compact = "".join(str(text or "").split())
    length = len(compact)
    if length <= 0:
        return configured_max
    if length <= 4:
        return min(configured_max, 120)
    if length <= 8:
        return min(configured_max, 160)
    if length <= 16:
        return min(configured_max, 220)
    return configured_max


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
