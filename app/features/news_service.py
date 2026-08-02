from __future__ import annotations

import re
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from app.features.contracts import NewsItem, NewsProvider, StructuredReply
from app.features.structured_reply import (
    OVERSIZED_URL_CONTINUATION,
    build_structured_reply,
    format_structured_external_url,
)
from app.models import NormalizedMessage, QQConfig
from app.storage.repositories import GroupNewsSubscriptionRepository


_CATEGORY_COMMANDS = {
    "#政事": ("politics", "全球政事"),
    "#财经": ("business", "财经"),
    "#科技": ("technology", "科技"),
    "#金融": ("finance", "金融"),
}
_DEFAULT_CATEGORIES = ("politics", "business", "technology", "finance")
_GROUP_HELP_ENTRIES = (
    "抖音/B站链接 - 自动下载并发送视频文件",
    "#政事 / #财经 / #科技 / #金融 - 今日分类新闻",
    "#A股 / #美股 - 市场概览",
    "#股票添加 代码 [成本=价格] [数量=数量] [预警=百分比]",
    "#股票删除 代码 / #我的股票 [详情]",
    "#chat 查一下 关键词 [--page 页码] / #chat 评价一下@群成员",
    "#画图 描述 / #改图 修改要求",
    "#新闻订阅 [HH:MM] / #新闻订阅状态 / #新闻退订（群主、群管理员或机器人管理员）",
)


@dataclass(frozen=True)
class NewsCommandResult:
    handled: bool
    text: str
    reason: str
    structured: StructuredReply | None = None


@dataclass(frozen=True)
class _NewsDigest:
    text: str
    complete: bool
    messages: tuple[str, ...]


def group_help_reply(page: int = 1) -> StructuredReply:
    return build_structured_reply(
        header="群聊功能",
        blocks=_GROUP_HELP_ENTRIES,
        page=page,
        page_size=4,
        next_command=f"/help {page + 1}",
    )


def group_help_text(page: int = 1) -> str:
    return group_help_reply(page).text


class NewsCommandService:
    def __init__(
        self,
        *,
        provider: NewsProvider | None,
        repository: GroupNewsSubscriptionRepository,
        qq_config: QQConfig,
        default_time: str = "08:00",
        timezone: str = "Asia/Shanghai",
        max_items: int = 8,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._qq_config = qq_config
        self._default_time = default_time
        self._timezone = timezone
        self._max_items = max_items

    async def handle(self, message: NormalizedMessage) -> NewsCommandResult | None:
        text = " ".join(message.text.strip().split())
        help_match = re.fullmatch(r"/help(?:\s+(-?\d+))?", text)
        if help_match is not None:
            page = int(help_match.group(1) or "1")
            if page <= 0:
                return NewsCommandResult(
                    True,
                    "页码必须从 1 开始",
                    "group_help_invalid_page",
                )
            structured = group_help_reply(page)
            return NewsCommandResult(
                True,
                structured.text,
                "group_help",
                structured=structured,
            )
        news_match = re.fullmatch(r"(#政事|#财经|#科技|#金融)(?:\s+(-?\d+))?", text)
        if news_match is not None:
            page = int(news_match.group(2) or "1")
            if page <= 0:
                return NewsCommandResult(
                    True,
                    "页码必须从 1 开始",
                    "news_invalid_page",
                )
            category, label = _CATEGORY_COMMANDS[news_match.group(1)]
            return await self._manual_news(
                category,
                label,
                page=page,
                command=news_match.group(1),
            )
        if text == "#新闻订阅状态":
            return await self._subscription_status(message)
        if text == "#新闻退订":
            return await self._set_subscription(message, enabled=False)
        if text == "#新闻订阅" or text.startswith("#新闻订阅 "):
            send_time = text.removeprefix("#新闻订阅").strip() or self._default_time
            if not _valid_hhmm(send_time):
                return NewsCommandResult(
                    True,
                    "时间格式错误，请使用 #新闻订阅 HH:MM",
                    "news_subscription_invalid_time",
                )
            return await self._set_subscription(
                message,
                enabled=True,
                send_time=send_time,
            )
        return None

    async def build_digest(self, categories: Sequence[str]) -> str:
        return (await self._build_digest(categories)).text

    async def _build_digest(self, categories: Sequence[str]) -> _NewsDigest:
        sections = []
        messages = []
        complete = True
        labels = {value[0]: value[1] for value in _CATEGORY_COMMANDS.values()}
        for category in categories:
            label = labels.get(category, category)
            blocks, error = await self._load_news_blocks(category, label)
            if error is not None:
                if error.reason != "news_empty":
                    complete = False
                sections.append(error.text)
                messages.append(error.text)
                continue
            category_messages = [f"{label}新闻\n{block}" for block in blocks]
            sections.append("\n\n".join(category_messages))
            messages.extend(category_messages)
        return _NewsDigest(
            text="\n\n".join(sections),
            complete=complete,
            messages=tuple(messages),
        )

    async def _manual_news(
        self,
        category: str,
        label: str,
        *,
        page: int = 1,
        command: str | None = None,
    ) -> NewsCommandResult:
        blocks, error = await self._load_news_blocks(category, label)
        if error is not None:
            return error
        structured = build_structured_reply(
            header=f"{label}新闻",
            blocks=blocks,
            page=page,
            page_size=2,
            next_command=f"{command or '#新闻'} {page + 1}",
        )
        return NewsCommandResult(
            True,
            structured.text,
            "news_manual",
            structured=structured,
        )

    async def _load_news_blocks(
        self,
        category: str,
        label: str,
    ) -> tuple[list[str], NewsCommandResult | None]:
        if self._provider is None:
            return [], NewsCommandResult(
                True, f"{label}新闻功能未配置", "news_provider_unconfigured"
            )
        try:
            items = list(await self._provider.fetch(category))[: self._max_items]
        except Exception:
            return [], NewsCommandResult(
                True, f"{label}新闻获取失败：数据源暂不可用", "news_provider_failed"
            )
        if not items:
            return [], NewsCommandResult(
                True, f"{label}今日暂无可用新闻", "news_empty"
            )
        blocks = []
        for index, item in enumerate(items, start=1):
            title = _clean_external_text(item.title, 100)
            source = _clean_external_text(item.source, 40) or "未知来源"
            published = _clean_external_text(item.published_at or "时间未知", 40)
            lines = [
                f"{index}. {title}",
                f"来源：{source}",
                f"时间：{published}",
            ]
            url, url_truncated = format_structured_external_url(item.url)
            lines.append(f"URL：{url}")
            if url_truncated:
                lines.append(OVERSIZED_URL_CONTINUATION)
            blocks.append("\n".join(lines))
        return blocks, None

    async def _subscription_status(self, message: NormalizedMessage) -> NewsCommandResult:
        subscription = await self._repository.get(message.group_id or "")
        if subscription is None or not subscription.enabled:
            text = "本群定时新闻：关闭"
        else:
            text = (
                f"本群定时新闻：开启，每日 {subscription.send_time} "
                f"({subscription.timezone})"
            )
        return NewsCommandResult(True, text, "news_subscription_status")

    async def _set_subscription(
        self,
        message: NormalizedMessage,
        *,
        enabled: bool,
        send_time: str | None = None,
    ) -> NewsCommandResult:
        if not self._can_manage_subscription(message):
            return NewsCommandResult(
                True,
                "只有群主、群管理员或机器人管理员可以修改新闻订阅",
                "news_subscription_forbidden",
            )
        if enabled and self._provider is None:
            return NewsCommandResult(
                True,
                "新闻功能未配置，暂时无法开启订阅",
                "news_subscription_provider_unconfigured",
            )
        active_time = send_time or self._default_time
        await self._repository.set_enabled(
            group_id=message.group_id or "",
            enabled=enabled,
            send_time=active_time,
            timezone=self._timezone,
            categories=_DEFAULT_CATEGORIES,
            updated_by=message.user_id,
        )
        return NewsCommandResult(
            True,
            (
                f"本群定时新闻已开启：每日 {active_time} ({self._timezone})"
                if enabled
                else "本群定时新闻已关闭"
            ),
            "news_subscription_enabled" if enabled else "news_subscription_disabled",
        )

    def _can_manage_subscription(self, message: NormalizedMessage) -> bool:
        return (
            message.group_role in {"owner", "admin"}
            or message.user_id in self._qq_config.owner_user_ids
            or message.user_id in self._qq_config.root_user_ids
        )


def _valid_hhmm(value: str) -> bool:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    return bool(match and int(match.group(1)) <= 23 and int(match.group(2)) <= 59)


def _clean_external_text(value: str, limit: int) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


async def run_news_subscription_once(
    bot,
    service: NewsCommandService,
    repository: GroupNewsSubscriptionRepository,
    *,
    now: datetime,
    can_send_group: Callable[[str], Awaitable[bool]] | None = None,
) -> int:
    due = await repository.list_due(
        local_date=now.date(),
        local_time=now.strftime("%H:%M"),
    )
    sent_count = 0
    for subscription in due:
        if can_send_group is not None and not await can_send_group(
            subscription.group_id
        ):
            continue
        try:
            checkpoint = await repository.get_delivery_checkpoint(
                group_id=subscription.group_id,
                local_date=now.date(),
            )
            if checkpoint is None:
                digest = await service._build_digest(subscription.categories)
                if not digest.complete:
                    continue
                checkpoint = await repository.get_or_create_delivery_checkpoint(
                    group_id=subscription.group_id,
                    local_date=now.date(),
                    messages=digest.messages,
                )
            for index in range(
                checkpoint.next_message_index,
                len(checkpoint.messages),
            ):
                await bot.call_api(
                    "send_group_msg",
                    group_id=int(subscription.group_id),
                    message=checkpoint.messages[index],
                )
                await repository.advance_delivery_checkpoint(
                    group_id=subscription.group_id,
                    local_date=now.date(),
                    expected_index=index,
                )
        except Exception:
            continue
        await repository.complete_delivery(subscription.group_id, now.date())
        sent_count += 1
    return sent_count


async def news_subscription_worker(
    bot,
    service: NewsCommandService,
    repository: GroupNewsSubscriptionRepository,
    *,
    timezone: str = "Asia/Shanghai",
    poll_seconds: float = 30.0,
) -> None:
    zone = ZoneInfo(timezone)
    while True:
        await run_news_subscription_once(
            bot,
            service,
            repository,
            now=datetime.now(zone),
        )
        await asyncio.sleep(poll_seconds)
