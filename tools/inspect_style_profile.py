from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.runtime_common import print_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a history-derived conversation character for safety.")
    parser.add_argument("profile")
    args = parser.parse_args()

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    source_user_id = str(profile.get("sourceUserId") or "")
    visible_profile = {
        key: value
        for key, value in profile.items()
        if key not in {"sourceUserId", "avoidRules"}
    }
    rendered = json.dumps(_all_strings(visible_profile), ensure_ascii=False)
    if source_user_id:
        rendered = rendered.replace(source_user_id, "")
    data = {
        "sourceUserId": source_user_id,
        "updatedAt": profile.get("updatedAt"),
        "metrics": profile.get("metrics"),
        "characterSummary": profile.get("characterSummary"),
        "styleSummary": profile.get("styleSummary"),
        "lexicon": profile.get("lexicon", []),
        "fewShotExamples": profile.get("fewShotExamples", []),
        "hasLongNumber": bool(re.search(r"\d{7,}", rendered)),
        "hasPhone": bool(re.search(r"1[3-9]\d{9}", rendered)),
        "hasQQNumberLike": bool(re.search(r"(?<!\d)[1-9]\d{4,10}(?!\d)", rendered)),
        "hasUrl": bool(re.search(r"https?://|www\.", rendered, re.IGNORECASE)),
        "hasAddressKeyword": any(
            keyword in rendered for keyword in ("住址", "地址", "小区", "门牌", "身份证")
        ),
        "overlongItems": [
            value
            for value in profile.get("lexicon", []) + profile.get("fewShotExamples", [])
            if isinstance(value, str) and len(value) > 40
        ],
    }
    print_json(data)
    return 0


def _all_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_all_strings(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_all_strings(item))
        return result
    return []


if __name__ == "__main__":
    raise SystemExit(main())
