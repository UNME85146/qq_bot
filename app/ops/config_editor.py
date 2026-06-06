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
    tts = _ensure_tts(raw)

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
        return _handle_voice_profile_command(path, raw, tts, parts)

    if action == "gender":
        if len(parts) != 3:
            return AllowCommandResult("用法：/voice gender male|female|neutral")
        return _set_voice_profile_by_metadata(
            path,
            raw,
            tts,
            gender=parts[2].lower(),
        )

    if action == "language":
        if len(parts) != 3:
            return AllowCommandResult("用法：/voice language <code>")
        return _set_voice_profile_by_metadata(
            path,
            raw,
            tts,
            language=parts[2].lower(),
        )

    return AllowCommandResult(_voice_usage())


def _handle_voice_profile_command(
    path: Path,
    raw: dict[str, Any],
    tts: dict[str, Any],
    parts: list[str],
) -> AllowCommandResult:
    if len(parts) == 3 and parts[2].lower() == "list":
        profiles = _voice_profiles(tts)
        current_id = str(tts.get("defaultVoiceProfileId", ""))
        if not profiles:
            return AllowCommandResult("暂无可用语音 profile")
        lines = ["语音 profile："]
        for profile in profiles:
            marker = "*" if str(profile.get("id", "")) == current_id else "-"
            enabled_text = "开" if bool(profile.get("enabled", True)) else "关"
            lines.append(
                f"{marker} id={profile.get('id', '')} 音色={profile.get('voice', '')} "
                f"语言={profile.get('language', '')} 性别={profile.get('gender', '')} "
                f"启用={enabled_text}"
            )
        return AllowCommandResult("\n".join(lines))

    if len(parts) != 4 or parts[2].lower() != "set":
        return AllowCommandResult("用法：/voice profile list|set <profile_id>")

    profile_id = parts[3].strip()
    profiles = _voice_profiles(tts)
    profile = _find_voice_profile(profiles, profile_id)
    if profile is None:
        return AllowCommandResult(f"没有找到语音 profile：{profile_id}")
    if not bool(profile.get("enabled", True)):
        return AllowCommandResult(f"语音 profile 已停用：{profile_id}")
    if str(tts.get("defaultVoiceProfileId", "")) == profile_id:
        return AllowCommandResult(f"当前已经是语音 profile：{profile_id}")
    _select_voice_profile(tts, profile)
    _write_config(path, raw)
    return AllowCommandResult(f"语音 profile 已切换为：{profile_id}", changed=True)


def _set_voice_profile_by_metadata(
    path: Path,
    raw: dict[str, Any],
    tts: dict[str, Any],
    *,
    gender: str | None = None,
    language: str | None = None,
) -> AllowCommandResult:
    profiles = [profile for profile in _voice_profiles(tts) if bool(profile.get("enabled", True))]
    if not profiles:
        return AllowCommandResult("暂无可用语音 profile")
    current = _find_voice_profile(profiles, str(tts.get("defaultVoiceProfileId", "")))
    current_language = str((current or {}).get("language", "")).lower()
    current_gender = str((current or {}).get("gender", "")).lower()
    candidates = profiles
    if gender is not None:
        candidates = [
            profile
            for profile in candidates
            if str(profile.get("gender", "")).lower() == gender
            and (
                not current_language
                or str(profile.get("language", "")).lower() == current_language
            )
        ]
        label = f"性别={gender}"
    else:
        candidates = [
            profile
            for profile in candidates
            if str(profile.get("language", "")).lower() == language
            and (
                not current_gender
                or str(profile.get("gender", "")).lower() == current_gender
            )
        ]
        label = f"语言={language}"
    if not candidates:
        return AllowCommandResult(f"没有匹配的已启用语音 profile：{label}")
    selected = candidates[0]
    selected_id = str(selected.get("id", ""))
    if str(tts.get("defaultVoiceProfileId", "")) == selected_id:
        return AllowCommandResult(f"当前语音 profile 已经匹配：{label}")
    _select_voice_profile(tts, selected)
    _write_config(path, raw)
    return AllowCommandResult(f"语音 profile 已按{label}切换为：{selected_id}", changed=True)


def _ensure_tts(raw: dict[str, Any]) -> dict[str, Any]:
    tts = raw.setdefault("tts", {})
    tts.setdefault("enabled", False)
    tts.setdefault("provider", "moss_tts_nano")
    tts.setdefault("backend", "onnx")
    tts.setdefault("executionProvider", "cuda")
    tts.setdefault("endpoint", "http://127.0.0.1:18100/tts")
    tts.setdefault("voice", "xiaohuang_default")
    tts.setdefault("format", "wav")
    tts.setdefault("maxChars", 160)
    tts.setdefault("requestTimeoutSeconds", 20)
    tts.setdefault("privateEnabled", False)
    tts.setdefault("groupEnabled", False)
    tts.setdefault("privateCooldownSeconds", 30)
    tts.setdefault("groupCooldownSeconds", 60)
    tts.setdefault("cacheDir", "data/tts/cache")
    tts.setdefault("defaultVoiceProfileId", "xiaohuang_default")
    tts.setdefault("voiceProfiles", [])
    return tts


def _voice_profiles(tts: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = tts.get("voiceProfiles", [])
    return [profile for profile in profiles if isinstance(profile, dict)]


def _find_voice_profile(
    profiles: list[dict[str, Any]],
    profile_id: str,
) -> dict[str, Any] | None:
    for profile in profiles:
        if str(profile.get("id", "")) == profile_id:
            return profile
    return None


def _select_voice_profile(tts: dict[str, Any], profile: dict[str, Any]) -> None:
    tts["defaultVoiceProfileId"] = str(profile.get("id", ""))
    tts["voice"] = str(profile.get("voice", tts.get("voice", "")))


def _voice_status_text(tts: dict[str, Any]) -> str:
    profiles = _voice_profiles(tts)
    current = _find_voice_profile(profiles, str(tts.get("defaultVoiceProfileId", "")))
    if current is None:
        current_text = "无"
    else:
        current_text = (
            f"{current.get('id', '')}/{current.get('voice', '')}/"
            f"{current.get('language', '')}/{current.get('gender', '')}"
        )
    return "\n".join(
        [
            f"语音回复={'开' if bool(tts.get('enabled', False)) else '关'}",
            f"私聊语音={'开' if bool(tts.get('privateEnabled', False)) else '关'} "
            f"群聊语音={'开' if bool(tts.get('groupEnabled', False)) else '关'}",
            f"服务={tts.get('provider', '')} 后端={tts.get('backend', '')} "
            f"推理={tts.get('executionProvider', '')}",
            f"端点={tts.get('endpoint', '')}",
            f"格式={tts.get('format', '')} 最大字数={tts.get('maxChars', '')}",
            f"当前 profile={current_text}",
        ]
    )


def _voice_usage() -> str:
    return (
        "用法：/voice status|on|off|private on|off|group on|off|"
        "profile list|profile set <profile_id>|gender male|female|neutral|language <code>"
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
