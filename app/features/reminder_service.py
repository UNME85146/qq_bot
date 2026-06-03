from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import NormalizedMessage, QQConfig, ScheduledTask
from app.routing.permission_service import PermissionService
from app.storage.repositories import ScheduledTaskRepository


_RELATIVE_PATTERN = re.compile(
    r"(?P<num>[0-9一二两三四五六七八九十半]+)\s*(?P<unit>秒|分钟|分|小时|个小时|天)后(?P<text>.*)"
)
_ABSOLUTE_PATTERN = re.compile(
    r"(?P<day>明天|今天|今晚|下午|晚上)?\s*(?P<hour>[0-2]?[0-9]|[一二两三四五六七八九十]{1,3})点(?P<minute>[0-5]?[0-9])?(?P<text>.*)"
)
_PREFIX_PATTERN = re.compile(r"^(?:提醒我|提醒一下我|叫我|到点(?:提醒我)?|定时提醒我)\s*")
REMINDER_COMMAND_PREFIX = "/remind"


@dataclass(frozen=True)
class ReminderIntent:
    due_at: datetime
    message: str


class ReminderService:
    def __init__(
        self,
        *,
        repository: ScheduledTaskRepository,
        qq_config: QQConfig,
        now_provider=None,
    ) -> None:
        self._repository = repository
        self._permission_service = PermissionService(qq_config)
        self._now_provider = now_provider or datetime.now

    async def try_create_from_message(
        self,
        message: NormalizedMessage,
    ) -> ScheduledTask | None:
        if message.scope_type != "private" or not self._is_allowed(message):
            return None
        if not is_explicit_reminder_request(message.text):
            return None
        return await self.try_create_private_reminder(
            user_id=message.user_id,
            user_name=message.user_name,
            scope_id=message.scope_id,
            text=message.text,
        )

    async def try_create_private_reminder(
        self,
        *,
        user_id: str,
        user_name: str | None,
        scope_id: str | None,
        text: str,
    ) -> ScheduledTask | None:
        if not self._permission_service.is_private_user_allowed(user_id):
            return None
        if not is_explicit_reminder_request(text):
            return None
        intent = parse_reminder_intent(text, now=self._now_provider())
        if intent is None:
            return None
        return await self._repository.create(
            task_type="reminder",
            scope_type="private",
            scope_id=scope_id or user_id,
            user_id=user_id,
            user_name=user_name,
            message=intent.message,
            due_at=_to_db_time(intent.due_at),
        )

    async def list_for_user(
        self,
        *,
        user_id: str,
        include_all: bool = False,
        limit: int = 10,
    ) -> list[ScheduledTask]:
        return await self._repository.list_for_user(
            user_id=user_id,
            include_all=include_all,
            limit=limit,
        )

    async def cancel(
        self,
        *,
        task_id: int,
        user_id: str,
        include_all: bool = False,
    ) -> bool:
        return await self._repository.cancel(
            task_id=task_id,
            user_id=user_id,
            include_all=include_all,
        )

    def _is_allowed(self, message: NormalizedMessage) -> bool:
        if message.scope_type == "private":
            return self._permission_service.is_private_user_allowed(message.user_id)
        if message.scope_type == "group" and message.group_id is not None:
            return self._permission_service.is_group_allowed(message.group_id)
        return False


def is_reminder_command(text: str) -> bool:
    stripped = text.strip()
    return stripped == REMINDER_COMMAND_PREFIX or stripped.startswith(
        REMINDER_COMMAND_PREFIX + " "
    )


def is_reminder_list_command(text: str) -> bool:
    return text.strip() == "/remind list"


def is_reminder_cancel_command(text: str) -> bool:
    stripped = text.strip()
    return stripped == "/remind cancel" or stripped.startswith("/remind cancel ")


def is_explicit_reminder_request(text: str) -> bool:
    cleaned = _clean_trigger_text(_strip_reminder_command_prefix(text))
    return is_reminder_command(text) or (
        _has_reminder_word(cleaned) and _has_time_signal(cleaned)
    )


def parse_reminder_cancel_id(text: str) -> int | None:
    raw_id = text.strip().removeprefix("/remind cancel").strip()
    if not raw_id.isdigit():
        return None
    return int(raw_id)


def format_reminder_tasks(tasks: list[ScheduledTask]) -> str:
    if not tasks:
        return "reminders=none"
    lines = ["待触发提醒："]
    for task in tasks:
        message = _clean_display_message(task.message)
        lines.append(
            f"- id={task.id} due={task.due_at} scope={task.scope_type}/{task.scope_id} msg={message}"
        )
    return "\n".join(lines)


def parse_reminder_intent(text: str, *, now: datetime | None = None) -> ReminderIntent | None:
    now = now or datetime.now()
    has_command_prefix = is_reminder_command(text)
    cleaned = _clean_trigger_text(_strip_reminder_command_prefix(text))
    relative = _RELATIVE_PATTERN.search(cleaned)
    if relative and (has_command_prefix or _has_reminder_word(cleaned)):
        amount = _parse_number(relative.group("num"))
        if amount is None:
            return None
        due_at = now + _delta(amount, relative.group("unit"))
        message = _cleanup_message(relative.group("text"))
        if message:
            return ReminderIntent(due_at=due_at, message=message)

    absolute = _ABSOLUTE_PATTERN.search(cleaned)
    if absolute and (has_command_prefix or _has_reminder_word(cleaned)):
        hour = _parse_number(absolute.group("hour"))
        minute_text = absolute.group("minute")
        minute = int(minute_text) if minute_text else 0
        if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        hour = int(hour)
        day = absolute.group("day") or ""
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day == "明天":
            target += timedelta(days=1)
        elif day in {"今晚", "晚上", "下午"} and hour < 12:
            target = target.replace(hour=hour + 12)
        if target <= now:
            target += timedelta(days=1)
        message = _cleanup_message(absolute.group("text"))
        if message:
            return ReminderIntent(due_at=target, message=message)
    return None


def _clean_trigger_text(text: str) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _strip_reminder_command_prefix(text: str) -> str:
    stripped = text.strip()
    if stripped == REMINDER_COMMAND_PREFIX:
        return ""
    if stripped.startswith(REMINDER_COMMAND_PREFIX + " "):
        return stripped.removeprefix(REMINDER_COMMAND_PREFIX).strip()
    return text


def _has_time_signal(text: str) -> bool:
    return _RELATIVE_PATTERN.search(text) is not None or _ABSOLUTE_PATTERN.search(text) is not None


def _has_reminder_word(text: str) -> bool:
    return any(word in text for word in ("提醒", "叫我", "到点", "定时", "闹钟"))


def _cleanup_message(text: str) -> str:
    cleaned = _PREFIX_PATTERN.sub("", text.strip(" ，,。:："))
    cleaned = _PREFIX_PATTERN.sub("", cleaned.strip())
    return cleaned[:240]


def _clean_display_message(text: str) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", text)
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 120:
        return cleaned[:117] + "..."
    return cleaned


def _delta(amount: float, unit: str) -> timedelta:
    if unit == "秒":
        return timedelta(seconds=amount)
    if unit in {"分钟", "分"}:
        return timedelta(minutes=amount)
    if unit in {"小时", "个小时"}:
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _parse_number(value: str) -> float | None:
    value = value.strip()
    if value == "半":
        return 0.5
    if value.isdigit():
        return float(value)
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1 if left == "" else None)
        ones = digits.get(right, 0 if right == "" else None)
        if tens is None or ones is None:
            return None
        return float(tens * 10 + ones)
    if value in digits:
        return float(digits[value])
    return None


def _to_db_time(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(sep=" ")
