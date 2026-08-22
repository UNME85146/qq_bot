from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from app.models import GroupMessageIndex, NormalizedMessage, StickerAssetAnalysis
from app.safety.safety_service import SafetyService
from app.storage.repositories import (
    ConversationRepository,
    GroupMessageIndexRepository,
    GroupSemanticTermRepository,
    StickerAssetAnalysisRepository,
)


@dataclass(frozen=True)
class ModelContext:
    prompt_block: str = ""
    long_term_memory: str = ""
    group_context: str = ""
    referenced_message: GroupMessageIndex | None = None
    sticker_analyses: tuple[StickerAssetAnalysis, ...] = field(default_factory=tuple)


class ModelContextService:
    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        safety_service: SafetyService,
        memory_service: object,
        group_context_service: object,
        group_semantic_term_repository: GroupSemanticTermRepository | None = None,
        group_message_index_repository: GroupMessageIndexRepository | None = None,
        sticker_analysis_repository: StickerAssetAnalysisRepository | None = None,
        group_member_profile_service: object | None = None,
        max_block_chars: int = 900,
    ) -> None:
        self._conversation_repository = conversation_repository
        self._safety_service = safety_service
        self._memory_service = memory_service
        self._group_context_service = group_context_service
        self._semantic_terms = group_semantic_term_repository
        self._group_messages = group_message_index_repository
        self._sticker_analysis = sticker_analysis_repository
        self._group_member_profiles = group_member_profile_service
        self._max_block_chars = max_block_chars

    async def build(
        self,
        message: NormalizedMessage,
        *,
        recent_context: list[dict] | None = None,
        image_intent: str = "",
    ) -> ModelContext:
        long_term_memory = await self._safe_get_memory(message.user_id)
        group_context = ""
        current_indexed_message = None
        referenced_message = None
        semantic_lines: list[str] = []
        sticker_analyses: list[StickerAssetAnalysis] = []
        member_profile_context = ""
        if message.group_id:
            (
                group_context,
                current_indexed_message,
                referenced_message,
                member_profile_context,
            ) = await asyncio.gather(
                self._safe_get_group_context(message.group_id),
                self._get_current_indexed_message(message),
                self._get_referenced_message(message),
                self._get_group_member_profile_context(
                    message.group_id,
                    message.user_id,
                ),
            )
            semantic_lines, sticker_analyses = await asyncio.gather(
                self._matched_semantic_lines(message, referenced_message),
                self._get_sticker_analyses(
                    current_indexed_message,
                    referenced_message,
                ),
            )

        intent = _restate_current_message(message, referenced_message, image_intent)
        parts = ["当前语境增强：", f"- 当前消息重述：{intent}"]
        if long_term_memory.strip():
            parts.append("- 相关长期记忆：\n" + _shorten(long_term_memory.strip(), 260))
        if group_context.strip():
            parts.append("- 群上下文摘要：\n" + _shorten(group_context.strip(), 220))
        if member_profile_context.strip():
            parts.append("- 当前成员低敏画像：\n" + _shorten(member_profile_context.strip(), 180))
        if referenced_message is not None:
            ref = _sanitize_prompt_text(referenced_message.text)
            if ref:
                ref_name = referenced_message.user_name or "某成员"
                parts.append(
                    "- 引用消息说明："
                    f"用户正在引用 {ref_name} 的消息：{_shorten(ref, 140)}"
                )
            elif referenced_message.sticker_asset_id:
                parts.append("- 引用消息说明：用户正在引用一条表情包/图片消息。")
        if semantic_lines:
            parts.append("- 群内语义解释：\n" + "\n".join(semantic_lines[:8]))
        analysis_lines = _analysis_lines(sticker_analyses, image_intent)
        if analysis_lines:
            parts.append("- 图片/表情包意图：\n" + "\n".join(analysis_lines))
        parts.append(_reply_mode_hint(message))
        block = "\n".join(parts)
        return ModelContext(
            prompt_block=_shorten(block, self._max_block_chars),
            long_term_memory=long_term_memory,
            group_context=group_context,
            referenced_message=referenced_message,
            sticker_analyses=tuple(sticker_analyses),
        )

    async def remember_group_terms(self, message: NormalizedMessage) -> None:
        if self._semantic_terms is None or not message.group_id:
            return
        if not self._safety_service.can_store_long_term_memory(message.text):
            return
        candidates = _extract_group_term_candidates(message)
        for term, description, confidence in candidates:
            if not _is_safe_term(term, self._safety_service):
                continue
            if not self._safety_service.can_store_long_term_memory(description):
                continue
            await self._semantic_terms.upsert(
                group_id=message.group_id,
                term=term,
                description=description,
                source="rule",
                confidence=confidence,
            )

    async def _safe_get_memory(self, user_id: str) -> str:
        try:
            return await self._memory_service.get_prompt_memory(user_id)
        except Exception:
            return ""

    async def _safe_get_group_context(self, group_id: str) -> str:
        try:
            return await self._group_context_service.get_prompt_context(group_id)
        except Exception:
            return ""

    async def _get_referenced_message(
        self,
        message: NormalizedMessage,
    ) -> GroupMessageIndex | None:
        if self._group_messages is None or not message.group_id or not message.reply_to_message_id:
            return None
        try:
            return await self._group_messages.get(message.group_id, message.reply_to_message_id)
        except Exception:
            return None

    async def _get_current_indexed_message(
        self,
        message: NormalizedMessage,
    ) -> GroupMessageIndex | None:
        if self._group_messages is None or not message.group_id or not message.message_id:
            return None
        try:
            return await self._group_messages.get(message.group_id, message.message_id)
        except Exception:
            return None

    async def _matched_semantic_lines(
        self,
        message: NormalizedMessage,
        referenced_message: GroupMessageIndex | None,
    ) -> list[str]:
        if self._semantic_terms is None or not message.group_id:
            return []
        text = " ".join(
            item
            for item in (
                message.text,
                message.user_name,
                referenced_message.text if referenced_message else "",
                referenced_message.user_name if referenced_message else "",
            )
            if item
        )
        if not text.strip():
            return []
        try:
            terms = await self._semantic_terms.find_relevant(group_id=message.group_id, text=text)
        except Exception:
            return []
        lines = []
        for term in terms:
            safe_desc = _sanitize_prompt_text(term.description)
            if safe_desc:
                lines.append(f"{term.term}：{_shorten(safe_desc, 90)}")
        return lines

    async def _get_sticker_analyses(
        self,
        current_message: GroupMessageIndex | None,
        referenced_message: GroupMessageIndex | None,
    ) -> list[StickerAssetAnalysis]:
        if self._sticker_analysis is None:
            return []
        asset_ids = []
        if current_message and current_message.sticker_asset_id:
            asset_ids.append(current_message.sticker_asset_id)
        if referenced_message and referenced_message.sticker_asset_id:
            asset_ids.append(referenced_message.sticker_asset_id)
        unique_asset_ids = list(dict.fromkeys(asset_ids))
        analyses: list[StickerAssetAnalysis] = []
        for asset_id in unique_asset_ids[:3]:
            try:
                analysis = await self._sticker_analysis.get(asset_id)
            except Exception:
                analysis = None
            if analysis is not None and analysis.analysis_status == "completed":
                analyses.append(analysis)
        return analyses

    async def _get_group_member_profile_context(
        self,
        group_id: str,
        user_id: str,
    ) -> str:
        if self._group_member_profiles is None:
            return ""
        try:
            return await self._group_member_profiles.get_prompt_context(group_id, user_id)
        except Exception:
            return ""


def _extract_group_term_candidates(message: NormalizedMessage) -> list[tuple[str, str, float]]:
    candidates: list[tuple[str, str, float]] = []
    name = message.user_name.strip()
    if 2 <= len(name) <= 16 and name != message.user_id:
        candidates.append((name, f"该群成员的当前显示名/常用称呼是 {name}", 0.65))
    text = _sanitize_prompt_text(message.text)
    for term in _extract_short_terms(text):
        candidates.append((term, _known_term_description(term), 0.55))
    return candidates[:8]


def _extract_short_terms(text: str) -> list[str]:
    if not text:
        return []
    known = (
        "API",
        "SDK",
        "JSON",
        "HTTP",
        "WS",
        "WebSocket",
        "DeepSeek",
        "OpenAI",
        "Codex",
        "NapCat",
        "OneBot",
        "QQ",
        "AI",
    )
    matched = [term for term in known if re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text)]
    chinese_terms = re.findall(r"(?<![\d])[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_+#.-]{1,9}", text)
    ignored = {"这个", "那个", "什么", "一下", "可以", "就是", "然后", "我们", "你们", "机器人", "小黄"}
    for term in chinese_terms:
        if term not in ignored and term not in matched and len(term) <= 10:
            if any(marker in term for marker in ("接口", "模型", "表情", "复读", "部署", "配置")):
                matched.append(term)
    return matched[:6]


def _known_term_description(term: str) -> str:
    lower = term.lower()
    descriptions = {
        "api": "技术语境里通常指接口或模型调用接口",
        "sdk": "技术语境里通常指开发工具包",
        "json": "技术语境里通常指结构化数据格式",
        "http": "技术语境里通常指网络请求协议",
        "ws": "技术语境里通常指 WebSocket 连接",
        "websocket": "技术语境里通常指实时双向连接",
        "deepseek": "当前项目常用的模型供应商或模型接口",
        "openai": "模型接口或 OpenAI-compatible 协议语境",
        "codex": "代码助手或开发协作语境",
        "napcat": "QQ Bot 登录和 OneBot 连接相关组件",
        "onebot": "QQ Bot 事件/消息协议语境",
        "qq": "当前聊天平台语境",
        "ai": "模型或人工智能语境",
    }
    return descriptions.get(lower, f"群里近期出现的低敏关键词：{term}")


def _is_safe_term(term: str, safety_service: SafetyService) -> bool:
    if len(term) < 2 or len(term) > 20:
        return False
    if re.search(r"https?://|[A-Za-z0-9_-]*sk-[A-Za-z0-9_-]+", term, re.IGNORECASE):
        return False
    if re.fullmatch(r"\d{6,}", term):
        return False
    return safety_service.can_store_long_term_memory(term)


def _restate_current_message(
    message: NormalizedMessage,
    referenced_message: GroupMessageIndex | None,
    image_intent: str,
) -> str:
    text = _sanitize_prompt_text(message.text) or "[media]"
    if image_intent:
        return f"{message.user_name} 发来图片/表情包并表达：{text}；图片意图：{image_intent}"
    if referenced_message is not None:
        return f"{message.user_name} 正在围绕引用消息追问：{text}"
    return f"{message.user_name} 本次想表达/询问：{text}"


def _analysis_lines(
    analyses: list[StickerAssetAnalysis],
    image_intent: str,
) -> list[str]:
    lines = []
    if image_intent:
        lines.append(_shorten(_sanitize_prompt_text(image_intent), 160))
    for analysis in analyses:
        bits = [
            analysis.intent_summary,
            f"情绪={analysis.emotion_tags}" if analysis.emotion_tags else "",
            f"场景={analysis.scene_tags}" if analysis.scene_tags else "",
            analysis.reply_usage_hint,
        ]
        line = "；".join(_sanitize_prompt_text(bit) for bit in bits if bit)
        if line:
            lines.append(_shorten(line, 180))
    return lines[:4]


def _reply_mode_hint(message: NormalizedMessage) -> str:
    compact = "".join(message.text.split())
    if looks_like_long_text_request(compact):
        return (
            "- Reply mode hint: this is an explicit long-form request. "
            "Use reply_mode=long_text and satisfy the requested length; "
            "do not compress it into a short QQ reply."
        )
    if message.scope_type == "group":
        return "- 回复模式建议：群聊只回答当前被 @/引用的这一问，不主动打包回答历史无关问题。"
    return "- 回复模式建议：私聊可以完整解释，不需要项目层截短。"


def looks_like_long_text_request(text: str) -> bool:
    return _looks_like_long_text_request(text)


def _looks_like_long_text_request(text: str) -> bool:
    normalized = "".join(str(text or "").split()).lower()
    if re.search(r"\d{2,5}(?:字|words?|characters?)", normalized, re.IGNORECASE):
        return True
    markers = (
        "详细",
        "展开",
        "步骤",
        "方案",
        "代码",
        "怎么实现",
        "报错",
        "调试",
        "完整",
        "教程",
        "长文本",
        "长文",
        "作文",
        "故事",
        "讲故事",
        "笑话",
        "段子",
        "写一篇",
        "写篇",
        "续写",
        "长一点",
        "长点",
        "多写点",
        "多写一点",
        "不少于",
        "至少",
        "几百字",
        "一千字",
        "两千字",
        "800字",
        "essay",
        "story",
        "joke",
        "longform",
        "long-form",
    )
    return any(marker.lower() in normalized for marker in markers)


def _sanitize_prompt_text(text: str) -> str:
    cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", text)
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    cleaned = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[phone]", cleaned)
    cleaned = re.sub(r"\b\d{17}[\dXx]\b", "[id]", cleaned)
    cleaned = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", cleaned)
    cleaned = re.sub(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)", "[secret]", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(" ：:，,。. ")


def _shorten(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."
