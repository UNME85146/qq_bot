from __future__ import annotations

from app.models import NormalizedMessage, QQConfig

MUTE_ENABLE_KEYWORDS = (
    "关机",
    "闭嘴",
    "退下",
    "滚",
    "别说话",
    "安静",
    "住嘴",
    "停一下",
    "别回了",
)
MUTE_DISABLE_KEYWORDS = (
    "开机",
    "说话",
    "启动",
    "恢复",
    "回来",
)


def is_group_mute_controller(user_id: str, qq_config: QQConfig) -> bool:
    controller_ids = qq_config.group_mute_controller_user_ids or qq_config.owner_user_ids
    return user_id in controller_ids


def is_group_mute_enable_command(message: NormalizedMessage, qq_config: QQConfig) -> bool:
    return (
        message.is_at_self
        and is_group_mute_controller(message.user_id, qq_config)
        and _contains_any(message.text, MUTE_ENABLE_KEYWORDS)
    )


def is_group_mute_disable_command(message: NormalizedMessage, qq_config: QQConfig) -> bool:
    return (
        message.is_at_self
        and is_group_mute_controller(message.user_id, qq_config)
        and _contains_any(message.text, MUTE_DISABLE_KEYWORDS)
    )


def should_group_mute_wake_for_message(message: NormalizedMessage, qq_config: QQConfig) -> bool:
    return (
        message.is_at_self
        and is_group_mute_controller(message.user_id, qq_config)
        and not is_group_mute_enable_command(message, qq_config)
    )


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    compact = "".join(text.split()).lower()
    return any(keyword.lower() in compact for keyword in keywords)
