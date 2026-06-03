from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.runtime_common import sanitize_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Export audit tables.")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--output", required=True)
    parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    args = parser.parse_args()

    rows = _load_rows(Path(args.db))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "jsonl":
        output.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
    else:
        with output.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=sorted(rows[0]) if rows else ["table"])
            writer.writeheader()
            writer.writerows(rows)
    print(str(output))
    return 0


def _load_rows(db_path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for table in ("reply_audits", "system_events"):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY id ASC"):
                item = dict(row)
                item["table"] = table
                if "detail" in item and item["detail"] is not None:
                    item["detail"] = sanitize_text(str(item["detail"]))
                rows.append(item)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
