from __future__ import annotations

from typing import Any

from app.models import PersonaConfig, PersonaState


class PromptBuilder:
    def __init__(self, persona: PersonaConfig) -> None:
        self._persona = persona

    def build_private_prompt(
        self,
        user_name: str,
        user_text: str,
        recent_context: list[dict[str, Any]],
        persona_state: PersonaState,
        long_term_memory: str = "",
        model_context: str = "",
    ) -> list[dict[str, Any]]:
        profile = self._persona.style_profile
        system_parts = [
            f"你是 QQ 机器人账号，不是 {profile.source_user_id} 本人。",
            f"你的目标是模仿 {profile.source_user_id} 的聊天风格，而不是冒充其身份。",
            "被问身份、真人、本人、是不是目标用户时，必须透明披露。",
            f"身份披露规则：{profile.identity_disclosure}",
            f"固定聊天风格摘要：{profile.style_summary}",
            "聊天记录画像只是风格参考，不是必须使用的词库或固定台词。",
            "优先根据当前语境自然回答；不要为了模仿而硬塞口头禅、数字梗、技术词或示例句。",
            "只有语境自然匹配时，才少量借用短表达、话题倾向或示例里的节奏。",
            _format_section("语气规则", profile.tone_rules),
            _format_section("可参考的话题倾向，按当前语境取用", profile.topic_biases),
            _format_section("语境合适时可参考的短表达，不要强行使用", profile.lexicon),
            _format_section("回复规则", profile.reply_rules),
            _format_section("禁止事项", profile.avoid_rules),
            _format_section("少量风格示例，只学节奏和语气，不要复述原句", profile.few_shot_examples),
            "回复要像 QQ 好友即时聊天，大多数回复 1-2 句，短一点，自然一点。",
            "不要主动列表化回答，除非用户明确要求。",
            "如需代码块，保持完整 Markdown 代码块；不要输出孤立的 ```。",
            "如果用 JSON 包装回复，只使用 reply_text/text/content、reply_mode、send_sticker、sticker_intent 这些字段。",
            "当前角色状态："
            f"mood={persona_state.mood}, "
            f"energy={persona_state.energy}, "
            f"trust={persona_state.trust}, "
            f"relationship_stage={persona_state.relationship_stage}。",
        ]
        if long_term_memory.strip():
            system_parts.append(f"可用的受控长期记忆：\n{long_term_memory.strip()}")
        if model_context.strip():
            system_parts.append(model_context.strip())
        system_message = "\n".join(system_parts)

        messages = [{"role": "system", "content": system_message}]
        for row in recent_context[-10:]:
            role = row.get("role", "user")
            if role not in {"user", "assistant"}:
                continue
            content = row.get("content", "").strip()
            if content:
                messages.append({"role": role, "content": content})

        messages.append(
            {
                "role": "user",
                "content": f"当前用户：{user_name}\n用户本次消息：{user_text}\n请直接给出 QQ 风格短回复。",
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
        )
        messages[0]["content"] += (
            "\n当前场景：群聊。你是在被 @ 后回复，回复要短，不要抢话，"
            "不要把群聊当成客服工单。"
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
