from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import MutableMapping


class RateLimiter:
    def __init__(
        self,
        group_cooldown_seconds: float,
        *,
        private_cooldown_seconds: float = 0,
        max_user_messages_per_minute: int = 0,
        max_group_messages_per_minute: int = 0,
    ) -> None:
        self._group_cooldown_seconds = group_cooldown_seconds
        self._private_cooldown_seconds = private_cooldown_seconds
        self._max_user_messages_per_minute = max_user_messages_per_minute
        self._max_group_messages_per_minute = max_group_messages_per_minute
        self._last_group_reply_at: dict[str, float] = {}
        self._last_private_reply_at: dict[str, float] = {}
        self._user_message_times: MutableMapping[str, deque[float]] = defaultdict(deque)
        self._group_message_times: MutableMapping[str, deque[float]] = defaultdict(deque)

    def allow_group(self, group_id: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        previous = self._last_group_reply_at.get(group_id)
        if previous is not None and current - previous < self._group_cooldown_seconds:
            return False
        self._last_group_reply_at[group_id] = current
        return True

    def allow_private(self, user_id: str, now: float | None = None) -> bool:
        if self._private_cooldown_seconds <= 0:
            return True
        current = time.monotonic() if now is None else now
        previous = self._last_private_reply_at.get(user_id)
        if previous is not None and current - previous < self._private_cooldown_seconds:
            return False
        self._last_private_reply_at[user_id] = current
        return True

    def allow_user_minute(self, user_id: str, now: float | None = None) -> bool:
        return self._allow_window(
            self._user_message_times[user_id],
            self._max_user_messages_per_minute,
            now,
        )

    def allow_group_minute(self, group_id: str, now: float | None = None) -> bool:
        return self._allow_window(
            self._group_message_times[group_id],
            self._max_group_messages_per_minute,
            now,
        )

    def _allow_window(
        self,
        timestamps: deque[float],
        max_events: int,
        now: float | None,
    ) -> bool:
        if max_events <= 0:
            return True
        current = time.monotonic() if now is None else now
        window_start = current - 60
        while timestamps and timestamps[0] <= window_start:
            timestamps.popleft()
        if len(timestamps) >= max_events:
            return False
        timestamps.append(current)
        return True
