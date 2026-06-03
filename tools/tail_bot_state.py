from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.runtime_common import print_json, recent_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Tail recent bot DB state.")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    while True:
        if os.name == "nt":
            os.system("cls")
        data = {
            "conversations": recent_rows(db_path, "conversations", limit=args.limit)
            if db_path.exists()
            else [],
            "replyAudits": recent_rows(db_path, "reply_audits", limit=args.limit)
            if db_path.exists()
            else [],
            "systemEvents": recent_rows(db_path, "system_events", limit=args.limit)
            if db_path.exists()
            else [],
        }
        print_json(data)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
