from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy the current Git HEAD to the QQ bot server.")
    parser.add_argument("--host", default="your-server-ip")
    parser.add_argument("--user", default="your-server-user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--root", default="/opt/qq_bot")
    parser.add_argument("--service", default="qq-bot.service")
    parser.add_argument("--napcat-container", default="napcat")
    parser.add_argument(
        "--video-cache-host",
        default="/opt/napcat/cache/qq-bot-media",
    )
    parser.add_argument(
        "--video-cache-container",
        default="/app/napcat/cache/qq-bot-media",
    )
    parser.add_argument("--password-env", default="", help="Optional env var containing the SSH password.")
    parser.add_argument("--sudo-password-env", default="", help="Optional env var containing the sudo password.")
    parser.add_argument("--skip-tests", action="store_true")
    napcat_restart = parser.add_mutually_exclusive_group()
    napcat_restart.add_argument(
        "--restart-napcat",
        action="store_true",
        dest="restart_napcat",
        help="Restart NapCat explicitly; disabled by default to preserve login state.",
    )
    napcat_restart.add_argument(
        "--skip-napcat-restart",
        action="store_false",
        dest="restart_napcat",
        help="Compatibility flag; NapCat restart is already disabled by default.",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-remote-archive", action="store_true")
    parser.add_argument("--archive-dir", default=".deploy")
    parser.set_defaults(restart_napcat=False)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.allow_dirty:
        _ensure_clean_worktree()

    commit = _git("rev-parse", "HEAD")
    archive_dir = _resolve_archive_dir(args.archive_dir)
    archive = archive_dir / f"qq_bot-{commit}.tar"
    remote_archive = f"/tmp/qq_bot-{commit}.tar"
    remote_script = build_remote_script(
        root=args.root,
        archive=remote_archive,
        commit=commit,
        service=args.service,
        napcat_container=args.napcat_container,
        sudo_password=_env_value(args.sudo_password_env),
        run_tests=not args.skip_tests,
        restart_napcat=args.restart_napcat,
        keep_archive=args.keep_remote_archive,
        video_cache_host=args.video_cache_host,
        video_cache_container=args.video_cache_container,
    )

    if args.dry_run:
        print(
            "\n".join(
                (
                    f"commit={commit}",
                    f"archive={archive}",
                    f"remote={args.user}@{args.host}:{remote_archive}",
                    f"root={args.root}",
                    f"run_tests={not args.skip_tests}",
                    f"restart_napcat={args.restart_napcat}",
                    f"video_cache_host={args.video_cache_host}",
                    f"video_cache_container={args.video_cache_container}",
                )
            )
        )
        return 0

    _git("archive", "--format=tar", "-o", str(archive), "HEAD", capture=False)
    password = _env_value(args.password_env)
    try:
        if password:
            _deploy_with_paramiko(
                archive=archive,
                remote_archive=remote_archive,
                remote_script=remote_script,
                host=args.host,
                user=args.user,
                port=args.port,
                password=password,
            )
        else:
            _deploy_with_openssh(
                archive=archive,
                remote_archive=remote_archive,
                remote_script=remote_script,
                host=args.host,
                user=args.user,
                port=args.port,
            )
    finally:
        archive.unlink(missing_ok=True)
    return 0


def build_remote_script(
    *,
    root: str,
    archive: str,
    commit: str,
    service: str,
    napcat_container: str,
    sudo_password: str,
    run_tests: bool,
    restart_napcat: bool,
    keep_archive: bool,
    video_cache_host: str = "/opt/napcat/cache/qq-bot-media",
    video_cache_container: str = "/app/napcat/cache/qq-bot-media",
) -> str:
    sudo_assignment = ""
    if sudo_password:
        sudo_assignment = (
            "SUDO_PASSWORD=$(cat <<'__QQ_BOT_SUDO_PASSWORD__'\n"
            f"{sudo_password}\n"
            "__QQ_BOT_SUDO_PASSWORD__\n"
            ")\n"
        )
    return f"""#!/usr/bin/env bash
set -euo pipefail

ROOT={shlex.quote(root)}
ARCHIVE={shlex.quote(archive)}
COMMIT={shlex.quote(commit)}
SERVICE={shlex.quote(service)}
NAPCAT_CONTAINER={shlex.quote(napcat_container)}
VIDEO_CACHE_HOST={shlex.quote(video_cache_host)}
VIDEO_CACHE_CONTAINER={shlex.quote(video_cache_container)}
RUN_TESTS={_bash_bool(run_tests)}
RESTART_NAPCAT={_bash_bool(restart_napcat)}
KEEP_ARCHIVE={_bash_bool(keep_archive)}
{sudo_assignment}

sudo_run() {{
  if [ -n "${{SUDO_PASSWORD:-}}" ]; then
    printf '%s\\n' "$SUDO_PASSWORD" | sudo -S "$@"
  else
    sudo "$@"
  fi
}}

backup_if_exists() {{
  local source=$1
  local target=$2
  if [ -e "$source" ]; then
    cp -a "$source" "$target"
  fi
}}

SERVICE_WAS_ACTIVE=0
MUTATION_STARTED=0
DB_EXISTED=0
DB_BACKUP=""
CACHE_CREATED=0
CACHE_MODE_BEFORE=""
CACHE_MARKER=""
BACKUP=""
RELEASE=""

cleanup_transient() {{
  if [ -n "$RELEASE" ]; then
    rm -rf "$RELEASE"
  fi
  if [ "$KEEP_ARCHIVE" != "1" ]; then
    rm -f "$ARCHIVE"
  fi
}}

restore_code() {{
  local restore_failed=0
  ROOT="$ROOT" python3 - <<'PY'
from __future__ import annotations

import os
import shutil
from pathlib import Path

root = Path(os.environ["ROOT"])
preserve_top = {{
    ".env",
    ".git",
    ".venv",
    "data",
    "logs",
    "run",
    "runtime_artifacts",
    "__pycache__",
    ".pytest_cache",
    "qq_realistic_role_bot.egg-info",
}}
preserve_config = {{"config.json", "persona_profile.local.json"}}

for item in root.iterdir():
    if item.name in preserve_top:
        continue
    if item.name == "config" and item.is_dir():
        for config_item in item.iterdir():
            if config_item.name in preserve_config or config_item.name.endswith(".local.json"):
                continue
            if config_item.is_dir():
                shutil.rmtree(config_item)
            else:
                config_item.unlink()
        continue
    if item.is_dir():
        shutil.rmtree(item)
    else:
        item.unlink()
PY
  if [ "$?" != "0" ]; then
    restore_failed=1
  fi
  tar -xf "$BACKUP/code.tar" -C "$ROOT" || restore_failed=1
  if [ -e "$BACKUP/.env" ]; then
    cp -a "$BACKUP/.env" "$ROOT/.env" || restore_failed=1
  fi
  if [ -e "$BACKUP/config/config.json" ]; then
    cp -a "$BACKUP/config/config.json" "$ROOT/config/config.json" || restore_failed=1
  fi
  if [ -e "$BACKUP/config/persona_profile.local.json" ]; then
    cp -a "$BACKUP/config/persona_profile.local.json" \
      "$ROOT/config/persona_profile.local.json" || restore_failed=1
  fi
  if [ -f "$BACKUP/venv.tar" ]; then
    rm -rf "$ROOT/.venv" || restore_failed=1
    tar -xf "$BACKUP/venv.tar" -C "$ROOT" || restore_failed=1
  fi
  return "$restore_failed"
}}

rollback_runtime() {{
  local rollback_failed=0
  echo "deployment failed; restoring runtime from $BACKUP" >&2
  sudo_run systemctl stop "$SERVICE" >/dev/null 2>&1 || true
  restore_code || rollback_failed=1
  if [ "$DB_EXISTED" = "1" ] && [ -n "$DB_BACKUP" ]; then
    rm -f "$DATABASE_PATH-wal" "$DATABASE_PATH-shm"
    cp -a "$DB_BACKUP" "$DATABASE_PATH" || rollback_failed=1
    if ! DATABASE_PATH="$DATABASE_PATH" python3 - <<'PY'
import os
import sqlite3

connection = sqlite3.connect(f"file:{{os.environ['DATABASE_PATH']}}?mode=ro", uri=True)
try:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit(1)
finally:
    connection.close()
PY
    then
      rollback_failed=1
    fi
  elif [ "$DB_EXISTED" = "0" ] && [ -n "${{DATABASE_PATH:-}}" ]; then
    rm -f "$DATABASE_PATH" "$DATABASE_PATH-wal" "$DATABASE_PATH-shm"
  fi
  if [ "$CACHE_CREATED" = "1" ]; then
    if [ -n "$CACHE_MARKER" ] && [ -f "$CACHE_MARKER" ] \
      && [ ! -L "$VIDEO_CACHE_HOST" ]; then
      rm -rf --one-file-system "$VIDEO_CACHE_HOST" || rollback_failed=1
    elif ! rmdir "$VIDEO_CACHE_HOST" 2>/dev/null; then
      rollback_failed=1
    fi
  elif [ -n "$CACHE_MODE_BEFORE" ]; then
    chmod "$CACHE_MODE_BEFORE" "$VIDEO_CACHE_HOST" || rollback_failed=1
  fi
  if [ "$SERVICE_WAS_ACTIVE" = "1" ]; then
    sudo_run systemctl start "$SERVICE" || rollback_failed=1
    sudo_run systemctl is-active --quiet "$SERVICE" || rollback_failed=1
  fi
  if [ "$rollback_failed" != "0" ]; then
    echo "automatic rollback was incomplete; preserve $BACKUP for manual recovery" >&2
  fi
}}

deployment_exit() {{
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" != "0" ]; then
    if [ "$MUTATION_STARTED" = "1" ]; then
      rollback_runtime
    elif [ "$SERVICE_WAS_ACTIVE" = "1" ]; then
      sudo_run systemctl start "$SERVICE" >/dev/null 2>&1 || true
    fi
  fi
  cleanup_transient
  exit "$status"
}}

if [ ! -f "$ARCHIVE" ]; then
  echo "archive not found: $ARCHIVE" >&2
  exit 2
fi
if [ ! -d "$ROOT" ]; then
  echo "runtime root not found: $ROOT" >&2
  exit 2
fi
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo ".venv/bin/python not found; create the server venv before deploying" >&2
  exit 2
fi
if ! "/usr/bin/ffmpeg" -version >/dev/null 2>&1; then
  echo "/usr/bin/ffmpeg is required for video downloads" >&2
  exit 2
fi

BACKUP="${{ROOT}}_runtime_backup_$(date +%Y%m%d-%H%M%S)"
RELEASE="/tmp/qq_bot_release_${{COMMIT}}_$$"
echo "backup=$BACKUP"
mkdir -p "$RELEASE"
trap deployment_exit EXIT
sudo_run -v
if sudo_run systemctl is-active --quiet "$SERVICE"; then
  SERVICE_WAS_ACTIVE=1
fi
SERVICE_USER=$(systemctl show "$SERVICE" --property=User --value)
SERVICE_GROUP=$(systemctl show "$SERVICE" --property=Group --value)
if [ -z "$SERVICE_USER" ]; then
  SERVICE_USER=root
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  echo "systemd service user does not exist: $SERVICE_USER" >&2
  exit 2
fi
NAPCAT_WAS_RUNNING=$(sudo_run docker inspect \
  --format '{{{{.State.Running}}}}' "$NAPCAT_CONTAINER")

HOST_CACHE_PARENT=$(dirname "$VIDEO_CACHE_HOST")
CONTAINER_CACHE_PARENT=$(dirname "$VIDEO_CACHE_CONTAINER")
MOUNTS_JSON=$(sudo_run docker inspect \
  --format '{{{{json .Mounts}}}}' "$NAPCAT_CONTAINER")
if ! MOUNTS_JSON="$MOUNTS_JSON" \
  HOST_CACHE_PARENT="$HOST_CACHE_PARENT" \
  CONTAINER_CACHE_PARENT="$CONTAINER_CACHE_PARENT" \
  python3 - <<'PY'
import json
import os

mounts = json.loads(os.environ["MOUNTS_JSON"])
matched = any(
    item.get("Source") == os.environ["HOST_CACHE_PARENT"]
    and item.get("Destination") == os.environ["CONTAINER_CACHE_PARENT"]
    and item.get("RW") is True
    for item in mounts
)
raise SystemExit(0 if matched else 1)
PY
then
  echo "NapCat cache mount does not match video cache paths" >&2
  exit 2
fi

if ! CONFIG_PATH="$ROOT/config/config.json" \
  VIDEO_CACHE_HOST="$VIDEO_CACHE_HOST" \
  VIDEO_CACHE_CONTAINER="$VIDEO_CACHE_CONTAINER" python3 - <<'PY'
import json
import os
from pathlib import Path

with Path(os.environ["CONFIG_PATH"]).open("r", encoding="utf-8") as handle:
    config = json.load(handle)
video = config.get("video", {{}})
matched = (
    video.get("hostCachePath") == os.environ["VIDEO_CACHE_HOST"]
    and video.get("containerCachePath") == os.environ["VIDEO_CACHE_CONTAINER"]
)
raise SystemExit(0 if matched else 1)
PY
then
  echo "private video cache configuration does not match verified paths" >&2
  exit 2
fi

if [ -L "$VIDEO_CACHE_HOST" ]; then
  echo "video cache path must not be a symbolic link: $VIDEO_CACHE_HOST" >&2
  exit 2
fi
if [ -e "$VIDEO_CACHE_HOST" ] && [ ! -d "$VIDEO_CACHE_HOST" ]; then
  echo "video cache path is not a directory: $VIDEO_CACHE_HOST" >&2
  exit 2
fi
if [ -d "$VIDEO_CACHE_HOST" ]; then
  CACHE_MODE_BEFORE=$(stat -c '%a' "$VIDEO_CACHE_HOST")
fi

tar -xf "$ARCHIVE" -C "$RELEASE"
if [ ! -f "$RELEASE/tools/backup_db.py" ] || [ ! -f "$RELEASE/pyproject.toml" ]; then
  echo "release archive is missing deployment files" >&2
  exit 2
fi

SERVICE_FRAGMENT=$(systemctl show "$SERVICE" --property=FragmentPath --value)
SERVICE_DROPINS=$(systemctl show "$SERVICE" --property=DropInPaths --value)
SERVICE_FRAGMENT_HASH=""
SERVICE_DROPIN_HASHES=""
if [ -n "$SERVICE_FRAGMENT" ] && [ -f "$SERVICE_FRAGMENT" ]; then
  SERVICE_FRAGMENT_HASH=$(sudo_run sha256sum "$SERVICE_FRAGMENT" | awk '{{print $1}}')
fi
if [ -n "$SERVICE_DROPINS" ]; then
  read -r -a DROPIN_PATHS <<< "$SERVICE_DROPINS"
  for dropin in "${{DROPIN_PATHS[@]}}"; do
    if [ -f "$dropin" ]; then
      dropin_hash=$(sudo_run sha256sum "$dropin" | awk '{{print $1}}')
      SERVICE_DROPIN_HASHES+="$dropin\t$dropin_hash\n"
    fi
  done
fi
if [ "$SERVICE_WAS_ACTIVE" = "1" ]; then
  sudo_run systemctl stop "$SERVICE"
fi
if sudo_run systemctl is-active --quiet "$SERVICE"; then
  echo "failed to stop $SERVICE before backup" >&2
  exit 2
fi

mkdir "$BACKUP"
mkdir -p "$BACKUP/config" "$BACKUP/data"
backup_if_exists "$ROOT/.env" "$BACKUP/.env"
backup_if_exists "$ROOT/config/config.json" "$BACKUP/config/config.json"
backup_if_exists "$ROOT/config/persona_profile.local.json" \
  "$BACKUP/config/persona_profile.local.json"
tar -cf "$BACKUP/code.tar" \
  --exclude='./.env' \
  --exclude='./.deploy' \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./data' \
  --exclude='./logs' \
  --exclude='./run' \
  --exclude='./runtime_artifacts' \
  --exclude='./config/config.json' \
  --exclude='./config/*.local.json' \
  --exclude='./__pycache__' \
  --exclude='./.pytest_cache' \
  --exclude='./qq_realistic_role_bot.egg-info' \
  -C "$ROOT" .
tar -C "$ROOT" -cf "$BACKUP/venv.tar" .venv

SYSTEMD_PATHS=()
if [ -n "$SERVICE_FRAGMENT" ] && [ -f "$SERVICE_FRAGMENT" ]; then
  SYSTEMD_PATHS+=("$SERVICE_FRAGMENT")
fi
if [ -n "$SERVICE_DROPINS" ]; then
  read -r -a DROPIN_PATHS <<< "$SERVICE_DROPINS"
  for dropin in "${{DROPIN_PATHS[@]}}"; do
    if [ -f "$dropin" ]; then
      SYSTEMD_PATHS+=("$dropin")
    fi
  done
fi
if [ "${{#SYSTEMD_PATHS[@]}}" -gt 0 ]; then
  SYSTEMD_RELATIVE_PATHS=("${{SYSTEMD_PATHS[@]#/}}")
  sudo_run tar -C / -cf "$BACKUP/systemd.tar" "${{SYSTEMD_RELATIVE_PATHS[@]}}"
  sudo_run chown "$(id -u):$(id -g)" "$BACKUP/systemd.tar"
else
  tar -cf "$BACKUP/systemd.tar" --files-from /dev/null
fi
"$ROOT/.venv/bin/python" -m pip freeze > "$BACKUP/pip-freeze.before.txt"

DATABASE_PATH=$(ROOT="$ROOT" CONFIG_PATH="$ROOT/config/config.json" python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["ROOT"]).resolve()
with Path(os.environ["CONFIG_PATH"]).open("r", encoding="utf-8") as handle:
    config = json.load(handle)
configured = Path(config.get("storage", {{}}).get("databasePath", "data/bot.db"))
resolved = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
if not resolved.is_relative_to(root):
    raise SystemExit("databasePath must remain inside the runtime root")
print(resolved)
PY
)
if [ -f "$DATABASE_PATH" ]; then
  DB_EXISTED=1
  DB_BACKUP=$("$ROOT/.venv/bin/python" "$RELEASE/tools/backup_db.py" \
    --db "$DATABASE_PATH" \
    --backup-dir "$BACKUP/data")
fi

BACKUP="$BACKUP" COMMIT="$COMMIT" SERVICE="$SERVICE" \
  SERVICE_WAS_ACTIVE="$SERVICE_WAS_ACTIVE" \
  SERVICE_USER="$SERVICE_USER" SERVICE_GROUP="$SERVICE_GROUP" \
  NAPCAT_WAS_RUNNING="$NAPCAT_WAS_RUNNING" \
  SERVICE_FRAGMENT="$SERVICE_FRAGMENT" SERVICE_FRAGMENT_HASH="$SERVICE_FRAGMENT_HASH" \
  SERVICE_DROPINS="$SERVICE_DROPINS" SERVICE_DROPIN_HASHES="$SERVICE_DROPIN_HASHES" \
  DATABASE_PATH="$DATABASE_PATH" DB_EXISTED="$DB_EXISTED" \
  DB_BACKUP="$DB_BACKUP" VIDEO_CACHE_HOST="$VIDEO_CACHE_HOST" \
  VIDEO_CACHE_CONTAINER="$VIDEO_CACHE_CONTAINER" \
  CACHE_MODE_BEFORE="$CACHE_MODE_BEFORE" python3 - <<'PY'
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

backup = Path(os.environ["BACKUP"])
files = []
for path in sorted(item for item in backup.rglob("*") if item.is_file()):
    files.append({{
        "path": path.relative_to(backup).as_posix(),
        "sizeBytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }})
manifest = {{
    "schemaVersion": 1,
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "commit": os.environ["COMMIT"],
    "service": os.environ["SERVICE"],
    "serviceWasActive": os.environ["SERVICE_WAS_ACTIVE"] == "1",
    "serviceUser": os.environ["SERVICE_USER"],
    "serviceGroup": os.environ["SERVICE_GROUP"] or None,
    "napcatWasRunning": os.environ["NAPCAT_WAS_RUNNING"] == "true",
    "serviceFragment": os.environ["SERVICE_FRAGMENT"],
    "unitHashes": {{
        "fragment": os.environ["SERVICE_FRAGMENT_HASH"] or None,
        "dropIns": {{
            line.split("\\t", 1)[0]: line.split("\\t", 1)[1]
            for line in os.environ["SERVICE_DROPIN_HASHES"].splitlines()
            if "\\t" in line
        }},
    }},
    "serviceDropIns": os.environ["SERVICE_DROPINS"].split(),
    "databasePath": os.environ["DATABASE_PATH"],
    "databaseExisted": os.environ["DB_EXISTED"] == "1",
    "databaseBackup": os.environ["DB_BACKUP"] or None,
    "videoCacheHost": os.environ["VIDEO_CACHE_HOST"],
    "videoCacheContainer": os.environ["VIDEO_CACHE_CONTAINER"],
    "videoCacheModeBefore": os.environ["CACHE_MODE_BEFORE"] or None,
    "files": files,
}}
(backup / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n",
    encoding="utf-8",
)
PY

if [ -L "$VIDEO_CACHE_HOST" ]; then
  echo "video cache path became a symbolic link before mutation" >&2
  exit 2
fi
MUTATION_STARTED=1
if [ ! -d "$VIDEO_CACHE_HOST" ]; then
  mkdir -m 0750 "$VIDEO_CACHE_HOST"
  CACHE_CREATED=1
  CACHE_MARKER="$VIDEO_CACHE_HOST/.qq-bot-deploy-cache-$COMMIT-$$"
  : > "$CACHE_MARKER"
fi
chmod 0750 "$VIDEO_CACHE_HOST"
SERVICE_IDENTITY_ARGS=(-u "$SERVICE_USER")
if [ -n "$SERVICE_GROUP" ]; then
  SERVICE_IDENTITY_ARGS+=(-g "$SERVICE_GROUP")
fi
if ! sudo_run "${{SERVICE_IDENTITY_ARGS[@]}}" test -w "$VIDEO_CACHE_HOST"; then
  echo "qq-bot.service identity cannot write the video cache" >&2
  exit 2
fi
if ! sudo_run docker exec "$NAPCAT_CONTAINER" test -r "$VIDEO_CACHE_CONTAINER"; then
  echo "NapCat cannot read the configured video cache" >&2
  exit 2
fi

ROOT="$ROOT" RELEASE="$RELEASE" python3 - <<'PY'
from __future__ import annotations

import os
import shutil
from pathlib import Path

root = Path(os.environ["ROOT"])
release = Path(os.environ["RELEASE"])
preserve_top = {{
    ".env",
    ".venv",
    "data",
    "logs",
    "run",
    "__pycache__",
    ".pytest_cache",
    "qq_realistic_role_bot.egg-info",
}}
preserve_config = {{
    "config.json",
    "persona_profile.local.json",
}}

for item in release.iterdir():
    if item.name in preserve_top:
        continue
    target = root / item.name
    if item.is_dir():
        if item.name == "config":
            target.mkdir(parents=True, exist_ok=True)
            for config_item in item.iterdir():
                if config_item.name in preserve_config or config_item.name.endswith(".local.json"):
                    continue
                config_target = target / config_item.name
                if config_target.exists():
                    if config_target.is_dir():
                        shutil.rmtree(config_target)
                    else:
                        config_target.unlink()
                if config_item.is_dir():
                    shutil.copytree(config_item, config_target)
                else:
                    shutil.copy2(config_item, config_target)
            continue
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(item, target)
        continue
    if item.name == ".env":
        continue
    shutil.copy2(item, target)
PY

mkdir -p "$ROOT/data" "$ROOT/logs"

cd "$ROOT"
. .venv/bin/activate
python -m pip install --upgrade-strategy only-if-needed -e ".[dev,market,video]"
python - <<'PY'
import asyncio
from app.config import load_config
from app.storage.database import init_database

config = load_config("config/config.json")
asyncio.run(
    init_database(
        config.storage.database_path,
        backup_dir=config.storage.backup_dir,
    )
)
print("database initialized:", config.storage.database_path)
PY

if [ "$RUN_TESTS" = "1" ]; then
  python -m pytest -q
  python -m compileall app bot.py tests tools
fi

sudo_run systemctl start "$SERVICE"
if [ "$RESTART_NAPCAT" = "1" ]; then
  sudo_run docker restart "$NAPCAT_CONTAINER" >/dev/null
fi
READY=0
for wait_seconds in 5 10 15; do
  sleep "$wait_seconds"
  if python tools/inspect_runtime_status.py --limit 5 --summary --require-ready; then
    READY=1
    break
  fi
done
if [ "$READY" != "1" ]; then
  echo "runtime readiness gate failed after deployment" >&2
  exit 1
fi

if [ -n "$CACHE_MARKER" ]; then
  rm -f "$CACHE_MARKER"
fi
echo "deployment successful; rollback material retained at $BACKUP"
"""


def _deploy_with_openssh(
    *,
    archive: Path,
    remote_archive: str,
    remote_script: str,
    host: str,
    user: str,
    port: int,
) -> None:
    target = f"{user}@{host}:{remote_archive}"
    _run(["scp", "-P", str(port), str(archive), target])
    _run(["ssh", "-p", str(port), f"{user}@{host}", "bash -s"], input_text=remote_script)


def _deploy_with_paramiko(
    *,
    archive: Path,
    remote_archive: str,
    remote_script: str,
    host: str,
    user: str,
    port: int,
    password: str,
) -> None:
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError(
            "paramiko is required when --password-env is used; install it or use SSH keys"
        ) from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        port=port,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    try:
        with client.open_sftp() as sftp:
            sftp.put(str(archive), remote_archive)
        stdin, stdout, stderr = client.exec_command("bash -s", timeout=600)
        stdin.write(remote_script)
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if out:
            print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise subprocess.CalledProcessError(code, "remote bash")
    finally:
        client.close()


def _ensure_clean_worktree() -> None:
    status = _git("status", "--porcelain")
    if status.strip():
        raise SystemExit(
            "working tree is dirty; commit/stash changes first or pass --allow-dirty"
        )


def _git(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if capture else ""
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, stderr)
    return result.stdout.strip() if capture else ""


def _run(command: list[str], *, input_text: str | None = None) -> None:
    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    result = subprocess.run(command, cwd=PROJECT_ROOT, input=input_bytes, check=False)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command)


def _env_value(name: str) -> str:
    if not name:
        return ""
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"environment variable is empty or missing: {name}")
    return value


def _bash_bool(value: bool) -> str:
    return "1" if value else "0"


def _resolve_archive_dir(path: str) -> Path:
    archive_dir = Path(path)
    if not archive_dir.is_absolute():
        archive_dir = PROJECT_ROOT / archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    return archive_dir


if __name__ == "__main__":
    raise SystemExit(main())
