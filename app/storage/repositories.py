from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import (
    ConversationSession,
    GroupContext,
    GroupNewsDeliveryCheckpoint,
    GroupMemberProfile,
    GroupNewsSubscription,
    GroupMessageIndex,
    GroupMuteState,
    GroupPendingQuestion,
    GroupSemanticTerm,
    MemoryProfile,
    PersonaState,
    ScheduledTask,
    SessionMemory,
    StockWatchItem,
    StickerAsset,
    StickerAssetAnalysis,
)
from app.storage.database import connect_database


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
        session_id: str | None = None,
    ) -> None:
        async with connect_database(self._database_path) as db:
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
                  message_id,
                  session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    session_id,
                ),
            )
            await db.commit()

    async def get_recent_conversations(
        self,
        scope_type: str,
        scope_id: str,
        *,
        limit: int,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["scope_type = ?", "scope_id = ?"]
        parameters: list[Any] = [scope_type, scope_id]
        if session_id is not None:
            conditions.append("session_id = ?")
            parameters.append(session_id)
        parameters.append(limit)
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                  trace_id,
                  scope_type,
                  scope_id,
                  user_id,
                  user_name,
                  role,
                  content,
                  message_id,
                  session_id,
                  created_at
                FROM conversations
                WHERE {' AND '.join(conditions)}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]


class ConversationSessionRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def create(
        self,
        *,
        scope_type: str,
        scope_id: str,
        initiator_user_id: str,
        root_message_id: str | None,
        now,
        expires_at,
    ) -> ConversationSession:
        session_id = uuid4().hex
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO conversation_sessions (
                  session_id,
                  scope_type,
                  scope_id,
                  initiator_user_id,
                  root_message_id,
                  status,
                  last_activity_at,
                  expires_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_id,
                    scope_type,
                    scope_id,
                    initiator_user_id,
                    root_message_id,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            await db.commit()
        session = await self.get(session_id)
        if session is None:
            raise RuntimeError("created conversation session was not found")
        return session

    async def get(self, session_id: str) -> ConversationSession | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  session_id,
                  scope_type,
                  scope_id,
                  initiator_user_id,
                  root_message_id,
                  status,
                  last_activity_at,
                  expires_at,
                  close_reason,
                  closed_at,
                  created_at
                FROM conversation_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
        return self._to_conversation_session(row) if row is not None else None

    async def get_latest_for_scope(
        self,
        scope_type: str,
        scope_id: str,
        *,
        initiator_user_id: str | None = None,
    ) -> ConversationSession | None:
        conditions = [
            "scope_type = ?",
            "scope_id = ?",
            "status IN ('active', 'dormant', 'suspended')",
        ]
        parameters: list[Any] = [scope_type, scope_id]
        if initiator_user_id is not None:
            conditions.append("initiator_user_id = ?")
            parameters.append(initiator_user_id)
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                  session_id,
                  scope_type,
                  scope_id,
                  initiator_user_id,
                  root_message_id,
                  status,
                  last_activity_at,
                  expires_at,
                  close_reason,
                  closed_at,
                  created_at
                FROM conversation_sessions
                WHERE {' AND '.join(conditions)}
                ORDER BY last_activity_at DESC, created_at DESC
                LIMIT 1
                """,
                parameters,
            )
            row = await cursor.fetchone()
        return self._to_conversation_session(row) if row is not None else None

    async def activate(
        self,
        session_id: str,
        *,
        now,
        expires_at,
    ) -> ConversationSession:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                UPDATE conversation_sessions
                SET
                  status = 'active',
                  last_activity_at = ?,
                  expires_at = ?,
                  close_reason = NULL,
                  closed_at = NULL
                WHERE session_id = ?
                """,
                (now.isoformat(), expires_at.isoformat(), session_id),
            )
            await db.commit()
        session = await self.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def mark_dormant(self, session_id: str) -> None:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                UPDATE conversation_sessions
                SET status = 'dormant'
                WHERE session_id = ? AND status = 'active'
                """,
                (session_id,),
            )
            await db.commit()

    async def suspend_scope(self, scope_type: str, scope_id: str) -> int:
        async with connect_database(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE conversation_sessions
                SET status = 'suspended'
                WHERE scope_type = ?
                  AND scope_id = ?
                  AND status IN ('active', 'dormant')
                """,
                (scope_type, scope_id),
            )
            await db.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _to_conversation_session(row: aiosqlite.Row) -> ConversationSession:
        return ConversationSession(
            session_id=str(row["session_id"]),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            initiator_user_id=str(row["initiator_user_id"]),
            root_message_id=row["root_message_id"],
            status=str(row["status"]),
            last_activity_at=str(row["last_activity_at"]),
            expires_at=str(row["expires_at"]),
            close_reason=row["close_reason"],
            closed_at=row["closed_at"],
            created_at=row["created_at"],
        )


class SessionMemoryRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get(self, session_id: str) -> SessionMemory | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_id, summary, keywords, sample_count, state, updated_at
                FROM session_memories
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
        return self._to_session_memory(row) if row is not None else None

    async def upsert(
        self,
        *,
        session_id: str,
        summary: str,
        keywords: tuple[str, ...],
        sample_count: int,
        state: str = "temporary",
    ) -> SessionMemory:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO session_memories (
                  session_id, summary, keywords, sample_count, state, updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(session_id) DO UPDATE SET
                  summary = excluded.summary,
                  keywords = excluded.keywords,
                  sample_count = excluded.sample_count,
                  state = excluded.state,
                  updated_at = datetime('now')
                """,
                (
                    session_id,
                    summary,
                    json.dumps(list(keywords), ensure_ascii=False),
                    sample_count,
                    state,
                ),
            )
            await db.commit()
        memory = await self.get(session_id)
        if memory is None:
            raise RuntimeError("upserted session memory was not found")
        return memory

    @staticmethod
    def _to_session_memory(row: aiosqlite.Row) -> SessionMemory:
        try:
            keywords = tuple(str(value) for value in json.loads(str(row["keywords"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            keywords = tuple(
                value for value in str(row["keywords"]).split(",") if value
            )
        return SessionMemory(
            session_id=str(row["session_id"]),
            summary=str(row["summary"]),
            keywords=keywords,
            sample_count=int(row["sample_count"]),
            state=str(row["state"]),
            updated_at=row["updated_at"],
        )


class GroupMemberProfileRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get(self, group_id: str, user_id: str) -> GroupMemberProfile | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  user_id,
                  display_name,
                  summary,
                  metrics_json,
                  message_count,
                  updated_at
                FROM group_member_profiles
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            )
            row = await cursor.fetchone()
        return self._to_profile(row) if row is not None else None

    async def upsert(
        self,
        *,
        group_id: str,
        user_id: str,
        display_name: str | None,
        summary: str,
        metrics: dict[str, int],
        message_count: int,
    ) -> GroupMemberProfile:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_member_profiles (
                  group_id,
                  user_id,
                  display_name,
                  summary,
                  metrics_json,
                  message_count,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  summary = excluded.summary,
                  metrics_json = excluded.metrics_json,
                  message_count = excluded.message_count,
                  updated_at = datetime('now')
                """,
                (
                    group_id,
                    user_id,
                    display_name,
                    summary,
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    message_count,
                ),
            )
            await db.commit()
        profile = await self.get(group_id, user_id)
        if profile is None:
            raise RuntimeError("updated group member profile was not found")
        return profile

    @staticmethod
    def _to_profile(row: aiosqlite.Row) -> GroupMemberProfile:
        raw_metrics = json.loads(str(row["metrics_json"] or "{}"))
        metrics = {
            str(key): int(value)
            for key, value in raw_metrics.items()
            if type(value) is int and int(value) >= 0
        }
        return GroupMemberProfile(
            group_id=str(row["group_id"]),
            user_id=str(row["user_id"]),
            display_name=row["display_name"],
            summary=str(row["summary"]),
            metrics=metrics,
            message_count=int(row["message_count"]),
            updated_at=row["updated_at"],
        )


class GroupNewsSubscriptionRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get(self, group_id: str) -> GroupNewsSubscription | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  enabled,
                  send_time,
                  timezone,
                  categories,
                  last_sent_date,
                  updated_by,
                  updated_at
                FROM group_news_subscriptions
                WHERE group_id = ?
                """,
                (group_id,),
            )
            row = await cursor.fetchone()
        return self._to_subscription(row) if row is not None else None

    async def set_enabled(
        self,
        *,
        group_id: str,
        enabled: bool,
        send_time: str,
        timezone: str,
        categories: tuple[str, ...],
        updated_by: str,
    ) -> GroupNewsSubscription:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_news_subscriptions (
                  group_id,
                  enabled,
                  send_time,
                  timezone,
                  categories,
                  updated_by,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                  enabled = excluded.enabled,
                  send_time = excluded.send_time,
                  timezone = excluded.timezone,
                  categories = excluded.categories,
                  updated_by = excluded.updated_by,
                  updated_at = datetime('now')
                """,
                (
                    group_id,
                    int(enabled),
                    send_time,
                    timezone,
                    ",".join(categories),
                    updated_by,
                ),
            )
            await db.execute(
                "DELETE FROM group_news_delivery_checkpoints WHERE group_id = ?",
                (group_id,),
            )
            await db.commit()
        subscription = await self.get(group_id)
        if subscription is None:
            raise RuntimeError("updated news subscription was not found")
        return subscription

    async def list_due(
        self,
        *,
        local_date: date,
        local_time: str,
    ) -> list[GroupNewsSubscription]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  enabled,
                  send_time,
                  timezone,
                  categories,
                  last_sent_date,
                  updated_by,
                  updated_at
                FROM group_news_subscriptions
                WHERE enabled = 1
                  AND send_time <= ?
                  AND (last_sent_date IS NULL OR last_sent_date <> ?)
                ORDER BY send_time, group_id
                """,
                (local_time, local_date.isoformat()),
            )
            rows = await cursor.fetchall()
        return [self._to_subscription(row) for row in rows]

    async def mark_sent(self, group_id: str, local_date: date) -> None:
        await self.complete_delivery(group_id, local_date)

    async def get_or_create_delivery_checkpoint(
        self,
        *,
        group_id: str,
        local_date: date,
        messages: tuple[str, ...],
    ) -> GroupNewsDeliveryCheckpoint:
        serialized = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
        delivery_date = local_date.isoformat()
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                DELETE FROM group_news_delivery_checkpoints
                WHERE group_id = ? AND delivery_date < ?
                """,
                (group_id, delivery_date),
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO group_news_delivery_checkpoints (
                  group_id,
                  delivery_date,
                  messages_json,
                  next_message_index
                ) VALUES (?, ?, ?, 0)
                """,
                (group_id, delivery_date, serialized),
            )
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  delivery_date,
                  messages_json,
                  next_message_index,
                  created_at,
                  updated_at
                FROM group_news_delivery_checkpoints
                WHERE group_id = ? AND delivery_date = ?
                """,
                (group_id, delivery_date),
            )
            row = await cursor.fetchone()
            await db.commit()
        if row is None:
            raise RuntimeError("news delivery checkpoint was not created")
        return self._to_delivery_checkpoint(row)

    async def get_delivery_checkpoint(
        self,
        *,
        group_id: str,
        local_date: date,
    ) -> GroupNewsDeliveryCheckpoint | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  delivery_date,
                  messages_json,
                  next_message_index,
                  created_at,
                  updated_at
                FROM group_news_delivery_checkpoints
                WHERE group_id = ? AND delivery_date = ?
                """,
                (group_id, local_date.isoformat()),
            )
            row = await cursor.fetchone()
        return self._to_delivery_checkpoint(row) if row is not None else None

    async def advance_delivery_checkpoint(
        self,
        *,
        group_id: str,
        local_date: date,
        expected_index: int,
    ) -> None:
        async with connect_database(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE group_news_delivery_checkpoints
                SET next_message_index = next_message_index + 1,
                    updated_at = datetime('now')
                WHERE group_id = ?
                  AND delivery_date = ?
                  AND next_message_index = ?
                """,
                (group_id, local_date.isoformat(), expected_index),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError("news delivery checkpoint did not advance")
            await db.commit()

    async def complete_delivery(self, group_id: str, local_date: date) -> None:
        delivery_date = local_date.isoformat()
        async with connect_database(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE group_news_subscriptions
                SET last_sent_date = ?, updated_at = datetime('now')
                WHERE group_id = ? AND enabled = 1
                """,
                (delivery_date, group_id),
            )
            await db.execute(
                """
                DELETE FROM group_news_delivery_checkpoints
                WHERE group_id = ? AND delivery_date = ?
                """,
                (group_id, delivery_date),
            )
            await db.commit()

    @staticmethod
    def _to_subscription(row: aiosqlite.Row) -> GroupNewsSubscription:
        return GroupNewsSubscription(
            group_id=str(row["group_id"]),
            enabled=bool(row["enabled"]),
            send_time=str(row["send_time"]),
            timezone=str(row["timezone"]),
            categories=tuple(
                item for item in str(row["categories"] or "").split(",") if item
            ),
            last_sent_date=row["last_sent_date"],
            updated_by=str(row["updated_by"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_delivery_checkpoint(
        row: aiosqlite.Row,
    ) -> GroupNewsDeliveryCheckpoint:
        try:
            raw_messages = json.loads(str(row["messages_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("news delivery checkpoint JSON is invalid") from exc
        if not isinstance(raw_messages, list) or not all(
            isinstance(item, str) for item in raw_messages
        ):
            raise RuntimeError("news delivery checkpoint messages are invalid")
        next_index = int(row["next_message_index"])
        if next_index < 0 or next_index > len(raw_messages):
            raise RuntimeError("news delivery checkpoint cursor is invalid")
        return GroupNewsDeliveryCheckpoint(
            group_id=str(row["group_id"]),
            delivery_date=str(row["delivery_date"]),
            messages=tuple(raw_messages),
            next_message_index=next_index,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class StockWatchRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def upsert(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        symbol: str,
        market: str,
        cost_price: float | None,
        quantity: float | None,
        alert_threshold_percent: float,
    ) -> StockWatchItem:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO stock_watch_items (
                  user_id,
                  scope_type,
                  scope_id,
                  symbol,
                  market,
                  cost_price,
                  quantity,
                  alert_threshold_percent,
                  enabled,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                ON CONFLICT(user_id, scope_type, scope_id, symbol) DO UPDATE SET
                  market = excluded.market,
                  cost_price = excluded.cost_price,
                  quantity = excluded.quantity,
                  alert_threshold_percent = excluded.alert_threshold_percent,
                  enabled = 1,
                  updated_at = datetime('now')
                """,
                (
                    user_id,
                    scope_type,
                    scope_id,
                    symbol,
                    market,
                    cost_price,
                    quantity,
                    alert_threshold_percent,
                ),
            )
            await db.commit()
        item = await self.get(user_id, scope_type, scope_id, symbol)
        if item is None:
            raise RuntimeError("updated stock watch item was not found")
        return item

    async def get(
        self,
        user_id: str,
        scope_type: str,
        scope_id: str,
        symbol: str,
    ) -> StockWatchItem | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM stock_watch_items
                WHERE user_id = ? AND scope_type = ? AND scope_id = ? AND symbol = ?
                """,
                (user_id, scope_type, scope_id, symbol),
            )
            row = await cursor.fetchone()
        return self._to_watch_item(row) if row is not None else None

    async def delete(
        self,
        user_id: str,
        scope_type: str,
        scope_id: str,
        symbol: str,
    ) -> bool:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                DELETE FROM stock_alert_states
                WHERE watch_item_id IN (
                  SELECT id FROM stock_watch_items
                  WHERE user_id = ? AND scope_type = ? AND scope_id = ? AND symbol = ?
                )
                """,
                (user_id, scope_type, scope_id, symbol),
            )
            cursor = await db.execute(
                """
                DELETE FROM stock_watch_items
                WHERE user_id = ? AND scope_type = ? AND scope_id = ? AND symbol = ?
                """,
                (user_id, scope_type, scope_id, symbol),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def list_for_scope(
        self,
        user_id: str,
        scope_type: str,
        scope_id: str,
    ) -> list[StockWatchItem]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM stock_watch_items
                WHERE user_id = ? AND scope_type = ? AND scope_id = ? AND enabled = 1
                ORDER BY market, symbol
                """,
                (user_id, scope_type, scope_id),
            )
            rows = await cursor.fetchall()
        return [self._to_watch_item(row) for row in rows]

    async def list_enabled(self, *, limit: int = 500) -> list[StockWatchItem]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM stock_watch_items
                WHERE enabled = 1
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [self._to_watch_item(row) for row in rows]

    async def record_alert_once(
        self,
        *,
        watch_item_id: int,
        trading_date: date,
        direction: str,
        last_price: float,
    ) -> bool:
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        async with connect_database(self._database_path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO stock_alert_states (
                  watch_item_id,
                  trading_date,
                  direction,
                  last_price
                ) VALUES (?, ?, ?, ?)
                """,
                (watch_item_id, trading_date.isoformat(), direction, last_price),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def release_alert(
        self,
        *,
        watch_item_id: int,
        trading_date: date,
        direction: str,
    ) -> None:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                DELETE FROM stock_alert_states
                WHERE watch_item_id = ? AND trading_date = ? AND direction = ?
                """,
                (watch_item_id, trading_date.isoformat(), direction),
            )
            await db.commit()

    @staticmethod
    def _to_watch_item(row: aiosqlite.Row) -> StockWatchItem:
        return StockWatchItem(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            symbol=str(row["symbol"]),
            market=str(row["market"]),
            cost_price=float(row["cost_price"]) if row["cost_price"] is not None else None,
            quantity=float(row["quantity"]) if row["quantity"] is not None else None,
            alert_threshold_percent=float(row["alert_threshold_percent"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class PersonaStateRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get_or_create(self, scope_type: str, scope_id: str) -> PersonaState:
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  muted,
                  mode,
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
        return await self.set_mode(
            group_id=group_id,
            mode="chat_muted" if muted else "normal",
            updated_by=updated_by,
            reason=reason,
        )

    async def set_mode(
        self,
        *,
        group_id: str,
        mode: str,
        updated_by: str,
        reason: str,
    ) -> GroupMuteState:
        if mode not in {"normal", "chat_muted", "all_muted"}:
            raise ValueError(f"unsupported group mute mode: {mode}")
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_mute_states (
                  group_id,
                  muted,
                  mode,
                  updated_by,
                  reason,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id) DO UPDATE SET
                  muted = excluded.muted,
                  mode = excluded.mode,
                  updated_by = excluded.updated_by,
                  reason = excluded.reason,
                  updated_at = datetime('now')
                """,
                (group_id, int(mode != "normal"), mode, updated_by, reason),
            )
            await db.commit()
        state = await self.get_by_group_id(group_id)
        if state is None:
            raise RuntimeError("group mute state upsert did not create a row")
        return state

    async def list_muted(self, *, limit: int = 20) -> list[GroupMuteState]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  muted,
                  mode,
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
        async with connect_database(self._database_path) as db:
            cursor = await db.execute(
                f"""
                UPDATE group_mute_states
                SET
                  muted = 0,
                  mode = 'normal',
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
            mode=str(row["mode"]),
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
        session_id: str | None = None,
    ) -> None:
        if not message_id:
            return
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO bot_sent_messages (
                  message_id,
                  trace_id,
                  group_id,
                  user_id,
                  original_message_id,
                  session_id,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    message_id,
                    trace_id,
                    group_id,
                    user_id,
                    original_message_id,
                    session_id,
                ),
            )
            await db.commit()

    async def is_bot_sent_message(self, message_id: str | None) -> bool:
        if not message_id:
            return False
        async with connect_database(self._database_path) as db:
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

    async def get_session_id(self, message_id: str | None) -> str | None:
        if not message_id:
            return None
        async with connect_database(self._database_path) as db:
            cursor = await db.execute(
                """
                SELECT session_id
                FROM bot_sent_messages
                WHERE message_id = ?
                LIMIT 1
                """,
                (message_id,),
            )
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])


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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM sticker_assets")
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def find_matching(
        self,
        *,
        query_tags: list[str],
        limit: int = 20,
    ) -> list[StickerAsset]:
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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


class StickerAssetAnalysisRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def get(self, asset_id: str | None) -> StickerAssetAnalysis | None:
        if not asset_id:
            return None
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  asset_id,
                  intent_summary,
                  emotion_tags,
                  scene_tags,
                  text_tags,
                  reply_usage_hint,
                  safety_category,
                  analysis_status,
                  analyzed_at,
                  updated_at
                FROM sticker_asset_analysis
                WHERE asset_id = ?
                LIMIT 1
                """,
                (asset_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_analysis(row)

    async def ensure_pending(self, asset_id: str) -> StickerAssetAnalysis:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO sticker_asset_analysis (
                  asset_id,
                  analysis_status,
                  updated_at
                ) VALUES (?, 'pending', datetime('now'))
                """,
                (asset_id,),
            )
            await db.commit()
        analysis = await self.get(asset_id)
        if analysis is None:
            raise RuntimeError("sticker analysis pending row was not created")
        return analysis

    async def upsert_completed(
        self,
        *,
        asset_id: str,
        intent_summary: str,
        emotion_tags: str,
        scene_tags: str,
        text_tags: str,
        reply_usage_hint: str,
        safety_category: str,
    ) -> StickerAssetAnalysis:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO sticker_asset_analysis (
                  asset_id,
                  intent_summary,
                  emotion_tags,
                  scene_tags,
                  text_tags,
                  reply_usage_hint,
                  safety_category,
                  analysis_status,
                  analyzed_at,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', datetime('now'), datetime('now'))
                ON CONFLICT(asset_id) DO UPDATE SET
                  intent_summary = excluded.intent_summary,
                  emotion_tags = excluded.emotion_tags,
                  scene_tags = excluded.scene_tags,
                  text_tags = excluded.text_tags,
                  reply_usage_hint = excluded.reply_usage_hint,
                  safety_category = excluded.safety_category,
                  analysis_status = 'completed',
                  analyzed_at = datetime('now'),
                  updated_at = datetime('now')
                """,
                (
                    asset_id,
                    intent_summary,
                    emotion_tags,
                    scene_tags,
                    text_tags,
                    reply_usage_hint,
                    safety_category,
                ),
            )
            await db.commit()
        analysis = await self.get(asset_id)
        if analysis is None:
            raise RuntimeError("sticker analysis upsert did not create a row")
        return analysis

    async def mark_failed(
        self,
        *,
        asset_id: str,
        safety_category: str = "unknown",
    ) -> StickerAssetAnalysis:
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO sticker_asset_analysis (
                  asset_id,
                  safety_category,
                  analysis_status,
                  updated_at
                ) VALUES (?, ?, 'failed', datetime('now'))
                ON CONFLICT(asset_id) DO UPDATE SET
                  safety_category = excluded.safety_category,
                  analysis_status = 'failed',
                  updated_at = datetime('now')
                """,
                (asset_id, safety_category),
            )
            await db.commit()
        analysis = await self.get(asset_id)
        if analysis is None:
            raise RuntimeError("sticker analysis failure row was not created")
        return analysis

    async def find_matching_asset_ids(
        self,
        *,
        query_tags: list[str],
        limit: int = 50,
    ) -> list[str]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  asset_id,
                  intent_summary,
                  emotion_tags,
                  scene_tags,
                  text_tags,
                  reply_usage_hint,
                  safety_category,
                  analysis_status
                FROM sticker_asset_analysis
                WHERE analysis_status = 'completed'
                  AND safety_category NOT IN ('adult', 'illegal', 'violence', 'privacy')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        if not rows:
            return []
        if not query_tags:
            return [str(row["asset_id"]) for row in rows]
        lowered_tags = [tag.lower() for tag in query_tags if tag.strip()]
        scored: list[tuple[int, str]] = []
        for row in rows:
            haystack = " ".join(
                str(row[key] or "")
                for key in (
                    "intent_summary",
                    "emotion_tags",
                    "scene_tags",
                    "text_tags",
                    "reply_usage_hint",
                )
            ).lower()
            score = sum(1 for tag in lowered_tags if tag in haystack)
            if score > 0:
                scored.append((score, str(row["asset_id"])))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [asset_id for _, asset_id in scored]

    async def list_failed_unknown(self, *, limit: int = 200) -> list[StickerAssetAnalysis]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  asset_id,
                  intent_summary,
                  emotion_tags,
                  scene_tags,
                  text_tags,
                  reply_usage_hint,
                  safety_category,
                  analysis_status,
                  analyzed_at,
                  updated_at
                FROM sticker_asset_analysis
                WHERE analysis_status = 'failed'
                  AND safety_category = 'unknown'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [self._to_analysis(row) for row in rows]

    def _to_analysis(self, row: aiosqlite.Row) -> StickerAssetAnalysis:
        return StickerAssetAnalysis(
            asset_id=str(row["asset_id"]),
            intent_summary=str(row["intent_summary"]),
            emotion_tags=str(row["emotion_tags"]),
            scene_tags=str(row["scene_tags"]),
            text_tags=str(row["text_tags"]),
            reply_usage_hint=str(row["reply_usage_hint"]),
            safety_category=str(row["safety_category"]),
            analysis_status=str(row["analysis_status"]),
            analyzed_at=row["analyzed_at"],
            updated_at=row["updated_at"],
        )


class GroupSemanticTermRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    async def upsert(
        self,
        *,
        group_id: str,
        term: str,
        description: str,
        source: str = "rule",
        confidence: float = 0.5,
    ) -> GroupSemanticTerm:
        term = term.strip()
        description = description.strip()
        if not group_id or not term or not description:
            raise ValueError("group_id, term and description are required")
        async with connect_database(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO group_semantic_terms (
                  group_id,
                  term,
                  description,
                  source,
                  confidence,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(group_id, term) DO UPDATE SET
                  description = CASE
                    WHEN excluded.confidence >= group_semantic_terms.confidence
                    THEN excluded.description
                    ELSE group_semantic_terms.description
                  END,
                  source = CASE
                    WHEN excluded.confidence >= group_semantic_terms.confidence
                    THEN excluded.source
                    ELSE group_semantic_terms.source
                  END,
                  confidence = CASE
                    WHEN excluded.confidence >= group_semantic_terms.confidence
                    THEN excluded.confidence
                    ELSE group_semantic_terms.confidence
                  END,
                  updated_at = datetime('now')
                """,
                (group_id, term, description, source, confidence),
            )
            await db.commit()
        term_row = await self.get(group_id, term)
        if term_row is None:
            raise RuntimeError("group semantic term upsert did not create a row")
        return term_row

    async def get(self, group_id: str, term: str) -> GroupSemanticTerm | None:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  term,
                  description,
                  source,
                  confidence,
                  updated_at
                FROM group_semantic_terms
                WHERE group_id = ? AND term = ?
                LIMIT 1
                """,
                (group_id, term),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._to_term(row)

    async def find_relevant(
        self,
        *,
        group_id: str,
        text: str,
        limit: int = 8,
    ) -> list[GroupSemanticTerm]:
        if not group_id or not text.strip():
            return []
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  term,
                  description,
                  source,
                  confidence,
                  updated_at
                FROM group_semantic_terms
                WHERE group_id = ?
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 100
                """,
                (group_id,),
            )
            rows = await cursor.fetchall()
        lowered = text.lower()
        matched = [
            self._to_term(row)
            for row in rows
            if str(row["term"]).lower() in lowered
        ]
        return matched[:limit]

    async def list_recent(self, group_id: str, *, limit: int = 20) -> list[GroupSemanticTerm]:
        async with connect_database(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                  group_id,
                  term,
                  description,
                  source,
                  confidence,
                  updated_at
                FROM group_semantic_terms
                WHERE group_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (group_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._to_term(row) for row in rows]

    def _to_term(self, row: aiosqlite.Row) -> GroupSemanticTerm:
        return GroupSemanticTerm(
            group_id=str(row["group_id"]),
            term=str(row["term"]),
            description=str(row["description"]),
            source=str(row["source"]),
            confidence=float(row["confidence"]),
            updated_at=row["updated_at"],
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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

    async def recent_messages(self, group_id: str, *, limit: int = 10) -> list[GroupMessageIndex]:
        async with connect_database(self._database_path) as db:
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
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (group_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._to_group_message_index(row) for row in rows]

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
        async with connect_database(self._database_path) as db:
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

    async def any_repeated(
        self,
        *,
        group_id: str,
        source_message_ids: list[str],
        repeat_kind: str,
    ) -> bool:
        if not source_message_ids:
            return False
        placeholders = ",".join("?" for _ in source_message_ids)
        async with connect_database(self._database_path) as db:
            cursor = await db.execute(
                f"""
                SELECT 1
                FROM message_repeat_states
                WHERE group_id = ?
                  AND repeat_kind = ?
                  AND source_message_id IN ({placeholders})
                LIMIT 1
                """,
                (group_id, repeat_kind, *source_message_ids),
            )
            row = await cursor.fetchone()
        return row is not None


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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
        async with connect_database(self._database_path) as db:
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
