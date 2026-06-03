from __future__ import annotations

import random


def contains_nickname(text: str, nicknames: set[str]) -> bool:
    return any(nickname and nickname in text for nickname in nicknames)


def nickname_probability_passes(probability: float, roll: float | None = None) -> bool:
    clamped = max(0.0, min(1.0, probability))
    value = random.random() if roll is None else roll
    return value < clamped
