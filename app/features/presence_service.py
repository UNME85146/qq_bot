from __future__ import annotations

import random
import time
from collections import deque

from app.models import NormalizedMessage, PresenceConfig


class BotPresenceService:
    def __init__(
        self,
        config: PresenceConfig,
        *,
        random_fn=None,
        monotonic_fn=None,
    ) -> None:
        self._config = config
        self._random = random_fn or random.random
        self._monotonic = monotonic_fn or time.monotonic
        self._focused_until: dict[str, float] = {}
        self._recent_group_messages: dict[str, deque[float]] = {}

    def focus_group(self, group_id: str | None) -> None:
        if not group_id:
            return
        self._focused_until[group_id] = (
            self._monotonic() + self._config.focus_window_seconds
        )

    def is_focused(self, group_id: str | None) -> bool:
        if not group_id:
            return False
        return self._monotonic() < self._focused_until.get(group_id, 0)

    def should_repeat(
        self,
        message: NormalizedMessage,
        *,
        repeat_kind: str,
        plus_one: bool = False,
    ) -> bool:
        if message.group_id is None:
            return False
        now = self._monotonic()
        self._record_group_message(message.group_id, now)
        probability = self._repeat_probability(message.group_id, repeat_kind, plus_one=plus_one)
        if probability <= 0:
            return False
        return self._random() < probability

    def _repeat_probability(self, group_id: str, repeat_kind: str, *, plus_one: bool) -> float:
        focused = self.is_focused(group_id)
        base = (
            self._config.focused_repeat_probability
            if focused
            else self._config.unfocused_repeat_probability
        )
        if plus_one:
            base = max(base, self._config.plus_one_repeat_probability)
        elif repeat_kind == "sticker":
            base = max(base, self._config.sticker_repeat_probability)
        elif repeat_kind == "text":
            base = max(base, self._config.text_repeat_probability)

        if not focused and self._random() >= self._config.base_online_probability:
            return 0.0
        return max(0.0, min(1.0, base * self._traffic_multiplier(group_id)))

    def _record_group_message(self, group_id: str, now: float) -> None:
        recent = self._recent_group_messages.setdefault(group_id, deque(maxlen=40))
        recent.append(now)
        while recent and now - recent[0] > 60:
            recent.popleft()

    def _traffic_multiplier(self, group_id: str) -> float:
        recent = self._recent_group_messages.get(group_id)
        if not recent:
            return 1.0
        count = len(recent)
        if count >= 25:
            return 0.25
        if count >= 15:
            return 0.5
        return 1.0
