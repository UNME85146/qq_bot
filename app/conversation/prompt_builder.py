from __future__ import annotations

from typing import Any

from app.models import PersonaConfig, PersonaState, SpeechConfig


class PromptBuilder:
    def __init__(self, persona: PersonaConfig, tts: SpeechConfig | None = None) -> None:
        self._persona = persona
        self._tts = tts

    def build_private_prompt(
        self,
        user_name: str,
        user_text: str,
        recent_context: list[dict[str, Any]],
        persona_state: PersonaState,
        long_term_memory: str = "",
        model_context: str = "",
        voice_scope_type: str | None = "private",
    ) -> list[dict[str, Any]]:
        profile = self._persona.style_profile
        if self._persona.mode == "history_derived_character":
            persona_parts = [
                "你是一个有自己说话方式的 QQ 聊天机器人，不扮演预设人物，也不冒充真人。",
                "你的对话角色来自历史低敏聊天记录的聚合统计；它属于你自己，不属于任何历史发言者。",
                f"自己的对话角色：{profile.character_summary}",
                f"历史统计风格摘要：{profile.style_summary}",
                "历史低敏聊天记录只用于提炼整体表达习惯，不是词库、剧本或固定台词。",
                "不要模仿、复述或冒充历史记录中的任何人，也不要提来源账号编号。",
                "优先根据当前语境自然回答；不要为了表现角色而硬塞口头禅、数字梗、技术词或示例句。",
                "只有语境自然匹配时，才体现画像里的短句节奏、标点和互动习惯。",
            ]
        else:
            persona_parts = [
                "你是 QQ 聊天机器人，不扮演预设人物，也不冒充真人。",
                "当前未加载历史提炼的对话角色；不要声称自己的表达来自历史聊天统计。",
                f"兼容对话配置摘要：{profile.style_summary}",
            ]

        system_parts = persona_parts + [
            "长期记忆、群名片、群内语义词和旧梗只作背景；当前问题无关时不要主动带入。",
            "普通寒暄要像正常 QQ 对话，不要把称呼、问候和无关动词硬拼在一起。",
            "如果用户要表情包、图片、语音、读一句或念一句，前置逻辑会处理；你不要用文字假装发送媒体。",
            "不要输出“（发送一个表情包）”“正在语音回复中”“念给你听”“读完了”这类动作说明。",
            "除非用户明确追问实现细节，不要主动提模型、prompt、本地加载、硬件或 TTS 机制。",
            "遇到代码、调试、技术步骤请求时，优先正确清晰；不要夹带无关玩梗或旧聊天梗。",
            _format_section("语气规则", profile.tone_rules),
            _format_section("回复规则", profile.reply_rules),
            _format_section("禁止事项", profile.avoid_rules),
            "回复要像 QQ 好友即时聊天，大多数回复 1-2 句，短一点，自然一点。",
            (
                "Length override: default to short QQ replies, but when the user explicitly asks "
                "for an essay, story, joke, long text, more detail, continuation, or a concrete "
                "word/character count, ignore the 1-2 sentence habit and write enough content to "
                "match the requested length. For those requests you may return reply_mode=long_text."
            ),
            (
                "Long-form formatting: do not use Markdown headings, standalone bold titles, or "
                "separate outline-title lines in essays, stories, jokes, or detailed replies. Write "
                "natural QQ paragraphs. If you use labels such as 一、二、 or 1., keep each label "
                "in the same paragraph as its content."
            ),
            "非技术闲聊不要总结用户问题，不要复述“你是在问/你想让我”，直接接话。",
            "不要主动列表化回答，除非用户明确要求。",
            "如需代码块，保持完整 Markdown 代码块；不要输出孤立的 ```；代码块开头不要写 cpp/python 等语言名。",
            "如果用 JSON 包装回复，只使用 reply_text/text/content、reply_mode、send_sticker、sticker_intent 这些字段。",
            _format_behavior_profile(profile.behavior_profile),
            "当前角色状态："
            f"mood={persona_state.mood}, "
            f"energy={persona_state.energy}, "
            f"trust={persona_state.trust}, "
            f"relationship_stage={persona_state.relationship_stage}。",
        ]
        voice_instruction = (
            self._voice_reply_instruction(voice_scope_type)
            if voice_scope_type is not None
            else None
        )
        if voice_instruction is not None:
            system_parts.append(voice_instruction)
        if long_term_memory.strip():
            system_parts.append(f"可用的受控长期记忆：\n{long_term_memory.strip()}")
        if model_context.strip():
            system_parts.append(model_context.strip())
        system_message = "\n".join(system_parts)

        messages = [{"role": "system", "content": system_message}]
        for row in recent_context[-8:]:
            role = row.get("role", "user")
            if role not in {"user", "assistant"}:
                continue
            content = row.get("content", "").strip()
            if content:
                content = _format_history_content(row, content)
                messages.append({"role": role, "content": content})

        messages.append(
            {
                "role": "user",
                "content": (
                    f"当前用户：{user_name}\n"
                    f"用户本次消息：{user_text}\n"
                    "请直接给出 QQ 风格回复。默认短回复；如果本次消息明确要求作文、故事、笑话、长一点、详细展开或指定字数，请按要求生成长文本。"
                ),
            }
        )
        return messages

    def build_group_prompt(
        self,
        user_name: str,
        user_text: str,
        recent_context: list[dict[str, Any]],
        persona_state: PersonaState,
        long_term_memory: str = "",
        group_context: str = "",
        model_context: str = "",
    ) -> list[dict[str, Any]]:
        messages = self.build_private_prompt(
            user_name=user_name,
            user_text=user_text,
            recent_context=recent_context,
            persona_state=persona_state,
            long_term_memory=long_term_memory,
            model_context=model_context,
            voice_scope_type=None,
        )
        voice_instruction = self._voice_reply_instruction("group")
        if voice_instruction is not None and voice_instruction not in messages[0]["content"]:
            messages[0]["content"] += f"\n{voice_instruction}"
        messages[0]["content"] += (
            "\n当前场景：群聊。你是在被 @ 后回复，回复要短，不要抢话，"
            "不要把群聊当成客服工单。"
        )
        messages[0]["content"] += (
            "\n群聊回复像群友插一句：能 3-12 个字解决就不要扩写；"
            "被骂或被吐槽时可以轻轻接梗，但不要认领侮辱性身份。"
        )
        messages[0]["content"] += (
            "\nGroup long-form exception: keep normal group chat short, but if the current @/quoted "
            "message explicitly asks for an essay, story, joke, long text, more detail, continuation, "
            "or a concrete word/character count, answer that request in reply_mode=long_text."
        )
        messages[0]["content"] += (
            "\nGroup rule: answer only the current pending question in this model call. "
            "Do not answer several unrelated questions in one reply. "
            "Do not automatically answer earlier pending questions unless the current user explicitly asks to fill in earlier questions. "
            "If the user quoted a previous bot reply and mentioned the bot, treat it as a follow-up."
        )
        if group_context.strip():
            messages[0]["content"] += (
                "\n可用的群聊低敏上下文摘要：\n"
                f"{group_context.strip()}\n"
                "只把它当作当前群话题背景，不要复述原文或暴露成员信息。"
            )
        return messages

    def _voice_reply_instruction(self, scope_type: str) -> str | None:
        if self._tts is None or not self._tts.enabled:
            return None
        if scope_type == "private" and not self._tts.private_enabled:
            return None
        if scope_type == "group" and not self._tts.group_enabled:
            return None
        return (
            "当前会话已开启远程语音生成：你只负责生成正常聊天回复文本，"
            "系统会把最终回复文本交给 /v1/audio/speech 兼容接口朗读。"
            "用户要求语音、念一下、读出来时，直接给出要表达或要朗读的内容；"
            "不要说自己没有语音功能、不能发语音、发不出语音、让用户脑补、"
            "文字代替语音、念完了，也不要输出音频标记、SSML 或解释 TTS 机制。"
        )

    def build_group_system_message(
        self,
        *,
        user_name: str,
        user_text: str,
        recent_context: list[dict[str, Any]],
        persona_state: PersonaState,
        long_term_memory: str = "",
        group_context: str = "",
        model_context: str = "",
    ) -> str:
        return self.build_group_prompt(
            user_name=user_name,
            user_text=user_text,
            recent_context=recent_context,
            persona_state=persona_state,
            long_term_memory=long_term_memory,
            group_context=group_context,
            model_context=model_context,
        )[0]["content"]


def _format_section(label: str, values: list[str]) -> str:
    cleaned = [value.strip() for value in values if value.strip()]
    if not cleaned:
        return f"{label}：无。"
    return f"{label}：" + "；".join(cleaned) + "。"


def _format_behavior_profile(profile) -> str:
    sections = [
        _format_section("历史画像提炼出的回复节奏", profile.reply_cadence),
        _format_section("历史画像提炼出的标点习惯", profile.punctuation_profile),
        _format_section("历史画像提炼出的互动习惯", profile.interaction_habits),
        _format_section("可用聊天动作规则", profile.chat_action_rules),
    ]
    return "\n".join(section for section in sections if not section.endswith("无。"))


def _format_history_content(row: dict[str, Any], content: str) -> str:
    if row.get("scope_type") != "group" or row.get("role") != "user":
        return content
    user_name = str(row.get("user_name") or "").strip()
    if not user_name:
        return content
    if content.startswith(f"{user_name}:") or content.startswith(f"{user_name}："):
        return content
    return f"{user_name}：{content}"
