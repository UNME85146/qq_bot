from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


def backup_database(
    db_path: str | Path,
    backup_dir: str | Path,
    *,
    timestamp: str | None = None,
) -> Path:
    source_path = Path(db_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"database not found: {source_path}")

    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = target_dir / f"bot-{suffix}.db"
    temporary = target.with_suffix(".db.tmp")
    if target.exists() or temporary.exists():
        raise FileExistsError(f"backup target already exists: {target}")

    source_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                check = [row[0] for row in destination.execute("PRAGMA quick_check")]
                if check != ["ok"]:
                    raise RuntimeError(f"backup integrity check failed: {check!r}")
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the bot SQLite database.")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--backup-dir", default="data/backups")
    args = parser.parse_args()

    target = backup_database(args.db, args.backup_dir)
    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
