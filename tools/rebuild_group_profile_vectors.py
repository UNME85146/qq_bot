from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from app.config import load_config
from app.memory.group_member_profile_vector_service import GroupMemberProfileVectorService
from app.model.llm_client import create_model_client
from app.safety.safety_service import SafetyService
from app.storage.database import init_database
from app.storage.repositories import GroupMemberProfileVectorRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild the SQLite-backed group member profile vector store."
    )
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--database", default="data/bot.db")
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-messages-per-user", type=int, default=80)
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="USER_ID=ALIAS",
        help="Operator-confirmed alias mapping; may be repeated.",
    )
    parser.add_argument("--no-model-enrichment", action="store_true")
    return parser


def parse_aliases(values: list[str]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for value in values:
        user_id, separator, alias = value.partition("=")
        user_id, alias = user_id.strip(), alias.strip()
        if not separator or not user_id or not alias or len(alias) > 32:
            raise ValueError("--alias must use USER_ID=ALIAS")
        aliases.setdefault(user_id, []).append(alias)
    return aliases


async def _run(args: argparse.Namespace) -> dict[str, object]:
    config = load_config(args.config)
    await init_database(args.database)
    profile_config = replace(
        config.group_profile,
        enabled=True,
        group_ids=(str(args.group_id),),
        lookback_days=args.days,
        max_messages_per_user=args.max_messages_per_user,
        model_enrichment_enabled=not args.no_model_enrichment,
    )
    service = GroupMemberProfileVectorService(
        repository=GroupMemberProfileVectorRepository(args.database),
        qq_config=config.qq,
        profile_config=profile_config,
        safety_service=SafetyService(
            identity_disclosure=config.persona.style_profile.identity_disclosure,
            source_user_id=config.persona.style_profile.source_user_id,
        ),
        model_client=create_model_client(config.model),
    )
    return await service.refresh_group(
        str(args.group_id),
        force=True,
        aliases=parse_aliases(args.alias),
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.days <= 0 or args.max_messages_per_user <= 0:
        raise SystemExit("--days and --max-messages-per-user must be positive")
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
