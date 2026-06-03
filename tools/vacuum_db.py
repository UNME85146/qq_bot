from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.backup_db import main as backup_main


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up and VACUUM the bot SQLite database.")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--backup-dir", default="data/backups")
    args = parser.parse_args()

    # Reuse backup behavior before touching the database file.
    import sys

    old_argv = sys.argv
    sys.argv = ["backup_db", "--db", args.db, "--backup-dir", args.backup_dir]
    try:
        backup_main()
    finally:
        sys.argv = old_argv

    db_path = Path(args.db)
    with sqlite3.connect(db_path) as conn:
        conn.execute("VACUUM")
    print(f"vacuumed {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
