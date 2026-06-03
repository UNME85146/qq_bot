from __future__ import annotations

from pathlib import Path

import aiosqlite


async def init_database(database_path: str | Path) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
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
