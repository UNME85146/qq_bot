#!/usr/bin/env bash
set -euo pipefail

ROOT="${QQ_BOT_ROOT:-/opt/qq_bot}"
TTS_ROOT="${QQ_BOT_TTS_ROOT:-/opt/moss_tts_nano}"
REPO_DIR="${QQ_BOT_TTS_REPO_DIR:-${TTS_ROOT}/MOSS-TTS-Nano}"
MODEL_DIR="${QQ_BOT_TTS_MODEL_DIR:-${TTS_ROOT}/models}"
OUTPUT_DIR="${QQ_BOT_TTS_OUTPUT_DIR:-${ROOT}/data/tts/cache}"
SERVICE_NAME="${QQ_BOT_TTS_SERVICE:-qq-bot-tts.service}"
ENV_NAME="${QQ_BOT_TTS_ENV_NAME:-moss-tts-nano}"
EXECUTION_PROVIDER="${QQ_BOT_TTS_EXECUTION_PROVIDER:-cuda}"

mkdir -p "$TTS_ROOT" "$MODEL_DIR" "$OUTPUT_DIR"

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone https://github.com/OpenMOSS/MOSS-TTS-Nano.git "$REPO_DIR"
fi

if command -v micromamba >/dev/null 2>&1; then
  micromamba create -y -n "$ENV_NAME" python=3.12 || true
  PYTHON="$(micromamba run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)')"
  PIP=(micromamba run -n "$ENV_NAME" python -m pip)
elif command -v conda >/dev/null 2>&1; then
  conda create -y -n "$ENV_NAME" python=3.12 || true
  PYTHON="$(conda run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)')"
  PIP=(conda run -n "$ENV_NAME" python -m pip)
else
  python3 -m venv "${TTS_ROOT}/.venv"
  PYTHON="${TTS_ROOT}/.venv/bin/python"
  PIP=("$PYTHON" -m pip)
fi

"${PIP[@]}" install --upgrade pip wheel
(
  cd "$REPO_DIR"
  "${PIP[@]}" install \
    "numpy>=1.24" \
    "fastapi>=0.110.0" \
    "pydantic>=2" \
    "uvicorn>=0.29.0" \
    "python-multipart>=0.0.9" \
    "sentencepiece>=0.1.99" \
    "transformers==4.57.1" \
    "huggingface_hub>=0.23,<1.0" \
    "soundfile"
  "${PIP[@]}" install -e . --no-deps
  if [ "$EXECUTION_PROVIDER" = "cuda" ]; then
    "${PIP[@]}" uninstall -y onnxruntime || true
    "${PIP[@]}" install \
      "onnxruntime-gpu>=1.20.0" \
      "nvidia-cublas-cu12" \
      "nvidia-cuda-runtime-cu12" \
      "nvidia-cuda-nvrtc-cu12" \
      "nvidia-curand-cu12" \
      "nvidia-cufft-cu12" \
      "nvidia-cudnn-cu12"
  else
    "${PIP[@]}" install "onnxruntime>=1.20.0"
  fi
)

CUDA_LIBRARY_PATH="$("$PYTHON" - <<'PY'
import os
import sysconfig

site_packages = sysconfig.get_paths().get("purelib", "")
root = os.path.join(site_packages, "nvidia")
libraries = []
if os.path.isdir(root):
    for dirpath, _dirnames, _filenames in os.walk(root):
        if os.path.basename(dirpath) == "lib":
            libraries.append(dirpath)
print(":".join(sorted(libraries)))
PY
)"

sudo install -m 0644 "${ROOT}/scripts/server/qq-bot-tts.service" "/etc/systemd/system/${SERVICE_NAME}"
sudo sed -i \
  -e "s#QQ_BOT_ROOT_PLACEHOLDER#${ROOT}#g" \
  -e "s#QQ_BOT_TTS_PYTHON_PLACEHOLDER#${PYTHON}#g" \
  -e "s#QQ_BOT_TTS_REPO_DIR_PLACEHOLDER#${REPO_DIR}#g" \
  -e "s#QQ_BOT_TTS_MODEL_DIR_PLACEHOLDER#${MODEL_DIR}#g" \
  -e "s#QQ_BOT_TTS_OUTPUT_DIR_PLACEHOLDER#${OUTPUT_DIR}#g" \
  -e "s#QQ_BOT_TTS_EXECUTION_PROVIDER_PLACEHOLDER#${EXECUTION_PROVIDER}#g" \
  -e "s#QQ_BOT_TTS_LD_LIBRARY_PATH_PLACEHOLDER#${CUDA_LIBRARY_PATH}#g" \
  "/etc/systemd/system/${SERVICE_NAME}"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Installed ${SERVICE_NAME}"
echo "Start with: sudo systemctl start ${SERVICE_NAME}"
echo "Health: curl http://127.0.0.1:18100/health"
