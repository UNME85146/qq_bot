from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.build_style_profile import (
    _extract_text,
    _is_low_sensitive_style_text,
    _is_pure_attachment,
    _is_system_or_recalled,
    _normalize_text,
    _sender_uin,
)
from tools.runtime_common import print_json
from app.safety.safety_service import SafetyService


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample safe style candidates from QQ exports.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--source-user-id", required=True)
    parser.add_argument("--safe-only", action="store_true", default=True)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    safety = SafetyService(source_user_id=args.source_user_id)
    lengths: list[int] = []
    phrases: Counter[str] = Counter()
    stats = Counter()
    for path in sorted(Path(args.input_dir).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            stats["total"] += 1
            if str(_sender_uin(record)) != args.source_user_id:
                continue
            stats["target"] += 1
            if _is_system_or_recalled(record):
                stats["system_or_recalled"] += 1
                continue
            text = _normalize_text(_extract_text(record))
            if not text:
                continue
            if _is_pure_attachment(text):
                stats["attachment"] += 1
                continue
            if args.safe_only and not _is_low_sensitive_style_text(text, safety):
                stats["sensitive"] += 1
                continue
            lengths.append(len(text))
            if len(text) <= 18:
                phrases[text] += 1
    print_json(
        {
            "stats": dict(stats),
            "length": {
                "count": len(lengths),
                "avg": round(sum(lengths) / len(lengths), 2) if lengths else 0,
                "max": max(lengths) if lengths else 0,
            },
            "shortPhrases": phrases.most_common(args.limit),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
