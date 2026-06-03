from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the bot SQLite database.")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--backup-dir", default="data/backups")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"bot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(db_path, target)
    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
