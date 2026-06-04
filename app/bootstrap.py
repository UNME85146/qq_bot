from __future__ import annotations

from app.conversation.conversation_service import ConversationService
from app.conversation.model_context_service import ModelContextService
from app.conversation.prompt_builder import PromptBuilder
from app.conversation.reply_formatter import ReplyFormatter
from app.memory.group_context_service import GroupContextService
from app.memory.memory_service import MemoryService
from app.model.llm_client import create_model_client
from app.model.resilience import ModelResilienceService
from app.model.vision_service import ImageUnderstandingService
from app.models import AppConfig
from app.persona.persona_state_service import PersonaStateService
from app.routing.permission_service import PermissionService
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    AuditRepository,
    ConversationRepository,
    GroupMessageIndexRepository,
    GroupContextRepository,
    GroupSemanticTermRepository,
    MemoryProfileRepository,
    PersonaStateRepository,
    StickerAssetAnalysisRepository,
)


def create_conversation_service(config: AppConfig) -> ConversationService:
    safety_service = SafetyService(
        identity_disclosure=config.persona.style_profile.identity_disclosure,
        source_user_id=config.persona.style_profile.source_user_id,
    )
    model_client = create_model_client(config.model)
    audit_repository = AuditRepository(config.storage.database_path)
    model_resilience_service = ModelResilienceService(
        model_client=model_client,
        limits=config.limits,
    )
    conversation_repository = ConversationRepository(config.storage.database_path)
    memory_service = MemoryService(
        repository=MemoryProfileRepository(config.storage.database_path),
        qq_config=config.qq,
        safety_service=safety_service,
        audit_repository=audit_repository,
    )
    group_context_service = GroupContextService(
        repository=GroupContextRepository(config.storage.database_path),
        qq_config=config.qq,
        safety_service=safety_service,
    )
    model_context_service = ModelContextService(
        conversation_repository=conversation_repository,
        safety_service=safety_service,
        memory_service=memory_service,
        group_context_service=group_context_service,
        group_semantic_term_repository=GroupSemanticTermRepository(
            config.storage.database_path
        ),
        group_message_index_repository=GroupMessageIndexRepository(
            config.storage.database_path
        ),
        sticker_analysis_repository=StickerAssetAnalysisRepository(
            config.storage.database_path
        ),
    )
    return ConversationService(
        permission_service=PermissionService(config.qq),
        prompt_builder=PromptBuilder(config.persona, config.tts),
        model_client=model_client,
        conversation_repository=conversation_repository,
        persona_state_service=PersonaStateService(
            PersonaStateRepository(config.storage.database_path)
        ),
        reply_formatter=ReplyFormatter(config.reply.max_reply_length),
        storage_config=config.storage,
        safety_service=safety_service,
        audit_repository=audit_repository,
        memory_service=memory_service,
        group_context_service=group_context_service,
        model_resilience_service=model_resilience_service,
        image_understanding_service=ImageUnderstandingService(
            model_resilience_service=model_resilience_service,
        ),
        model_context_service=model_context_service,
    )
