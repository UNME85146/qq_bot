from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite


DEFAULT_BUSY_TIMEOUT_MS = 5_000
MigrationAction = Callable[[aiosqlite.Connection], Awaitable[None]]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...] = ()
    action: MigrationAction | None = None


@asynccontextmanager
async def connect_database(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> AsyncIterator[aiosqlite.Connection]:
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")
    db = await aiosqlite.connect(
        Path(database_path),
        timeout=busy_timeout_ms / 1000,
    )
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        yield db
    finally:
        await db.close()


async def init_database(
    database_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup_dir is not None and await _database_needs_migration(path):
        await _backup_before_migration(path, Path(backup_dir))

    async with connect_database(path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              trace_id TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              user_name TEXT,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              message_id TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_states (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              mood INTEGER NOT NULL DEFAULT 60,
              energy INTEGER NOT NULL DEFAULT 70,
              trust INTEGER NOT NULL DEFAULT 30,
              relationship_stage TEXT NOT NULL DEFAULT 'stranger',
              last_interaction_at TEXT,
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              UNIQUE(scope_type, scope_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_profiles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL UNIQUE,
              display_name TEXT,
              preferred_name TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '',
              likes TEXT NOT NULL DEFAULT '',
              dislikes TEXT NOT NULL DEFAULT '',
              important_events TEXT NOT NULL DEFAULT '',
              safety_notes TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_contexts (
              group_id TEXT PRIMARY KEY,
              summary TEXT NOT NULL DEFAULT '',
              topic_keywords TEXT NOT NULL DEFAULT '',
              last_message_id TEXT,
              message_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_mute_states (
              group_id TEXT PRIMARY KEY,
              muted INTEGER NOT NULL DEFAULT 0,
              updated_by TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_sent_messages (
              message_id TEXT PRIMARY KEY,
              trace_id TEXT NOT NULL,
              group_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              original_message_id TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_pending_questions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              group_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              user_name TEXT,
              message_id TEXT NOT NULL,
              question_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              answered_at TEXT,
              UNIQUE(group_id, message_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_type TEXT NOT NULL DEFAULT 'reminder',
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              user_name TEXT,
              message TEXT NOT NULL,
              due_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              completed_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
            ON scheduled_tasks(status, due_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sticker_assets (
              asset_id TEXT PRIMARY KEY,
              source_scope_type TEXT NOT NULL,
              source_scope_id TEXT NOT NULL,
              source_user_id TEXT NOT NULL,
              source_message_id TEXT,
              file_path TEXT NOT NULL,
              url_hash TEXT NOT NULL,
              media_type TEXT NOT NULL,
              source_file TEXT,
              tags TEXT NOT NULL DEFAULT '',
              risk_level TEXT NOT NULL DEFAULT 'safe',
              usage_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              last_used_at TEXT,
              UNIQUE(url_hash)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sticker_asset_analysis (
              asset_id TEXT PRIMARY KEY,
              intent_summary TEXT NOT NULL DEFAULT '',
              emotion_tags TEXT NOT NULL DEFAULT '',
              scene_tags TEXT NOT NULL DEFAULT '',
              text_tags TEXT NOT NULL DEFAULT '',
              reply_usage_hint TEXT NOT NULL DEFAULT '',
              safety_category TEXT NOT NULL DEFAULT 'unknown',
              analysis_status TEXT NOT NULL DEFAULT 'pending',
              analyzed_at TEXT,
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY(asset_id) REFERENCES sticker_assets(asset_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sticker_asset_analysis_status
            ON sticker_asset_analysis(analysis_status, safety_category)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sticker_assets_usage
            ON sticker_assets(risk_level, usage_count, last_used_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_semantic_terms (
              group_id TEXT NOT NULL,
              term TEXT NOT NULL,
              description TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'rule',
              confidence REAL NOT NULL DEFAULT 0.5,
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY(group_id, term)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_semantic_terms_group
            ON group_semantic_terms(group_id, updated_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_message_index (
              group_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              user_name TEXT,
              text TEXT NOT NULL DEFAULT '',
              media_type TEXT NOT NULL DEFAULT '',
              sticker_asset_id TEXT,
              is_bot INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY(group_id, message_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_message_index_recent
            ON group_message_index(group_id, created_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_repeat_states (
              group_id TEXT NOT NULL,
              source_message_id TEXT NOT NULL,
              repeat_kind TEXT NOT NULL,
              repeated_by TEXT NOT NULL,
              trigger_user_id TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY(group_id, source_message_id, repeat_kind)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_audits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              trace_id TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              action TEXT NOT NULL,
              reason TEXT NOT NULL,
              model_called INTEGER NOT NULL DEFAULT 0,
              safety_blocked INTEGER NOT NULL DEFAULT 0,
              elapsed_ms INTEGER,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS system_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              level TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT,
              trace_id TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.commit()
        await run_migrations(db)


async def run_migrations(
    db: aiosqlite.Connection,
    migrations: Sequence[Migration] | None = None,
) -> None:
    selected = tuple(MIGRATIONS if migrations is None else migrations)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.commit()
    rows = await (
        await db.execute("SELECT version FROM schema_migrations")
    ).fetchall()
    applied = {int(row[0]) for row in rows}

    for migration in sorted(selected, key=lambda item: item.version):
        if migration.version in applied:
            continue
        await db.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                await db.execute(statement)
            if migration.action is not None:
                await migration.action(db)
            await db.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def _database_needs_migration(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False

    def current_version() -> int:
        with sqlite3.connect(path) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if table is None:
                return 0
            row = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            return int(row[0]) if row else 0

    return await asyncio.to_thread(current_version) < LATEST_SCHEMA_VERSION


async def _backup_before_migration(path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        f"bot-before-v{LATEST_SCHEMA_VERSION}-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}.db"
    )

    def backup() -> None:
        with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)

    await asyncio.to_thread(backup)
    return target


async def _add_column_if_missing(
    db: aiosqlite.Connection,
    *,
    table: str,
    column: str,
    definition: str,
) -> None:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    if column not in {str(row[1]) for row in rows}:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _execute_sql_script(db: aiosqlite.Connection, script: str) -> None:
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            await db.execute(candidate)
            pending.clear()
    if "\n".join(pending).strip():
        raise ValueError("migration SQL contains an incomplete statement")


async def _migration_002_sessions_and_features(db: aiosqlite.Connection) -> None:
    await _add_column_if_missing(
        db,
        table="conversations",
        column="session_id",
        definition="TEXT",
    )
    await _add_column_if_missing(
        db,
        table="bot_sent_messages",
        column="session_id",
        definition="TEXT",
    )
    await _add_column_if_missing(
        db,
        table="group_mute_states",
        column="mode",
        definition="TEXT NOT NULL DEFAULT 'normal'",
    )
    await db.execute(
        """
        UPDATE group_mute_states
        SET mode = CASE WHEN muted = 1 THEN 'chat_muted' ELSE 'normal' END
        """
    )
    await _execute_sql_script(
        db,
        """
        CREATE TABLE IF NOT EXISTS conversation_sessions (
          session_id TEXT PRIMARY KEY,
          scope_type TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          initiator_user_id TEXT NOT NULL,
          root_message_id TEXT,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active', 'dormant', 'suspended', 'closed')),
          last_activity_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          close_reason TEXT,
          closed_at TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_sessions_scope
        ON conversation_sessions(scope_type, scope_id, status, last_activity_at);

        CREATE TABLE IF NOT EXISTS session_memories (
          session_id TEXT PRIMARY KEY,
          summary TEXT NOT NULL DEFAULT '',
          keywords TEXT NOT NULL DEFAULT '',
          sample_count INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'temporary',
          updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          FOREIGN KEY(session_id) REFERENCES conversation_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS group_member_profiles (
          group_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          display_name TEXT,
          summary TEXT NOT NULL DEFAULT '',
          metrics_json TEXT NOT NULL DEFAULT '{}',
          message_count INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY(group_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS group_news_subscriptions (
          group_id TEXT PRIMARY KEY,
          enabled INTEGER NOT NULL DEFAULT 0,
          send_time TEXT NOT NULL DEFAULT '08:00',
          timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
          categories TEXT NOT NULL DEFAULT 'politics,business,technology,finance',
          last_sent_date TEXT,
          updated_by TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stock_watch_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL,
          scope_type TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          market TEXT NOT NULL,
          cost_price REAL,
          quantity REAL,
          alert_threshold_percent REAL NOT NULL DEFAULT 3.0,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(user_id, scope_type, scope_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS stock_alert_states (
          watch_item_id INTEGER NOT NULL,
          trading_date TEXT NOT NULL,
          direction TEXT NOT NULL,
          last_price REAL,
          alerted_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY(watch_item_id, trading_date, direction),
          FOREIGN KEY(watch_item_id) REFERENCES stock_watch_items(id)
        );
        """
    )


async def _migration_003_news_delivery_checkpoints(
    db: aiosqlite.Connection,
) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS group_news_delivery_checkpoints (
          group_id TEXT NOT NULL,
          delivery_date TEXT NOT NULL,
          messages_json TEXT NOT NULL,
          next_message_index INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY(group_id, delivery_date),
          FOREIGN KEY(group_id) REFERENCES group_news_subscriptions(group_id)
            ON DELETE CASCADE,
          CHECK(next_message_index >= 0)
        )
        """
    )


MIGRATIONS = (
    Migration(version=1, name="baseline schema"),
    Migration(
        version=2,
        name="conversation sessions and feature state",
        action=_migration_002_sessions_and_features,
    ),
    Migration(
        version=3,
        name="scheduled news delivery checkpoints",
        action=_migration_003_news_delivery_checkpoints,
    ),
)
LATEST_SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)
