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

VOICE_SCOPE_FIELDS = {
    "private": "privateEnabled",
    "group": "groupEnabled",
}

VOICE_SCOPE_LABELS = {
    "private": "私聊语音",
    "group": "群聊语音",
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


def handle_voice_config_command(config_path: str | Path, command_text: str) -> AllowCommandResult:
    parts = command_text.strip().split()
    if len(parts) < 2 or parts[0] != "/voice":
        return AllowCommandResult(_voice_usage())

    action = parts[1].lower()
    path = Path(config_path)
    raw = _read_config(path)
    tts = _ensure_speech(raw)

    if action == "status":
        return AllowCommandResult(_voice_status_text(tts))

    if action in {"on", "off"} and len(parts) == 2:
        enabled = action == "on"
        if bool(tts.get("enabled", False)) == enabled:
            return AllowCommandResult(f"语音回复已经{'开启' if enabled else '关闭'}")
        tts["enabled"] = enabled
        _write_config(path, raw)
        return AllowCommandResult(f"语音回复已{'开启' if enabled else '关闭'}", changed=True)

    if action in VOICE_SCOPE_FIELDS:
        if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
            return AllowCommandResult("用法：/voice private|group on|off")
        enabled = parts[2].lower() == "on"
        field_name = VOICE_SCOPE_FIELDS[action]
        if bool(tts.get(field_name, False)) == enabled:
            return AllowCommandResult(
                f"{VOICE_SCOPE_LABELS[action]}已经{'开启' if enabled else '关闭'}"
            )
        tts[field_name] = enabled
        _write_config(path, raw)
        return AllowCommandResult(
            f"{VOICE_SCOPE_LABELS[action]}已{'开启' if enabled else '关闭'}",
            changed=True,
        )

    if action == "profile":
        return AllowCommandResult(
            "远程语音不使用本地 profile，请由管理员配置 speech.voice"
        )

    if action == "gender":
        return AllowCommandResult(
            "远程语音不按本地性别 profile 切换，请由管理员配置 speech.voice"
        )

    if action == "language":
        return AllowCommandResult(
            "远程语音语言由模型和 speech.voice 决定"
        )

    return AllowCommandResult(_voice_usage())


def _ensure_speech(raw: dict[str, Any]) -> dict[str, Any]:
    speech = raw.setdefault("speech", {})
    speech.setdefault("enabled", False)
    speech.setdefault("apiMode", "audio_speech")
    speech.setdefault("baseUrl", "")
    speech.setdefault("apiKeyEnv", "")
    speech.setdefault("model", "")
    speech.setdefault("voice", "")
    speech.setdefault("format", "mp3")
    speech.setdefault("timeoutSeconds", 60)
    speech.setdefault("sendTimeoutSeconds", 60)
    speech.setdefault("cacheDir", "runtime_artifacts/speech")
    speech.setdefault("maxChars", 4096)
    speech.setdefault("privateEnabled", True)
    speech.setdefault("groupEnabled", True)
    speech.setdefault("privateCooldownSeconds", 30)
    speech.setdefault("groupCooldownSeconds", 60)
    speech.setdefault("randomReplyEnabled", True)
    speech.setdefault("maxAudioBytes", 8 * 1024 * 1024)
    return speech


def _voice_status_text(tts: dict[str, Any]) -> str:
    configured = all(
        str(tts.get(field, "")).strip()
        for field in ("baseUrl", "apiKeyEnv", "model", "voice")
    )
    api_mode = str(tts.get("apiMode", "audio_speech")).strip().lower()
    endpoint_path = (
        "/chat/completions"
        if api_mode == "chat_completions_audio"
        else "/audio/speech"
    )
    endpoint = str(tts.get("baseUrl", "")).rstrip("/")
    if endpoint:
        endpoint += endpoint_path
    return "\n".join(
        [
            f"语音回复={'开' if bool(tts.get('enabled', False)) else '关'}",
            f"私聊语音={'开' if bool(tts.get('privateEnabled', True)) else '关'} "
            f"群聊语音={'开' if bool(tts.get('groupEnabled', True)) else '关'}",
            f"接口={'已配置' if configured else '未配置'} {endpoint_path}",
            f"端点={endpoint or '未配置'}",
            f"模型={tts.get('model', '') or '未配置'} 音色={tts.get('voice', '') or '未配置'}",
            f"格式={tts.get('format', '')} 最大字数={tts.get('maxChars', '')}",
            f"随机语音={'开' if bool(tts.get('randomReplyEnabled', True)) else '关'}",
        ]
    )


def _voice_usage() -> str:
    return (
        "用法：/voice status|on|off|private on|off|group on|off；"
        "模型、音色、Base URL 和 API Key 环境变量由管理员配置 speech"
    )


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
