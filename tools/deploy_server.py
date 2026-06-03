from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the current Git HEAD to the QQ bot server.")
    parser.add_argument("--host", default="your-server-ip")
    parser.add_argument("--user", default="your-server-user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--root", default="/home/your-server-user/qq_bot")
    parser.add_argument("--service", default="qq-bot.service")
    parser.add_argument("--napcat-container", default="napcat")
    parser.add_argument("--password-env", default="", help="Optional env var containing the SSH password.")
    parser.add_argument("--sudo-password-env", default="", help="Optional env var containing the sudo password.")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-napcat-restart", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-remote-archive", action="store_true")
    parser.add_argument("--archive-dir", default=".deploy")
    args = parser.parse_args()

    if not args.allow_dirty:
        _ensure_clean_worktree()

    commit = _git("rev-parse", "--short", "HEAD")
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
        restart_napcat=not args.skip_napcat_restart,
        keep_archive=args.keep_remote_archive,
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
                    f"restart_napcat={not args.skip_napcat_restart}",
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

if [ ! -f "$ARCHIVE" ]; then
  echo "archive not found: $ARCHIVE" >&2
  exit 2
fi
if [ ! -d "$ROOT" ]; then
  echo "runtime root not found: $ROOT" >&2
  exit 2
fi

BACKUP="${{ROOT}}_runtime_backup_$(date +%Y%m%d-%H%M%S)"
RELEASE="/tmp/qq_bot_release_${{COMMIT}}_$$"
echo "backup=$BACKUP"
mkdir -p "$BACKUP/config" "$BACKUP/data" "$RELEASE"
cp -a "$ROOT/.env" "$BACKUP/.env" 2>/dev/null || true
cp -a "$ROOT/config/config.json" "$BACKUP/config/config.json" 2>/dev/null || true
cp -a "$ROOT/config/persona_profile.local.json" "$BACKUP/config/persona_profile.local.json" 2>/dev/null || true
cp -a "$ROOT/data/bot.db" "$BACKUP/data/bot.db" 2>/dev/null || true

tar -xf "$ARCHIVE" -C "$RELEASE"
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
cp -a "$BACKUP/.env" "$ROOT/.env" 2>/dev/null || true
cp -a "$BACKUP/config/config.json" "$ROOT/config/config.json" 2>/dev/null || true
cp -a "$BACKUP/config/persona_profile.local.json" "$ROOT/config/persona_profile.local.json" 2>/dev/null || true
cp -a "$BACKUP/data/bot.db" "$ROOT/data/bot.db" 2>/dev/null || true

cd "$ROOT"
if [ ! -x ".venv/bin/python" ]; then
  echo ".venv/bin/python not found; create the server venv before deploying" >&2
  exit 2
fi
. .venv/bin/activate
python -m pip install -e ".[dev]"
python - <<'PY'
import asyncio
from app.config import load_config
from app.storage.database import init_database

config = load_config("config/config.json")
asyncio.run(init_database(config.storage.database_path))
print("database initialized:", config.storage.database_path)
PY

if [ "$RUN_TESTS" = "1" ]; then
  python -m pytest -q
  python -m compileall app bot.py tests tools
fi

sudo_run systemctl restart "$SERVICE"
if [ "$RESTART_NAPCAT" = "1" ]; then
  sudo_run docker restart "$NAPCAT_CONTAINER" >/dev/null
fi
sleep 10
python tools/inspect_runtime_status.py --limit 5

rm -rf "$RELEASE"
if [ "$KEEP_ARCHIVE" != "1" ]; then
  rm -f "$ARCHIVE"
fi
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
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        input=input_text,
        check=False,
    )
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
