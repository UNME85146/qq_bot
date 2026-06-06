from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.features.sticker_analysis_service import upsert_local_tag_fallback_analysis
from app.models import StickerAsset
from app.storage.repositories import StickerAssetAnalysisRepository, StickerAssetRepository
from tools.runtime_common import print_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill failed sticker analysis rows with local tag fallback data.",
    )
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--db", default=None)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = args.db or config.storage.database_path
    schema_error = _schema_error(Path(db_path))
    if schema_error is not None:
        print_json(
            {
                "db": db_path,
                "dryRun": args.dry_run,
                "error": schema_error,
                "failedUnknownRows": 0,
                "eligible": 0,
                "updated": 0,
            }
        )
        return 2
    result = asyncio.run(
        _backfill(
            db_path=db_path,
            limit=max(0, args.limit),
            dry_run=args.dry_run,
        )
    )
    print_json(result)
    return 0


def _schema_error(db_path: Path) -> str | None:
    if not db_path.exists():
        return "db_not_found"
    try:
        with sqlite3.connect(db_path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
    except sqlite3.Error:
        return "db_unreadable"
    required = {"sticker_assets", "sticker_asset_analysis"}
    missing = sorted(required - tables)
    if missing:
        return "missing_tables:" + ",".join(missing)
    return None


async def _backfill(*, db_path: str, limit: int, dry_run: bool) -> dict[str, object]:
    analysis_repo = StickerAssetAnalysisRepository(db_path)
    sticker_repo = StickerAssetRepository(db_path)
    failed = await analysis_repo.list_failed_unknown(limit=limit)
    candidates: list[StickerAsset] = []
    skipped_missing_asset = 0
    skipped_unsafe = 0
    skipped_missing_file = 0
    skipped_no_tags = 0
    for analysis in failed:
        asset = await sticker_repo.get_by_asset_id(analysis.asset_id)
        if asset is None:
            skipped_missing_asset += 1
            continue
        if asset.risk_level != "safe":
            skipped_unsafe += 1
            continue
        if not Path(asset.file_path).exists():
            skipped_missing_file += 1
            continue
        if not asset.tags.strip():
            skipped_no_tags += 1
            continue
        candidates.append(asset)

    updated = 0
    if not dry_run:
        for asset in candidates:
            await upsert_local_tag_fallback_analysis(
                analysis_repo,
                asset,
                reason="backfill_failed_unknown",
            )
            updated += 1

    return {
        "db": db_path,
        "dryRun": dry_run,
        "failedUnknownRows": len(failed),
        "eligible": len(candidates),
        "updated": updated,
        "skipped": {
            "missingAsset": skipped_missing_asset,
            "unsafe": skipped_unsafe,
            "missingFile": skipped_missing_file,
            "noTags": skipped_no_tags,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
