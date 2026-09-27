"""Initialize the dashboard password interactively without printing it."""

from __future__ import annotations

import argparse
from getpass import getpass
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ops.web_dashboard import hash_password


def create_password_hash_file(path: Path, password: str) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("credential parent must be an existing directory")
    encoded = hash_password(password)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
            path.unlink()
            raise ValueError("credential permissions must be 0600")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    password = getpass("Dashboard password (16+ characters): ")
    if password != getpass("Confirm dashboard password: "):
        raise SystemExit("password confirmation mismatch")
    create_password_hash_file(args.path, password)
    print("Private dashboard credential created. Do not share the password or hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
