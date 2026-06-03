from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QQConfig:
    self_id: str
    owner_user_ids: set[str]
    allowed_private_user_ids: set[str]
    allowed_group_ids: set[str]
    memory_allowed_user_ids: set[str]
    nicknames: set[str]
    group_mute_controller_user_ids: set[str] = field(default_factory=set)
    root_user_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class OneBotConfig:
    mode: str
    access_token_env: str
    host: str
    port: int
    api_root: str


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    api_key_env: str
    base_url: str
    name: str
    timeout_seconds: float
    temperature: float
    max_tokens: int
    api_key: str | None
    use_mock: bool


@dataclass(frozen=True)
class PersonaConfig:
    mode: str
    profile_path: str
    fallback_profile_path: str
    style_profile: "StyleProfileConfig"


@dataclass(frozen=True)
class StyleProfileConfig:
    source_user_id: str
    identity_disclosure: str
    style_summary: str
    tone_rules: list[str]
    topic_biases: list[str]
    lexicon: list[str]
    reply_rules: list[str]
    avoid_rules: list[str]
    few_shot_examples: list[str]
    updated_at: str | None


@dataclass(frozen=True)
class PersonaState:
    scope_type: str
    scope_id: str
    mood: int
    energy: int
    trust: int
    relationship_stage: str
    last_interaction_at: str | None


@dataclass(frozen=True)
class MemoryProfile:
    user_id: str
    display_name: str | None
    preferred_name: str
    summary: str
    likes: str
    dislikes: str
    important_events: str
    safety_notes: str
    updated_at: str | None


@dataclass(frozen=True)
class GroupContext:
    group_id: str
    summary: str
    topic_keywords: str
    last_message_id: str | None
    message_count: int
    updated_at: str | None


@dataclass(frozen=True)
class GroupMuteState:
    group_id: str
    muted: bool
    updated_by: str
    reason: str
    updated_at: str | None


@dataclass(frozen=True)
class GroupPendingQuestion:
    id: int
    group_id: str
    user_id: str
    user_name: str
    message_id: str
    question_text: str
    status: str
    created_at: str | None
    answered_at: str | None


@dataclass(frozen=True)
class ScheduledTask:
    id: int
    task_type: str
    scope_type: str
    scope_id: str
    user_id: str
    user_name: str | None
    message: str
    due_at: str
    status: str
    created_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class StickerAsset:
    asset_id: str
    source_scope_type: str
    source_scope_id: str
    source_user_id: str
    source_message_id: str | None
    file_path: str
    url_hash: str
    media_type: str
    source_file: str | None
    tags: str
    risk_level: str
    usage_count: int
    created_at: str | None
    last_used_at: str | None


@dataclass(frozen=True)
class GroupMessageIndex:
    group_id: str
    message_id: str
    user_id: str
    user_name: str | None
    text: str
    media_type: str
    sticker_asset_id: str | None
    is_bot: bool
    created_at: str | None


@dataclass(frozen=True)
class MediaItem:
    type: str
    url: str | None = None
    file: str | None = None
    summary: str | None = None
    sub_type: str | None = None


@dataclass(frozen=True)
class SafetyCheckResult:
    action: str
    reason: str
    replacement_text: str | None = None
    safety_level: str = "pass"


@dataclass(frozen=True)
class ReplyConfig:
    private_always_reply: bool
    group_mention_reply: bool
    nickname_reply_probability: float
    active_window_seconds: int
    min_delay_ms: int
    max_delay_ms: int
    max_reply_length: int


@dataclass(frozen=True)
class PresenceConfig:
    focus_window_seconds: int = 180
    base_online_probability: float = 0.08
    focused_repeat_probability: float = 0.35
    unfocused_repeat_probability: float = 0.05
    plus_one_repeat_probability: float = 0.45
    sticker_repeat_probability: float = 0.25
    text_repeat_probability: float = 0.08


@dataclass(frozen=True)
class LimitsConfig:
    private_cooldown_seconds: float
    group_cooldown_seconds: float
    max_user_messages_per_minute: int
    max_group_messages_per_minute: int
    model_failure_break_count: int
    model_failure_break_seconds: int


@dataclass(frozen=True)
class StorageConfig:
    database_path: str
    backup_dir: str
    save_raw_model_text: bool
    save_raw_user_message: bool


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    log_dir: str
    sanitize_message_content: bool


@dataclass(frozen=True)
class AppConfig:
    qq: QQConfig
    onebot: OneBotConfig
    model: ModelConfig
    persona: PersonaConfig
    reply: ReplyConfig
    presence: PresenceConfig
    limits: LimitsConfig
    storage: StorageConfig
    logging: LoggingConfig


@dataclass(frozen=True)
class NormalizedMessage:
    trace_id: str
    self_id: str
    message_id: str
    message_type: str
    scope_type: str
    scope_id: str
    user_id: str
    group_id: str | None
    user_name: str
    raw_message: str
    text: str
    is_at_self: bool
    mentioned_user_ids: list[str]
    received_at: str
    trigger_reason: str | None = None
    reply_to_message_id: str | None = None
    media_items: tuple[MediaItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReplyDecision:
    action: str
    reason: str
    should_call_model: bool
    should_write_memory: bool
    delay_ms: int = 0
    fallback_text: str | None = None


@dataclass(frozen=True)
class GeneratedReply:
    text: str
    raw_model_text: str
    model_name: str
    finish_reason: str
    safety_level: str = "pass"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
