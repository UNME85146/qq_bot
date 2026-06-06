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
    voice_read_text: str | None = None
    voice_reply_requested: bool = False


def parse_direct_reply_intent(message: NormalizedMessage) -> DirectReplyIntent:
    """Parse pre-model reply intents that should consume the message directly."""
    allow_short_sticker = message.scope_type == "group" and message.is_at_self
    allow_sticker = message.scope_type == "private" or message.is_at_self
    voice_read_text = extract_explicit_voice_read_text(message)
    return DirectReplyIntent(
        sticker_request=allow_sticker
        and is_sticker_request(message.text, allow_short=allow_short_sticker),
        voice_read_text=voice_read_text,
        voice_reply_requested=voice_read_text is None
        and is_explicit_voice_reply_request(message),
    )
