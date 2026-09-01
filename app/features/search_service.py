from __future__ import annotations

import re
from dataclasses import dataclass

from app.features.contracts import SearchProvider, StructuredReply
from app.features.information_localizer import (
    InformationLocalizationError,
    localize_information_fields,
)
from app.features.structured_reply import (
    build_compact_brief,
    build_structured_reply,
    format_structured_external_url,
)
from app.model.llm_client import LlmClient
from app.models import NormalizedMessage
from app.storage.repositories import (
    ConversationRepository,
    ConversationSessionRepository,
    GroupMemberProfileRepository,
)


@dataclass(frozen=True)
class SearchCommandResult:
    handled: bool
    text: str
    reason: str
    model_called: bool = False
    structured: StructuredReply | None = None


class GroupSearchCommandService:
    def __init__(
        self,
        *,
        search_provider: SearchProvider | None,
        model_client: LlmClient,
        conversation_repository: ConversationRepository,
        session_repository: ConversationSessionRepository,
        profile_repository: GroupMemberProfileRepository,
        max_results: int = 40,
        page_size: int = 20,
        minimum_results: int = 20,
    ) -> None:
        self._search_provider = search_provider
        self._model_client = model_client
        self._conversations = conversation_repository
        self._sessions = session_repository
        self._profiles = profile_repository
        self._max_results = max_results
        self._page_size = page_size
        self._minimum_results = minimum_results

    async def handle(self, message: NormalizedMessage) -> SearchCommandResult | None:
        if message.scope_type != "group" or not message.group_id:
            return None
        text = " ".join(message.text.strip().split())
        search_match = re.fullmatch(
            r"#chat\s+(?:帮我\s*)?查一下(?:\s+(.+?))?(?:\s+--page\s+(-?\d+))?",
            text,
            re.IGNORECASE,
        )
        if search_match is not None:
            page = int(search_match.group(2) or "1")
            if page <= 0:
                return SearchCommandResult(
                    True,
                    "页码必须从 1 开始",
                    "search_invalid_page",
                )
            return await self._search(
                search_match.group(1) or "",
                page=page,
            )
        if re.fullmatch(r"#chat\s+评价一下.*", text, re.IGNORECASE):
            return await self._evaluate(message)
        return None

    async def _search(self, query: str, *, page: int = 1) -> SearchCommandResult:
        query = _clean_external_text(query, 200)
        if not query:
            return SearchCommandResult(
                True,
                "用法：#chat 查一下 关键词",
                "search_query_missing",
            )
        if self._search_provider is None:
            return SearchCommandResult(
                True,
                "资料检索功能未配置",
                "search_provider_unconfigured",
            )
        try:
            results = list(await self._search_provider.search(query))[: self._max_results]
        except Exception:
            return SearchCommandResult(
                True,
                "资料检索失败：数据源暂不可用",
                "search_provider_failed",
            )
        if not results:
            return SearchCommandResult(True, "没有找到可用资料", "search_empty")
        effective_page_size = self._page_size
        if len(results) < self._page_size * 2:
            effective_page_size = len(results)
        start = (page - 1) * effective_page_size
        selected_results = results[start : start + effective_page_size]
        try:
            localized = await localize_information_fields(
                self._model_client,
                [
                    {
                        "id": str(start + index),
                        "title": item.title,
                        "summary": item.snippet or "暂无摘要",
                    }
                    for index, item in enumerate(selected_results, start=1)
                ],
            )
        except InformationLocalizationError:
            return SearchCommandResult(
                True,
                "资料中文化失败：模型暂不可用，本次未发送外文内容",
                "search_localization_failed",
                model_called=True,
            )
        selected_blocks = []
        for index, (item, translated) in enumerate(
            zip(selected_results, localized, strict=True),
            start=start + 1,
        ):
            title = _clean_external_text(translated["title"], 120) or "无标题"
            source = _clean_external_text(item.source, 50) or "未知来源"
            lines = [f"{index}. {title}", f"来源：{source}"]
            snippet = _clean_external_text(translated["summary"], 240) or "暂无摘要"
            url, url_truncated = format_structured_external_url(item.url)
            lines.extend((f"摘要：{snippet}", f"URL：{url}"))
            if url_truncated:
                lines.append("说明：链接异常过长，未予展示")
            selected_blocks.append("\n".join(lines))
        blocks = [""] * len(results)
        blocks[start : start + len(selected_blocks)] = selected_blocks
        fallback = _build_search_brief(selected_blocks)
        structured = build_structured_reply(
            header="资料检索结果",
            blocks=blocks,
            page=page,
            page_size=effective_page_size,
            next_command=f"在原查询命令末尾添加 --page {page + 1}",
            footer=(
                f"本页 {len(selected_blocks)} 条，共取得 {len(blocks)} 条可核验资料"
                if selected_blocks
                else None
            ),
            fallback_message=fallback,
        )
        return SearchCommandResult(
            True,
            structured.text,
            "search_results",
            structured=structured,
        )

    async def _evaluate(self, message: NormalizedMessage) -> SearchCommandResult:
        target_ids = list(
            dict.fromkeys(
                user_id
                for user_id in message.mentioned_user_ids
                if user_id and user_id != message.self_id
            )
        )
        if len(target_ids) != 1:
            return SearchCommandResult(
                True,
                "请真实 @ 一位群成员后再评价",
                "evaluation_target_invalid",
            )
        target_id = target_ids[0]
        session_id = message.session_id
        if session_id is None:
            session = await self._sessions.get_latest_for_scope(
                "group",
                message.group_id or "",
                initiator_user_id=message.user_id,
            )
            session_id = session.session_id if session is not None else None

        profile = await self._profiles.get(message.group_id or "", target_id)
        history = []
        if session_id is not None:
            rows = await self._conversations.get_recent_conversations(
                "group",
                message.group_id or "",
                limit=30,
                session_id=session_id,
            )
            history = [
                row
                for row in rows
                if row.get("role") == "user"
                and str(row.get("user_id")) == target_id
            ]
        if not history and (profile is None or not profile.summary.strip()):
            return SearchCommandResult(
                True,
                "当前聊天线和本群低敏画像里还没有足够信息",
                "evaluation_context_empty",
            )

        display_name = (
            _clean_external_text(profile.display_name, 32)
            if profile is not None and profile.display_name
            else "该成员"
        )
        context_lines = []
        if profile is not None and profile.summary.strip():
            context_lines.append(
                "本群低敏表达习惯：" + _clean_external_text(profile.summary, 240)
            )
        for row in history[-12:]:
            content = _clean_external_text(row.get("content"), 300)
            if content:
                context_lines.append(f"{display_name}在当前聊天线发言：{content}")
        prompt = [
            {
                "role": "system",
                "content": (
                    "你只根据提供的当前群当前聊天线发言和低敏表达画像评价沟通风格。"
                    "不要推断政治立场、民族、宗教、健康、性取向、财务、违法经历等敏感属性，"
                    "不要暴露账号标识，不要做人格诊断或事实断言。用简短、克制的中文回答。"
                ),
            },
            {
                "role": "user",
                "content": "请评价这位群成员的群聊表达特点：\n" + "\n".join(context_lines),
            },
        ]
        try:
            generated = await self._model_client.generate(prompt)
        except Exception:
            return SearchCommandResult(
                True,
                "群友评价生成失败：模型暂不可用",
                "evaluation_model_failed",
                model_called=True,
            )
        return SearchCommandResult(
            True,
            generated.text.strip(),
            "member_evaluation",
            model_called=True,
        )


def _clean_external_text(value, limit: int) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:limit]


def _build_search_brief(blocks: list[str]) -> str:
    return build_compact_brief(
        header="资料检索简报（二次汇总）",
        blocks=blocks,
        footer=f"本页共 {len(blocks)} 条；完整版本超过平台单次发送上限，已汇总一次。",
    )
