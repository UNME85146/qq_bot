from __future__ import annotations

from dataclasses import dataclass

from app.features.sticker_service import is_sticker_request
from app.features.tts_service import (
    extract_explicit_voice_read_text,
    is_explicit_voice_reply_request,
)
from app.models import NormalizedMessage


@dataclass(frozen=True)
class DirectReplyIntent:
    sticker_request: bool = False
    sticker_battle_request: bool = False
    voice_read_text: str | None = None
    voice_reply_requested: bool = False


def parse_direct_reply_intent(
    message: NormalizedMessage,
    *,
    allow_group_without_at: bool = False,
) -> DirectReplyIntent:
    """Parse pre-model reply intents that should consume the message directly."""
    allow_short_sticker = message.scope_type == "group" and message.is_at_self
    allow_sticker = message.scope_type == "private" or message.is_at_self
    voice_read_text = extract_explicit_voice_read_text(
        message,
        allow_group_without_at=allow_group_without_at,
    )
    return DirectReplyIntent(
        sticker_request=allow_sticker
        and is_sticker_request(message.text, allow_short=allow_short_sticker),
        sticker_battle_request=allow_sticker
        and _is_sticker_battle_request(message.text),
        voice_read_text=voice_read_text,
        voice_reply_requested=voice_read_text is None
        and is_explicit_voice_reply_request(
            message,
            allow_group_without_at=allow_group_without_at,
        ),
    )

def _is_sticker_battle_request(text: str) -> bool:
    compact = "".join(str(text or "").split()).lower()
    if not compact:
        return False
    return any(
        marker in compact
        for marker in (
            "斗图",
            "接一下",
            "接个图",
            "接张图",
            "用表情包回",
            "表情包回",
            "回个表情",
            "回张图",
            "拿图回",
            "拿表情回",
        )
    )
