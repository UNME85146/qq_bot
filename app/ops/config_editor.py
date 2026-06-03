from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AllowCommandResult:
    text: str
    changed: bool = False


ALLOW_TARGETS = {
    "private": "allowedPrivateUserIds",
    "group": "allowedGroupIds",
}


def handle_allow_config_command(config_path: str | Path, command_text: str) -> AllowCommandResult:
    parts = command_text.strip().split()
    if len(parts) < 3 or parts[0] != "/allow":
        return AllowCommandResult("usage: /allow private|group add|remove|list <id>")

    target = parts[1].lower()
    action = parts[2].lower()
    field_name = ALLOW_TARGETS.get(target)
    if field_name is None:
        return AllowCommandResult("usage: /allow private|group add|remove|list <id>")
    if action not in {"add", "remove", "list"}:
        return AllowCommandResult("usage: /allow private|group add|remove|list <id>")

    path = Path(config_path)
    raw = _read_config(path)
    qq = raw.setdefault("qq", {})
    values = _string_list(qq.setdefault(field_name, []))

    if action == "list":
        label = "private" if target == "private" else "group"
        return AllowCommandResult(f"allowed {label}: {_format_values(values)}")

    if len(parts) != 4:
        return AllowCommandResult("missing id")
    item_id = parts[3].strip()
    if not item_id.isdigit():
        return AllowCommandResult("invalid id")

    if action == "add":
        if item_id in values:
            return AllowCommandResult(f"{item_id} already allowed")
        values.append(item_id)
        qq[field_name] = values
        _write_config(path, raw)
        return AllowCommandResult(f"{item_id} added", changed=True)

    if item_id not in values:
        return AllowCommandResult(f"{item_id} not found")
    qq[field_name] = [value for value in values if value != item_id]
    _write_config(path, raw)
    return AllowCommandResult(f"{item_id} removed", changed=True)


def handle_owner_config_command(config_path: str | Path, command_text: str) -> AllowCommandResult:
    parts = command_text.strip().split()
    if len(parts) < 2 or parts[0] != "/owner":
        return AllowCommandResult("usage: /owner add|remove|list <qq>")

    action = parts[1].lower()
    if action not in {"add", "remove", "list"}:
        return AllowCommandResult("usage: /owner add|remove|list <qq>")

    path = Path(config_path)
    raw = _read_config(path)
    qq = raw.setdefault("qq", {})
    owners = _string_list(qq.setdefault("ownerUserIds", []))
    roots = _string_list(qq.get("rootUserIds", qq.get("ownerUserIds", [])))

    if action == "list":
        return AllowCommandResult(f"owners: {_format_values(owners)}")

    if len(parts) != 3:
        return AllowCommandResult("missing id")
    user_id = parts[2].strip()
    if not user_id.isdigit():
        return AllowCommandResult("invalid id")

    if action == "add":
        if user_id in owners:
            return AllowCommandResult(f"{user_id} already owner")
        owners.append(user_id)
        qq["ownerUserIds"] = owners
        _write_config(path, raw)
        return AllowCommandResult(f"{user_id} owner added", changed=True)

    if user_id not in owners:
        return AllowCommandResult(f"{user_id} not found")
    if user_id in roots:
        return AllowCommandResult("cannot remove root owner")
    if len(owners) <= 1:
        return AllowCommandResult("cannot remove last owner")
    qq["ownerUserIds"] = [value for value in owners if value != user_id]
    _write_config(path, raw)
    return AllowCommandResult(f"{user_id} owner removed", changed=True)


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _write_config(path: Path, raw: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value).strip()]


def _format_values(values: list[str]) -> str:
    return ", ".join(values) if values else "none"
