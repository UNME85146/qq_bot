from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import (
    GroupContext,
    GroupMessageIndex,
    GroupMuteState,
    GroupPendingQuestion,
    MemoryProfile,
    PersonaState,
    ScheduledTask,
    StickerAsset,
)


class ConversationRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def save_conversation(
        self,
        *,
        trace_id: str,
        scope_type: str,
        scope_id: str,
        user_id: str,
        user_name: str | None,
        role: str,
        content: str,
        message_id: str | None,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO conversations (
                  trace_id,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  role,
                  content,
                  message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    scope_type,
                    scope_id,
                    user_id,
                    user_name,
                    role,
                    content,
                    message_id,
                ),
            )
            await db.commit()

    async def get_recent_conversations(
        self,
        scope_type: str,
        scope_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  trace_id,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  role,
                  content,
                  message_id,
                  created_at
                FROM conversations
                WHERE scope_type = ? AND scope_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (scope_type, scope_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]


class PersonaStateRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get_or_create(self, scope_type: str, scope_id: str) -> PersonaState:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT OR IGNORE INTO persona_states (
                  scope_type,
                  scope_id,
                  mood,
                  energy,
                  trust,
                  relationship_stage
                ) VALUES (?, ?, 60, 70, 30, 'stranger')
                """,
                (scope_type, scope_id),
            )
            await db.commit()
            cursor = await db.execute(
                """
                SELECT
                  scope_type,
                  scope_id,
                  mood,
                  energy,
                  trust,
                  relationship_stage,
                  last_interaction_at
                FROM persona_states
                WHERE scope_type = ? AND scope_id = ?
                """,
                (scope_type, scope_id),
            )
            row = await cursor.fetchone()
        return self._to_persona_state(row)

    async def save(self, state: PersonaState) -> PersonaState:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE persona_states
                SET
                  mood = ?,
                  energy = ?,
                  trust = ?,
                  relationship_stage = ?,
                  last_interaction_at = ?,
                  updated_at = datetime('now')
                WHERE scope_type = ? AND scope_id = ?
                """,
                (
                    state.mood,
                    state.energy,
                    state.trust,
                    state.relationship_stage,
                    state.last_interaction_at,
                    state.scope_type,
                    state.scope_id,
                ),
            )
            await db.commit()
        return state

    def _to_persona_state(self, row: aiosqlite.Row) -> PersonaState:
        return PersonaState(
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            mood=int(row["mood"]),
            energy=int(row["energy"]),
            trust=int(row["trust"]),
            relationship_stage=str(row["relationship_stage"]),
            last_interaction_at=row["last_interaction_at"],
        )


class MemoryProfileRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get_by_user_id(self, user_id: str) -> MemoryProfile | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  user_id,
                  display_name,
                  preferred_name,
                  summary,
                  likes,
                  dislikes,
                  important_events,
                  safety_notes,
                  updated_at
                FROM memory_profiles
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_memory_profile(row)

    async def upsert_summary(
        self,
        *,
        user_id: str,
        display_name: str | None,
        summary: str,
        preferred_name: str = "",
        likes: str = "",
        dislikes: str = "",
        important_events: str = "",
        safety_notes: str = "",
    ) -> MemoryProfile:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO memory_profiles (
                  user_id,
                  display_name,
                  preferred_name,
                  summary,
                  likes,
                  dislikes,
                  important_events,
                  safety_notes,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  preferred_name = excluded.preferred_name,
                  summary = excluded.summary,
                  likes = excluded.likes,
                  dislikes = excluded.dislikes,
                  important_events = excluded.important_events,
                  safety_notes = excluded.safety_notes,
                  updated_at = datetime('now')
                """,
                (
                    user_id,
                    display_name,
                    preferred_name,
                    summary,
                    likes,
                    dislikes,
                    important_events,
                    safety_notes,
                ),
            )
            await db.commit()
        profile = await self.get_by_user_id(user_id)
        if profile is None:
            raise RuntimeError("memory profile upsert did not create a row")
        return profile

    async def clear(self, user_id: str) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("DELETE FROM memory_profiles WHERE user_id = ?", (user_id,))
            await db.commit()

    def _to_memory_profile(self, row: aiosqlite.Row) -> MemoryProfile:
        return MemoryProfile(
            user_id=str(row["user_id"]),
            display_name=row["display_name"],
            preferred_name=str(row["preferred_name"]),
            summary=str(row["summary"]),
            likes=str(row["likes"]),
            dislikes=str(row["dislikes"]),
            important_events=str(row["important_events"]),
            safety_notes=str(row["safety_notes"]),
            updated_at=row["updated_at"],
        )


class GroupContextRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get_by_group_id(self, group_id: str) -> GroupContext | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  summary,
                  topic_keywords,
                  last_message_id,
                  message_count,
                  updated_at
                FROM group_contexts
                WHERE group_id = ?
                """,
                (group_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_group_context(row)

    async def upsert(
        self,
        *,
        group_id: str,
        summary: str,
        topic_keywords: str,
        last_message_id: str | None,
        message_count: int,
    ) -> GroupContext:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_contexts (
                  group_id,
                  summary,
                  topic_keywords,
                  last_message_id,
                  message_count,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                  summary = excluded.summary,
                  topic_keywords = excluded.topic_keywords,
                  last_message_id = excluded.last_message_id,
                  message_count = excluded.message_count,
                  updated_at = datetime('now')
                """,
                (group_id, summary, topic_keywords, last_message_id, message_count),
            )
            await db.commit()
        context = await self.get_by_group_id(group_id)
        if context is None:
            raise RuntimeError("group context upsert did not create a row")
        return context

    def _to_group_context(self, row: aiosqlite.Row) -> GroupContext:
        return GroupContext(
            group_id=str(row["group_id"]),
            summary=str(row["summary"]),
            topic_keywords=str(row["topic_keywords"]),
            last_message_id=row["last_message_id"],
            message_count=int(row["message_count"]),
            updated_at=row["updated_at"],
        )


class GroupMuteStateRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get_by_group_id(self, group_id: str) -> GroupMuteState | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  muted,
                  updated_by,
                  reason,
                  updated_at
                FROM group_mute_states
                WHERE group_id = ?
                """,
                (group_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_group_mute_state(row)

    async def set_muted(
        self,
        *,
        group_id: str,
        muted: bool,
        updated_by: str,
        reason: str,
    ) -> GroupMuteState:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_mute_states (
                  group_id,
                  muted,
                  updated_by,
                  reason,
                  updated_at
                ) VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                  muted = excluded.muted,
                  updated_by = excluded.updated_by,
                  reason = excluded.reason,
                  updated_at = datetime('now')
                """,
                (group_id, int(muted), updated_by, reason),
            )
            await db.commit()
        state = await self.get_by_group_id(group_id)
        if state is None:
            raise RuntimeError("group mute state upsert did not create a row")
        return state

    async def list_muted(self, *, limit: int = 20) -> list[GroupMuteState]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  muted,
                  updated_by,
                  reason,
                  updated_at
                FROM group_mute_states
                WHERE muted = 1
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [self._to_group_mute_state(row) for row in rows]

    async def clear_muted(
        self,
        *,
        updated_by: str,
        reason: str,
        group_id: str | None = None,
    ) -> int:
        where_clause = "WHERE muted = 1"
        params: tuple[object, ...]
        if group_id is None:
            params = (updated_by, reason)
        else:
            where_clause += " AND group_id = ?"
            params = (updated_by, reason, group_id)
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                f"""
                UPDATE group_mute_states
                SET
                  muted = 0,
                  updated_by = ?,
                  reason = ?,
                  updated_at = datetime('now')
                {where_clause}
                """,
                params,
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    def _to_group_mute_state(self, row: aiosqlite.Row) -> GroupMuteState:
        return GroupMuteState(
            group_id=str(row["group_id"]),
            muted=bool(row["muted"]),
            updated_by=str(row["updated_by"]),
            reason=str(row["reason"]),
            updated_at=row["updated_at"],
        )


class BotSentMessageRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def save_group_message(
        self,
        *,
        message_id: str,
        trace_id: str,
        group_id: str,
        user_id: str,
        original_message_id: str | None,
    ) -> None:
        if not message_id:
            return
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO bot_sent_messages (
                  message_id,
                  trace_id,
                  group_id,
                  user_id,
                  original_message_id,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (message_id, trace_id, group_id, user_id, original_message_id),
            )
            await db.commit()

    async def is_bot_sent_message(self, message_id: str | None) -> bool:
        if not message_id:
            return False
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM bot_sent_messages
                WHERE message_id = ?
                LIMIT 1
                """,
                (message_id,),
            )
            row = await cursor.fetchone()
        return row is not None


class GroupPendingQuestionRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def upsert_pending(
        self,
        *,
        group_id: str,
        user_id: str,
        user_name: str,
        message_id: str,
        question_text: str,
    ) -> GroupPendingQuestion:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO group_pending_questions (
                  group_id,
                  user_id,
                  user_name,
                  message_id,
                  question_text,
                  status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (group_id, user_id, user_name, message_id, question_text),
            )
            await db.commit()
        question = await self.get_by_group_message(group_id, message_id)
        if question is None:
            raise RuntimeError("pending question upsert did not create a row")
        return question

    async def get_by_group_message(
        self,
        group_id: str,
        message_id: str,
    ) -> GroupPendingQuestion | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  id,
                  group_id,
                  user_id,
                  user_name,
                  message_id,
                  question_text,
                  status,
                  created_at,
                  answered_at
                FROM group_pending_questions
                WHERE group_id = ? AND message_id = ?
                """,
                (group_id, message_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_pending_question(row)

    async def list_pending(
        self,
        group_id: str,
        *,
        limit: int = 3,
    ) -> list[GroupPendingQuestion]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  id,
                  group_id,
                  user_id,
                  user_name,
                  message_id,
                  question_text,
                  status,
                  created_at,
                  answered_at
                FROM group_pending_questions
                WHERE group_id = ? AND status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (group_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._to_pending_question(row) for row in rows]

    async def mark_answered(self, question_id: int) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE group_pending_questions
                SET status = 'answered',
                    answered_at = datetime('now')
                WHERE id = ?
                """,
                (question_id,),
            )
            await db.commit()

    def _to_pending_question(self, row: aiosqlite.Row) -> GroupPendingQuestion:
        return GroupPendingQuestion(
            id=int(row["id"]),
            group_id=str(row["group_id"]),
            user_id=str(row["user_id"]),
            user_name=str(row["user_name"] or row["user_id"]),
            message_id=str(row["message_id"]),
            question_text=str(row["question_text"]),
            status=str(row["status"]),
            created_at=row["created_at"],
            answered_at=row["answered_at"],
        )


class ScheduledTaskRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def create(
        self,
        *,
        task_type: str,
        scope_type: str,
        scope_id: str,
        user_id: str,
        user_name: str | None,
        message: str,
        due_at: str,
    ) -> ScheduledTask:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO scheduled_tasks (
                  task_type,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  message,
                  due_at,
                  status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (task_type, scope_type, scope_id, user_id, user_name, message, due_at),
            )
            await db.commit()
            task_id = int(cursor.lastrowid)
        task = await self.get_by_id(task_id)
        if task is None:
            raise RuntimeError("scheduled task insert did not create a row")
        return task

    async def get_by_id(self, task_id: int) -> ScheduledTask | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  id,
                  task_type,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  message,
                  due_at,
                  status,
                  created_at,
                  completed_at
                FROM scheduled_tasks
                WHERE id = ?
                """,
                (task_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_scheduled_task(row)

    async def list_pending_due(self, *, now_iso: str, limit: int = 20) -> list[ScheduledTask]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  id,
                  task_type,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  message,
                  due_at,
                  status,
                  created_at,
                  completed_at
                FROM scheduled_tasks
                WHERE status = 'pending' AND due_at <= ?
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                (now_iso, limit),
            )
            rows = await cursor.fetchall()
        return [self._to_scheduled_task(row) for row in rows]

    async def list_for_user(
        self,
        *,
        user_id: str,
        include_all: bool = False,
        limit: int = 10,
    ) -> list[ScheduledTask]:
        where = "status = 'pending'"
        params: list[object] = []
        if not include_all:
            where += " AND user_id = ?"
            params.append(user_id)
        params.append(limit)
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                  id,
                  task_type,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  message,
                  due_at,
                  status,
                  created_at,
                  completed_at
                FROM scheduled_tasks
                WHERE {where}
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
        return [self._to_scheduled_task(row) for row in rows]

    async def cancel(self, *, task_id: int, user_id: str, include_all: bool = False) -> bool:
        where = "id = ? AND status = 'pending'"
        params: list[object] = [task_id]
        if not include_all:
            where += " AND user_id = ?"
            params.append(user_id)
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                f"""
                UPDATE scheduled_tasks
                SET status = 'cancelled',
                    completed_at = datetime('now')
                WHERE {where}
                """,
                tuple(params),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0

    async def mark_completed(self, task_id: int) -> None:
        await self._mark(task_id, "completed")

    async def mark_failed(self, task_id: int) -> None:
        await self._mark(task_id, "failed")

    async def mark_cancelled(self, task_id: int) -> None:
        await self._mark(task_id, "cancelled")

    async def _mark(self, task_id: int, status: str) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?,
                    completed_at = datetime('now')
                WHERE id = ?
                """,
                (status, task_id),
            )
            await db.commit()

    def _to_scheduled_task(self, row: aiosqlite.Row) -> ScheduledTask:
        return ScheduledTask(
            id=int(row["id"]),
            task_type=str(row["task_type"]),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            user_id=str(row["user_id"]),
            user_name=row["user_name"],
            message=str(row["message"]),
            due_at=str(row["due_at"]),
            status=str(row["status"]),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )


class StickerAssetRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def upsert(
        self,
        *,
        asset_id: str,
        source_scope_type: str,
        source_scope_id: str,
        source_user_id: str,
        source_message_id: str | None,
        file_path: str,
        url_hash: str,
        media_type: str,
        source_file: str | None,
        tags: str,
        risk_level: str = "safe",
    ) -> StickerAsset:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO sticker_assets (
                  asset_id,
                  source_scope_type,
                  source_scope_id,
                  source_user_id,
                  source_message_id,
                  file_path,
                  url_hash,
                  media_type,
                  source_file,
                  tags,
                  risk_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                  url_hash = excluded.url_hash,
                  tags = CASE
                    WHEN excluded.tags != '' THEN excluded.tags
                    ELSE sticker_assets.tags
                  END,
                  risk_level = excluded.risk_level
                ON CONFLICT(url_hash) DO UPDATE SET
                  asset_id = excluded.asset_id,
                  file_path = excluded.file_path,
                  tags = CASE
                    WHEN excluded.tags != '' THEN excluded.tags
                    ELSE sticker_assets.tags
                  END,
                  risk_level = excluded.risk_level
                """,
                (
                    asset_id,
                    source_scope_type,
                    source_scope_id,
                    source_user_id,
                    source_message_id,
                    file_path,
                    url_hash,
                    media_type,
                    source_file,
                    tags,
                    risk_level,
                ),
            )
            await db.commit()
        asset = await self.get_by_asset_id(asset_id)
        if asset is None:
            asset = await self.get_by_url_hash(url_hash)
        if asset is None:
            raise RuntimeError("sticker asset upsert did not create a row")
        return asset

    async def get_by_url_hash(self, url_hash: str) -> StickerAsset | None:
        return await self._get_one("url_hash = ?", (url_hash,))

    async def get_by_asset_id(self, asset_id: str | None) -> StickerAsset | None:
        if not asset_id:
            return None
        return await self._get_one("asset_id = ?", (asset_id,))

    async def count(self) -> int:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM sticker_assets")
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def find_matching(
        self,
        *,
        query_tags: list[str],
        limit: int = 20,
    ) -> list[StickerAsset]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  asset_id,
                  source_scope_type,
                  source_scope_id,
                  source_user_id,
                  source_message_id,
                  file_path,
                  url_hash,
                  media_type,
                  source_file,
                  tags,
                  risk_level,
                  usage_count,
                  created_at,
                  last_used_at
                FROM sticker_assets
                WHERE risk_level = 'safe'
                ORDER BY usage_count ASC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        assets = [self._to_sticker_asset(row) for row in rows]
        if not query_tags:
            return assets
        lowered = [tag.lower() for tag in query_tags]
        matched = [
            asset
            for asset in assets
            if any(tag in asset.tags.lower() for tag in lowered)
        ]
        return matched or assets

    async def mark_used(self, asset_id: str) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE sticker_assets
                SET usage_count = usage_count + 1,
                    last_used_at = datetime('now')
                WHERE asset_id = ?
                """,
                (asset_id,),
            )
            await db.commit()

    async def _get_one(
        self,
        where: str,
        params: tuple[object, ...],
    ) -> StickerAsset | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                  asset_id,
                  source_scope_type,
                  source_scope_id,
                  source_user_id,
                  source_message_id,
                  file_path,
                  url_hash,
                  media_type,
                  source_file,
                  tags,
                  risk_level,
                  usage_count,
                  created_at,
                  last_used_at
                FROM sticker_assets
                WHERE {where}
                LIMIT 1
                """,
                params,
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_sticker_asset(row)

    def _to_sticker_asset(self, row: aiosqlite.Row) -> StickerAsset:
        return StickerAsset(
            asset_id=str(row["asset_id"]),
            source_scope_type=str(row["source_scope_type"]),
            source_scope_id=str(row["source_scope_id"]),
            source_user_id=str(row["source_user_id"]),
            source_message_id=row["source_message_id"],
            file_path=str(row["file_path"]),
            url_hash=str(row["url_hash"]),
            media_type=str(row["media_type"]),
            source_file=row["source_file"],
            tags=str(row["tags"]),
            risk_level=str(row["risk_level"]),
            usage_count=int(row["usage_count"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )


class GroupMessageIndexRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def upsert(
        self,
        *,
        group_id: str,
        message_id: str,
        user_id: str,
        user_name: str | None,
        text: str,
        media_type: str = "",
        sticker_asset_id: str | None = None,
        is_bot: bool = False,
    ) -> None:
        if not group_id or not message_id:
            return
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_message_index (
                  group_id,
                  message_id,
                  user_id,
                  user_name,
                  text,
                  media_type,
                  sticker_asset_id,
                  is_bot,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id, message_id) DO UPDATE SET
                  user_name = excluded.user_name,
                  text = excluded.text,
                  media_type = CASE
                    WHEN excluded.media_type != '' THEN excluded.media_type
                    ELSE group_message_index.media_type
                  END,
                  sticker_asset_id = COALESCE(
                    excluded.sticker_asset_id,
                    group_message_index.sticker_asset_id
                  ),
                  is_bot = excluded.is_bot
                """
                ,
                (
                    group_id,
                    message_id,
                    user_id,
                    user_name,
                    text,
                    media_type,
                    sticker_asset_id,
                    int(is_bot),
                ),
            )
            await db.commit()

    async def get(self, group_id: str, message_id: str | None) -> GroupMessageIndex | None:
        if not message_id:
            return None
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  message_id,
                  user_id,
                  user_name,
                  text,
                  media_type,
                  sticker_asset_id,
                  is_bot,
                  created_at
                FROM group_message_index
                WHERE group_id = ? AND message_id = ?
                LIMIT 1
                """,
                (group_id, message_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_group_message_index(row)

    async def recent_repeatable(self, group_id: str) -> GroupMessageIndex | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  message_id,
                  user_id,
                  user_name,
                  text,
                  media_type,
                  sticker_asset_id,
                  is_bot,
                  created_at
                FROM group_message_index
                WHERE group_id = ?
                  AND is_bot = 0
                  AND (text != '' OR sticker_asset_id IS NOT NULL)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (group_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_group_message_index(row)

    def _to_group_message_index(self, row: aiosqlite.Row) -> GroupMessageIndex:
        return GroupMessageIndex(
            group_id=str(row["group_id"]),
            message_id=str(row["message_id"]),
            user_id=str(row["user_id"]),
            user_name=row["user_name"],
            text=str(row["text"]),
            media_type=str(row["media_type"]),
            sticker_asset_id=row["sticker_asset_id"],
            is_bot=bool(row["is_bot"]),
            created_at=row["created_at"],
        )


class MessageRepeatStateRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def try_mark_repeated(
        self,
        *,
        group_id: str,
        source_message_id: str,
        repeat_kind: str,
        repeated_by: str,
        trigger_user_id: str,
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO message_repeat_states (
                  group_id,
                  source_message_id,
                  repeat_kind,
                  repeated_by,
                  trigger_user_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, source_message_id, repeat_kind, repeated_by, trigger_user_id),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0


class AuditRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def save_reply_audit(
        self,
        *,
        trace_id: str,
        scope_type: str,
        scope_id: str,
        user_id: str,
        action: str,
        reason: str,
        model_called: bool,
        safety_blocked: bool,
        elapsed_ms: int | None,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO reply_audits (
                  trace_id,
                  scope_type,
                  scope_id,
                  user_id,
                  action,
                  reason,
                  model_called,
                  safety_blocked,
                  elapsed_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    scope_type,
                    scope_id,
                    user_id,
                    action,
                    reason,
                    int(model_called),
                    int(safety_blocked),
                    elapsed_ms,
                ),
            )
            await db.commit()

    async def save_system_event(
        self,
        *,
        level: str,
        event: str,
        detail: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO system_events (
                  level,
                  event,
                  detail,
                  trace_id
                ) VALUES (?, ?, ?, ?)
                """,
                (level, event, _sanitize_detail(detail), trace_id),
            )
            await db.commit()

    async def get_recent_reply_audits(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  trace_id,
                  scope_type,
                  scope_id,
                  user_id,
                  action,
                  reason,
                  model_called,
                  safety_blocked,
                  elapsed_ms,
                  created_at
                FROM reply_audits
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def get_recent_system_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  level,
                  event,
                  detail,
                  trace_id,
                  created_at
                FROM system_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]


def _sanitize_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    compact = " ".join(str(detail).split())
    for marker in ("Bearer ", "sk-", "QQ_BOT_MODEL_API_KEY", "QQ_BOT_ONEBOT_TOKEN"):
        if marker in compact:
            return "[redacted]"
    secret_patterns = (
        re.compile(r"fe_oa_[A-Za-z0-9]+", re.IGNORECASE),
        re.compile(r"(api[_-]?key|token|authorization)\s*[:=]\s*\S+", re.IGNORECASE),
    )
    if any(pattern.search(compact) for pattern in secret_patterns):
        return "[redacted]"
    return compact[:300]
