from __future__ import annotations

import time

from app.conversation.prompt_builder import PromptBuilder
from app.conversation.reply_formatter import ReplyFormatter, parse_model_reply
from app.memory.group_context_service import NullGroupContextService
from app.memory.memory_service import NullMemoryService
from app.model.llm_client import LlmClient
from app.model.resilience import ModelCallResult, ModelResilienceService, classify_model_exception
from app.model.vision_service import (
    ImageAnalysisResult,
    ImageUnderstandingService,
    first_image_with_url,
    has_media,
)
from app.models import GeneratedReply, NormalizedMessage, StorageConfig
from app.persona.persona_state_service import PersonaStateService
from app.routing.permission_service import PermissionService
from app.safety.safety_service import SafetyService
from app.storage.repositories import AuditRepository, ConversationRepository


class ConversationService:
    def __init__(
        self,
        *,
        permission_service: PermissionService,
        prompt_builder: PromptBuilder,
        model_client: LlmClient,
        conversation_repository: ConversationRepository,
        persona_state_service: PersonaStateService,
        reply_formatter: ReplyFormatter,
        storage_config: StorageConfig,
        safety_service: SafetyService | None = None,
        audit_repository: AuditRepository | None = None,
        memory_service: object | None = None,
        group_context_service: object | None = None,
        model_resilience_service: ModelResilienceService | None = None,
        image_understanding_service: ImageUnderstandingService | None = None,
        model_context_service: object | None = None,
    ) -> None:
        self._permission_service = permission_service
        self._prompt_builder = prompt_builder
        self._model_client = model_client
        self._conversation_repository = conversation_repository
        self._persona_state_service = persona_state_service
        self._reply_formatter = reply_formatter
        self._storage_config = storage_config
        self._safety_service = safety_service or SafetyService()
        self._audit_repository = audit_repository
        self._memory_service = memory_service or NullMemoryService()
        self._group_context_service = group_context_service or NullGroupContextService()
        self._model_resilience_service = model_resilience_service
        self._image_understanding_service = image_understanding_service
        self._model_context_service = model_context_service

    @property
    def model_client(self) -> LlmClient:
        return self._model_client

    async def handle_private_message(
        self,
        message: NormalizedMessage,
    ) -> GeneratedReply | None:
        started_at = time.monotonic()
        if not self._permission_service.is_private_user_allowed(message.user_id):
            await self._audit(
                message,
                action="silence",
                reason="private_not_allowed",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None

        input_safety = self._safety_service.check_input(message.text, scope_type=message.scope_type)
        if input_safety.action in {"block", "rewrite"} and input_safety.replacement_text is not None:
            reply = GeneratedReply(
                text=self._reply_formatter.format_unlimited(input_safety.replacement_text),
                raw_model_text=input_safety.replacement_text,
                model_name="safety",
                finish_reason=input_safety.reason,
                safety_level=input_safety.safety_level,
            )
            await self._save_user_message(message)
            await self._save_assistant_message(message, reply)
            await self._audit(
                message,
                action="refuse" if input_safety.action == "block" else "reply",
                reason=input_safety.reason,
                model_called=False,
                safety_blocked=input_safety.action == "block",
                started_at=started_at,
            )
            return reply

        if has_media(message.media_items):
            return await self.handle_private_image_message(message, started_at=started_at)

        recent_context, persona_state, model_context = await self._prepare_model_context(message)
        prompt = self._prompt_builder.build_private_prompt(
            user_name=message.user_name,
            user_text=message.text,
            recent_context=recent_context,
            persona_state=persona_state,
            long_term_memory=model_context.long_term_memory,
            model_context=model_context.prompt_block,
        )

        await self._save_user_message(message)
        model_result = await self._generate_reply(prompt, message)
        reply = model_result.reply

        reply, output_safety = self._parse_and_format_reply(reply, message, unlimited=True)
        await self._save_assistant_message(message, reply)
        await self._persona_state_service.record_successful_reply(
            message.scope_type,
            message.scope_id,
        )
        await self._memory_service.record_user_message(
            user_id=message.user_id,
            user_name=message.user_name,
            text=message.text,
        )
        await self._audit(
            message,
            action="reply",
            reason=_reply_reason(
                output_safety.action,
                output_safety.reason,
                model_result.failure_reason,
                "private_allowed",
            ),
            model_called=model_result.model_called,
            safety_blocked=output_safety.action == "block",
            started_at=started_at,
        )
        return reply

    async def handle_group_message(
        self,
        message: NormalizedMessage,
    ) -> GeneratedReply | None:
        started_at = time.monotonic()
        if message.group_id is None:
            await self._audit(
                message,
                action="silence",
                reason="missing_group_id",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None
        if not self._permission_service.is_group_allowed(message.group_id):
            await self._audit(
                message,
                action="silence",
                reason="group_not_allowed",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None
        await self._group_context_service.record_group_message(
            group_id=message.group_id,
            message_id=message.message_id,
            text=message.text,
        )
        await self._remember_group_terms(message)
        if not message.is_at_self and message.trigger_reason not in {
            "nickname_trigger",
            "active_window",
        }:
            await self._audit(
                message,
                action="silence",
                reason="nickname_probability_skipped"
                if message.trigger_reason == "nickname_probability_skipped"
                else "group_not_triggered",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None

        input_safety = self._safety_service.check_input(message.text, scope_type=message.scope_type)
        if input_safety.action == "block" and message.scope_type == "group":
            await self._save_user_message(message)
            await self._audit(
                message,
                action="silence",
                reason="group_high_risk_silence",
                model_called=False,
                safety_blocked=True,
                started_at=started_at,
            )
            return None
        if input_safety.action in {"block", "rewrite"} and input_safety.replacement_text is not None:
            reply = GeneratedReply(
                text=self._reply_formatter.format(input_safety.replacement_text),
                raw_model_text=input_safety.replacement_text,
                model_name="safety",
                finish_reason=input_safety.reason,
                safety_level=input_safety.safety_level,
            )
            await self._save_user_message(message)
            await self._save_assistant_message(message, reply)
            await self._audit(
                message,
                action="refuse" if input_safety.action == "block" else "reply",
                reason=input_safety.reason,
                model_called=False,
                safety_blocked=input_safety.action == "block",
                started_at=started_at,
            )
            return reply

        recent_context, persona_state, model_context = await self._prepare_model_context(message)
        prompt = self._prompt_builder.build_group_prompt(
            user_name=message.user_name,
            user_text=message.text,
            recent_context=recent_context,
            persona_state=persona_state,
            long_term_memory=model_context.long_term_memory,
            group_context=model_context.group_context,
            model_context=model_context.prompt_block,
        )

        await self._save_user_message(message)
        model_result = await self._generate_reply(prompt, message)
        reply = model_result.reply

        reply, output_safety = self._parse_and_format_reply(reply, message, unlimited=False)
        await self._save_assistant_message(message, reply)
        await self._persona_state_service.record_successful_reply(
            message.scope_type,
            message.scope_id,
        )
        await self._memory_service.record_user_message(
            user_id=message.user_id,
            user_name=message.user_name,
            text=message.text,
        )
        await self._audit(
            message,
            action="reply",
            reason=_reply_reason(
                output_safety.action,
                output_safety.reason,
                model_result.failure_reason,
                message.trigger_reason or "group_mention",
            ),
            model_called=model_result.model_called,
            safety_blocked=output_safety.action == "block",
            started_at=started_at,
        )
        return reply

    async def handle_private_image_message(
        self,
        message: NormalizedMessage,
        *,
        started_at: float | None = None,
    ) -> GeneratedReply | None:
        started_at = started_at if started_at is not None else time.monotonic()
        if not self._permission_service.is_private_user_allowed(message.user_id):
            await self._audit(
                message,
                action="silence",
                reason="private_not_allowed",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None

        recent_context, persona_state, model_context = await self._prepare_model_context(message)
        style_system_prompt = self._prompt_builder.build_private_prompt(
            user_name=message.user_name,
            user_text=message.text,
            recent_context=recent_context,
            persona_state=persona_state,
            long_term_memory=model_context.long_term_memory,
            model_context=model_context.prompt_block,
        )[0]["content"]

        await self._save_user_message(message)
        analysis = await self._analyze_image_message(
            message,
            style_system_prompt=style_system_prompt,
            started_at=started_at,
        )
        if analysis is None:
            return await self._image_unavailable_reply(
                message,
                reason="image_url_missing",
                model_called=False,
                started_at=started_at,
            )
        if analysis.action == "refuse":
            reply = GeneratedReply(
                text=self._reply_formatter.format_unlimited("这个图不太适合展开聊，换个安全点的吧。"),
                raw_model_text="这个图不太适合展开聊，换个安全点的吧。",
                model_name="vision",
                finish_reason=analysis.category,
            )
            await self._save_assistant_message(message, reply)
            await self._audit(
                message,
                action="refuse",
                reason="private_image_high_risk_refuse",
                model_called=analysis.model_called,
                safety_blocked=True,
                started_at=started_at,
            )
            return reply

        return await self._finalize_image_reply(
            message,
            analysis=analysis,
            reason="private_image",
            started_at=started_at,
            unlimited=True,
        )

    async def handle_group_image_message(
        self,
        message: NormalizedMessage,
        *,
        reason: str | None = None,
    ) -> GeneratedReply | None:
        started_at = time.monotonic()
        if message.group_id is None:
            await self._audit(
                message,
                action="silence",
                reason="missing_group_id",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None
        if not self._permission_service.is_group_allowed(message.group_id):
            await self._audit(
                message,
                action="silence",
                reason="group_not_allowed",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return None
        recent_context, persona_state, model_context = await self._prepare_model_context(message)
        style_system_prompt = self._prompt_builder.build_group_system_message(
            user_name=message.user_name,
            user_text=message.text,
            recent_context=recent_context,
            persona_state=persona_state,
            long_term_memory=model_context.long_term_memory,
            group_context=model_context.group_context,
            model_context=model_context.prompt_block,
        )
        await self._save_user_message(message)
        analysis = await self._analyze_image_message(
            message,
            style_system_prompt=style_system_prompt,
            started_at=started_at,
        )
        if analysis is None:
            return await self._image_unavailable_reply(
                message,
                reason="image_url_missing",
                model_called=False,
                started_at=started_at,
            )
        if analysis.action == "silence":
            await self._audit(
                message,
                action="silence",
                reason="group_image_high_risk_silence",
                model_called=analysis.model_called,
                safety_blocked=True,
                started_at=started_at,
            )
            return None

        await self._group_context_service.record_group_message(
            group_id=message.group_id,
            message_id=message.message_id,
            text=message.text,
        )
        return await self._finalize_image_reply(
            message,
            analysis=analysis,
            reason=reason or message.trigger_reason or "group_image",
            started_at=started_at,
            unlimited=False,
        )

    async def _analyze_image_message(
        self,
        message: NormalizedMessage,
        *,
        style_system_prompt: str,
        started_at: float,
    ) -> ImageAnalysisResult | None:
        if self._image_understanding_service is None or not has_media(message.media_items):
            return ImageAnalysisResult(
                action="reply",
                category="unknown",
                reply=GeneratedReply(
                    text=_image_unavailable_text(message.scope_type),
                    raw_model_text=_image_unavailable_text(message.scope_type),
                    model_name="vision",
                    finish_reason="vision_unavailable",
                ),
                model_called=False,
                failure_reason="vision_unavailable",
            )
        if first_image_with_url(message.media_items) is None:
            return None
        return await self._image_understanding_service.analyze(
            user_text=message.text,
            media_items=message.media_items,
            scope_type=message.scope_type,
            style_system_prompt=style_system_prompt,
        )

    async def _image_unavailable_reply(
        self,
        message: NormalizedMessage,
        *,
        reason: str,
        model_called: bool,
        started_at: float,
    ) -> GeneratedReply:
        text = _image_unavailable_text(message.scope_type)
        reply = GeneratedReply(
            text=text,
            raw_model_text=text,
            model_name="vision",
            finish_reason=reason,
        )
        await self._save_assistant_message(message, reply)
        await self._audit(
            message,
            action="reply",
            reason=reason,
            model_called=model_called,
            safety_blocked=False,
            started_at=started_at,
        )
        return reply

    async def _finalize_image_reply(
        self,
        message: NormalizedMessage,
        *,
        analysis: ImageAnalysisResult,
        reason: str,
        started_at: float,
        unlimited: bool,
    ) -> GeneratedReply:
        reply = analysis.reply or GeneratedReply(
            text=_image_unavailable_text(message.scope_type),
            raw_model_text=_image_unavailable_text(message.scope_type),
            model_name="vision",
            finish_reason=analysis.failure_reason or analysis.category,
        )
        reply, output_safety = self._parse_and_format_reply(
            reply,
            message,
            unlimited=unlimited,
        )
        await self._save_assistant_message(message, reply)
        await self._persona_state_service.record_successful_reply(
            message.scope_type,
            message.scope_id,
        )
        await self._audit(
            message,
            action="reply",
            reason=_reply_reason(
                output_safety.action,
            output_safety.reason,
            analysis.failure_reason,
            reason,
        )
            if analysis.failure_reason is None
            else _image_failure_reason(analysis.failure_reason),
            model_called=analysis.model_called,
            safety_blocked=output_safety.action == "block",
            started_at=started_at,
        )
        if (
            analysis.failure_reason is not None
            and _image_failure_reason(analysis.failure_reason) == "vision_generate_failed"
        ):
            await self._system_event(
                "ERROR",
                "vision_generate_failed",
                f"category={analysis.category}; failure={analysis.failure_reason}",
                message.trace_id,
            )
        return reply

    async def handle_group_question(
        self,
        message: NormalizedMessage,
        *,
        question_text: str,
        question_user_name: str | None = None,
        reason: str | None = None,
    ) -> GeneratedReply | None:
        question_message = message
        if question_text != message.text or question_user_name:
            from dataclasses import replace

            question_message = replace(
                message,
                text=question_text,
                user_name=question_user_name or message.user_name,
                trigger_reason=reason or message.trigger_reason,
            )
        return await self.handle_group_message(question_message)

    async def record_silent_group_message(
        self,
        message: NormalizedMessage,
        *,
        reason: str,
        safety_blocked: bool = False,
    ) -> None:
        started_at = time.monotonic()
        if message.group_id is None:
            await self._audit(
                message,
                action="silence",
                reason="missing_group_id",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return
        if not self._permission_service.is_group_allowed(message.group_id):
            await self._audit(
                message,
                action="silence",
                reason="group_not_allowed",
                model_called=False,
                safety_blocked=False,
                started_at=started_at,
            )
            return
        await self._save_user_message(message)
        await self._group_context_service.record_group_message(
            group_id=message.group_id,
            message_id=message.message_id,
            text=message.text,
        )
        await self._remember_group_terms(message)
        await self._audit(
            message,
            action="silence",
            reason=reason,
            model_called=False,
            safety_blocked=safety_blocked,
            started_at=started_at,
        )

    async def _prepare_model_context(self, message: NormalizedMessage):
        recent_context = await self._conversation_repository.get_recent_conversations(
            message.scope_type,
            message.scope_id,
            limit=20,
        )
        persona_state = await self._persona_state_service.get_or_create(
            message.scope_type,
            message.scope_id,
        )
        if self._model_context_service is not None:
            try:
                model_context = await self._model_context_service.build(
                    message,
                    recent_context=recent_context,
                )
                return recent_context, persona_state, model_context
            except Exception as exc:
                await self._system_event(
                    "ERROR",
                    "model_context_build_failed",
                    f"{type(exc).__name__}: {str(exc)[:120]}",
                    message.trace_id,
                )
        long_term_memory = await self._memory_service.get_prompt_memory(message.user_id)
        group_context = (
            await self._group_context_service.get_prompt_context(message.group_id)
            if message.group_id
            else ""
        )
        from app.conversation.model_context_service import ModelContext

        return (
            recent_context,
            persona_state,
            ModelContext(long_term_memory=long_term_memory, group_context=group_context),
        )

    async def _remember_group_terms(self, message: NormalizedMessage) -> None:
        if self._model_context_service is None:
            return
        try:
            await self._model_context_service.remember_group_terms(message)
        except Exception as exc:
            await self._system_event(
                "ERROR",
                "group_semantic_terms_update_failed",
                f"{type(exc).__name__}: {str(exc)[:120]}",
                message.trace_id,
            )

    def _parse_and_format_reply(
        self,
        reply: GeneratedReply,
        message: NormalizedMessage,
        *,
        unlimited: bool,
    ) -> tuple[GeneratedReply, object]:
        parsed = parse_model_reply(reply.text)
        output_safety = self._safety_service.check_output(
            parsed.text,
            scope_type=message.scope_type,
        )
        reply_text = output_safety.replacement_text if output_safety.replacement_text else parsed.text
        formatted_text = (
            self._reply_formatter.format_unlimited(reply_text)
            if unlimited
            else self._reply_formatter.format(reply_text)
        )
        if parsed.reply_mode in {"long_text", "code_block"} and not unlimited:
            formatted_text = self._reply_formatter.format_unlimited(reply_text)
        return (
            GeneratedReply(
                text=formatted_text,
                raw_model_text=reply.raw_model_text,
                model_name=reply.model_name,
                finish_reason=reply.finish_reason,
                safety_level=output_safety.safety_level
                if output_safety.action != "allow"
                else reply.safety_level,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                reply_mode=parsed.reply_mode,
                send_sticker=parsed.send_sticker,
                sticker_intent=parsed.sticker_intent,
            ),
            output_safety,
        )

    async def _generate_reply(
        self,
        prompt: list[dict],
        message: NormalizedMessage,
    ):
        if self._model_resilience_service is not None:
            result = await self._model_resilience_service.generate(
                prompt,
                scope_type=message.scope_type,
            )
            for detail in result.system_events:
                await self._system_event(
                    "ERROR" if "model_failure" in detail else "INFO",
                    "model_generate_failed"
                    if "model_failure" in detail
                    else "model_breaker_open",
                    detail,
                    message.trace_id,
                )
            return result

        try:
            reply = await self._model_client.generate(prompt)
        except Exception as exc:
            failure = classify_model_exception(exc)
            fallback = "卡了，等下再说。" if message.scope_type == "group" else "我刚刚有点卡，等下再说这个。"
            reply = GeneratedReply(
                text=fallback,
                raw_model_text=fallback,
                model_name="fallback",
                finish_reason=failure.category,
            )
            await self._system_event(
                "ERROR",
                "model_generate_failed",
                f"model_failure category={failure.category}; detail={failure.detail}",
                message.trace_id,
            )

            return ModelCallResult(
                reply=reply,
                model_called=True,
                failure_reason=failure.category,
            )

        return ModelCallResult(reply=reply, model_called=True)

    async def record_system_event(
        self,
        *,
        level: str,
        event: str,
        detail: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        await self._system_event(level, event, detail, trace_id)

    async def record_reply_audit(
        self,
        message: NormalizedMessage,
        *,
        action: str,
        reason: str,
        model_called: bool,
        safety_blocked: bool,
        elapsed_ms: int | None = None,
    ) -> None:
        if self._audit_repository is None:
            return
        await self._audit_repository.save_reply_audit(
            trace_id=message.trace_id,
            scope_type=message.scope_type,
            scope_id=message.scope_id,
            user_id=message.user_id,
            action=action,
            reason=reason,
            model_called=model_called,
            safety_blocked=safety_blocked,
            elapsed_ms=elapsed_ms,
        )

    async def _save_user_message(self, message: NormalizedMessage) -> None:
        if self._storage_config.save_raw_user_message:
            user_content = message.raw_message if message.media_items else message.text
        else:
            user_content = "[hidden]"
        await self._conversation_repository.save_conversation(
            trace_id=message.trace_id,
            scope_type=message.scope_type,
            scope_id=message.scope_id,
            user_id=message.user_id,
            user_name=message.user_name,
            role="user",
            content=user_content,
            message_id=message.message_id,
        )

    async def _save_assistant_message(
        self,
        message: NormalizedMessage,
        reply: GeneratedReply,
    ) -> None:
        raw_model_text = reply.raw_model_text if self._storage_config.save_raw_model_text else reply.text
        await self._conversation_repository.save_conversation(
            trace_id=message.trace_id,
            scope_type=message.scope_type,
            scope_id=message.scope_id,
            user_id=message.self_id,
            user_name=None,
            role="assistant",
            content=raw_model_text,
            message_id=None,
        )

    async def _audit(
        self,
        message: NormalizedMessage,
        *,
        action: str,
        reason: str,
        model_called: bool,
        safety_blocked: bool,
        started_at: float,
    ) -> None:
        if self._audit_repository is None:
            return
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        await self.record_reply_audit(
            message,
            action=action,
            reason=reason,
            model_called=model_called,
            safety_blocked=safety_blocked,
            elapsed_ms=elapsed_ms,
        )

    async def _system_event(
        self,
        level: str,
        event: str,
        detail: str | None,
        trace_id: str | None,
    ) -> None:
        if self._audit_repository is None:
            return
        await self._audit_repository.save_system_event(
            level=level,
            event=event,
            detail=detail,
            trace_id=trace_id,
        )


def _reply_reason(
    safety_action: str,
    safety_reason: str,
    model_failure_reason: str | None,
    default_reason: str,
) -> str:
    if safety_action != "allow":
        return safety_reason
    if model_failure_reason is not None:
        return model_failure_reason
    return default_reason


def _image_unavailable_text(scope_type: str) -> str:
    if scope_type == "group":
        return "这图我现在看不了"
    return "这图我现在看不了，换张或者直接说内容吧。"


def _image_failure_reason(failure_reason: str) -> str:
    if failure_reason == "vision_unavailable":
        return "vision_unavailable"
    return "vision_generate_failed"
