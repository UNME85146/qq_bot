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
class BehaviorProfileConfig:
    reply_cadence: list[str] = field(default_factory=list)
    punctuation_profile: list[str] = field(default_factory=list)
    interaction_habits: list[str] = field(default_factory=list)
    chat_action_rules: list[str] = field(default_factory=list)


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
    character_summary: str = ""
    behavior_profile: BehaviorProfileConfig = field(default_factory=BehaviorProfileConfig)


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
class GroupMemberProfile:
    group_id: str
    user_id: str
    display_name: str | None
    summary: str
    metrics: dict[str, int]
    message_count: int
    updated_at: str | None


@dataclass(frozen=True)
class GroupNewsSubscription:
    group_id: str
    enabled: bool
    send_time: str
    timezone: str
    categories: tuple[str, ...]
    last_sent_date: str | None
    updated_by: str
    updated_at: str | None


@dataclass(frozen=True)
class GroupNewsDeliveryCheckpoint:
    group_id: str
    delivery_date: str
    messages: tuple[str, ...]
    next_message_index: int
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class StockWatchItem:
    id: int
    user_id: str
    scope_type: str
    scope_id: str
    symbol: str
    market: str
    cost_price: float | None
    quantity: float | None
    alert_threshold_percent: float
    enabled: bool
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class ConversationSession:
    session_id: str
    scope_type: str
    scope_id: str
    initiator_user_id: str
    root_message_id: str | None
    status: str
    last_activity_at: str
    expires_at: str
    close_reason: str | None
    closed_at: str | None
    created_at: str | None


@dataclass(frozen=True)
class SessionMemory:
    session_id: str
    summary: str
    keywords: tuple[str, ...]
    sample_count: int
    state: str
    updated_at: str | None


@dataclass(frozen=True)
class GroupMuteState:
    group_id: str
    muted: bool
    updated_by: str
    reason: str
    updated_at: str | None
    mode: str = "normal"


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
class StickerAssetAnalysis:
    asset_id: str
    intent_summary: str
    emotion_tags: str
    scene_tags: str
    text_tags: str
    reply_usage_hint: str
    safety_category: str
    analysis_status: str
    analyzed_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class GroupSemanticTerm:
    group_id: str
    term: str
    description: str
    source: str
    confidence: float
    updated_at: str | None


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
    long_text_max_length: int = 2200
    long_text_max_bubbles: int = 8


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
class ConversationSessionsConfig:
    inactivity_seconds: int = 900
    chat_delay_min_ms: int = 2000
    chat_delay_max_ms: int = 3000


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    timeout_multipliers: tuple[float, ...] = (1.0, 2.0, 3.0)
    backoff_seconds: tuple[float, ...] = (2.0, 5.0)


@dataclass(frozen=True)
class VideoConfig:
    enabled: bool = False
    host_cache_path: str = "runtime_artifacts/video-cache"
    container_cache_path: str = "/path/in/container/qq-bot-media"
    per_message_concurrency: int = 3
    global_concurrency: int = 6
    download_timeout_seconds: float = 300.0
    send_timeout_seconds: float = 90.0
    qq_video_max_bytes: int | None = None
    min_free_bytes: int = 0
    http_proxy_env: str = "QQ_BOT_VIDEO_HTTP_PROXY"
    socks_proxy_env: str = "QQ_BOT_VIDEO_SOCKS_PROXY"
    cookie_file_env: str = "QQ_BOT_VIDEO_COOKIE_FILE"
    progress_threshold_seconds: float = 3.0
    domain_failure_threshold: int = 2
    domain_recovery_seconds: float = 120.0
    canonical_url_cache_seconds: float = 3600.0
    backoff_jitter_seconds: float = 0.5


def _default_news_feeds() -> dict[str, tuple[str, ...]]:
    return {
        "politics": (),
        "business": (),
        "technology": (),
        "finance": (),
    }


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = False
    default_time: str = "08:00"
    timezone: str = "Asia/Shanghai"
    feeds: dict[str, tuple[str, ...]] = field(default_factory=_default_news_feeds)


@dataclass(frozen=True)
class MarketProviderConfig:
    provider: str = ""
    base_url: str = ""
    api_key_env: str = ""


@dataclass(frozen=True)
class MarketsConfig:
    enabled: bool = False
    alert_threshold_percent: float = 3.0
    poll_interval_seconds: int = 300
    command_timeout_seconds: float = 20.0
    provider_timeout_seconds: float = 8.0
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 60.0
    a_share: MarketProviderConfig = field(default_factory=MarketProviderConfig)
    a_share_fallbacks: tuple[MarketProviderConfig, ...] = field(
        default_factory=lambda: (MarketProviderConfig(provider="sina"),)
    )
    us_share: MarketProviderConfig = field(default_factory=MarketProviderConfig)


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool = False
    provider: str = ""
    base_url: str = ""
    api_key_env: str = ""


@dataclass(frozen=True)
class SpeechConfig:
    enabled: bool = False
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    voice: str = ""
    format: str = "mp3"
    timeout_seconds: float = 60.0
    send_timeout_seconds: float = 60.0
    cache_dir: str = "runtime_artifacts/speech"
    max_chars: int = 4096
    private_enabled: bool = True
    group_enabled: bool = True
    private_cooldown_seconds: float = 30.0
    group_cooldown_seconds: float = 60.0

@dataclass(frozen=True)
class ImageGenerationConfig:
    enabled: bool = False
    base_url: str = ""
    api_key_env: str = ""
    generation_endpoint: str = "/images/generations"
    edit_endpoint: str = "/images/edits"
    model: str = ""
    timeout_seconds: float = 120.0
    send_timeout_seconds: float = 60.0
    cache_dir: str = "runtime_artifacts/image-generation"
    edit_window_seconds: int = 180


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
    conversation_sessions: ConversationSessionsConfig = field(
        default_factory=ConversationSessionsConfig
    )
    retry: RetryConfig = field(default_factory=RetryConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    image_generation: ImageGenerationConfig = field(default_factory=ImageGenerationConfig)


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
    group_role: str = "unknown"
    session_id: str | None = None


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
    reply_mode: str = "short"
    send_sticker: bool = False
    sticker_intent: str = ""
