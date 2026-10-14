"""Small local control plane for the Bot child inside the combined container."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any


class BotProcess:
    def __init__(self, command: list[str], cwd: Path, *, env: dict[str, str] | None = None,
                 control_token: str = "") -> None:
        self.command = list(command)
        self.cwd = Path(cwd)
        self.env = dict(os.environ if env is None else env)
        self.control_token = control_token
        self.process: subprocess.Popen[str] | None = None
        self.generation = 0
        self._stop = False
        self._lock = threading.RLock()

    def step(self) -> None:
        with self._lock:
            if self._stop:
                return
            if self.process is not None and self.process.poll() is None:
                return
            self._start_locked()

    def _start_locked(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            text=True,
            start_new_session=True,
        )
        self.generation += 1

    def request_restart(self) -> bool:
        with self._lock:
            if self._stop or self.process is None or self.process.poll() is not None:
                return False
            try:
                if hasattr(os, "killpg"):
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            except (ProcessLookupError, PermissionError):
                return False
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self.process
            running = process is not None and process.poll() is None
            return {
                "running": running,
                "pid": process.pid if running and process is not None else None,
                "generation": self.generation,
            }

    def close(self) -> None:
        with self._lock:
            self._stop = True
            process = self.process
            if process is None or process.poll() is not None:
                return
            try:
                if hasattr(os, "killpg"):
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass


def control_action(runner: BotProcess, method: str, path: str) -> tuple[int, dict[str, Any]]:
    if method == "GET" and path == "/status":
        return 200, runner.status()
    if method == "POST" and path == "/restart":
        return (202, {"status": "restarting"}) if runner.request_restart() else (503, {"error": "bot unavailable"})
    return 404, {"error": "not found"}


class _Handler(BaseHTTPRequestHandler):
    runner: BotProcess

    def _respond(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = self.runner.control_token
        return not expected or self.headers.get("X-QQ-Bot-Control-Token", "") == expected

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._respond(403, {"error": "forbidden"})
            return
        code, payload = control_action(self.runner, "GET", self.path)
        self._respond(code, payload)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._respond(403, {"error": "forbidden"})
            return
        code, payload = control_action(self.runner, "POST", self.path)
        self._respond(code, payload)

    def log_message(self, *_args: Any) -> None:
        return


def serve(runner: BotProcess, host: str = "127.0.0.1", port: int = 3015) -> None:
    handler = type("BotControlHandler", (_Handler,), {"runner": runner})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    stop = threading.Event()

    def shutdown(*_args: Any) -> None:
        runner.close()
        if not stop.is_set():
            stop.set()
            server.server_close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.timeout = 0.5
    while not stop.is_set():
        runner.step()
        server.handle_request()


def main() -> None:
    root = Path(os.environ.get("QQ_BOT_ROOT", "/app/qq_bot"))
    command = [os.environ.get("QQ_BOT_PYTHON", "/opt/qqbot-venv/bin/python"), "-B", str(root / "bot.py")]
    env = dict(os.environ)
    env["QQ_BOT_CONFIG_PATH"] = os.environ.get("QQ_BOT_CONFIG_PATH", str(root / "config/config.json"))
    env["QQ_BOT_ENV_PATH"] = os.environ.get("QQ_BOT_ENV_PATH", str(root / ".env"))
    token_path = os.environ.get("QQ_BOT_CONTROL_TOKEN_FILE", "")
    control_token = Path(token_path).read_text(encoding="ascii").strip() if token_path else ""
    if token_path and not control_token:
        raise SystemExit("QQ_BOT_CONTROL_TOKEN_FILE is empty")
    runner = BotProcess(command, root, env=env, control_token=control_token)
    serve(runner, os.environ.get("QQ_BOT_CONTROL_HOST", "127.0.0.1"), int(os.environ.get("QQ_BOT_CONTROL_PORT", "3015")))


if __name__ == "__main__":
    main()
