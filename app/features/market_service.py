from __future__ import annotations

import re
import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.features.contracts import MarketDataProvider, MarketQuote, StructuredReply
from app.features.structured_reply import build_structured_reply
from app.models import NormalizedMessage, StockWatchItem
from app.storage.repositories import StockWatchRepository


@dataclass(frozen=True)
class _SectorSpec:
    name: str
    symbol: str
    company: str


_SECTOR_SPECS = {
    "a_share": (
        _SectorSpec("白酒", "600519.SH", "贵州茅台"),
        _SectorSpec("银行", "601398.SH", "工商银行"),
        _SectorSpec("保险", "601318.SH", "中国平安"),
        _SectorSpec("证券", "600030.SH", "中信证券"),
        _SectorSpec("新能源汽车", "002594.SZ", "比亚迪"),
        _SectorSpec("动力电池", "300750.SZ", "宁德时代"),
        _SectorSpec("光伏", "601012.SH", "隆基绿能"),
        _SectorSpec("半导体", "688981.SH", "中芯国际"),
        _SectorSpec("消费电子", "002475.SZ", "立讯精密"),
        _SectorSpec("通信", "600941.SH", "中国移动"),
        _SectorSpec("人工智能", "603019.SH", "中科曙光"),
        _SectorSpec("软件", "600588.SH", "用友网络"),
        _SectorSpec("医药", "600276.SH", "恒瑞医药"),
        _SectorSpec("医疗器械", "300760.SZ", "迈瑞医疗"),
        _SectorSpec("家电", "000333.SZ", "美的集团"),
        _SectorSpec("食品饮料", "603288.SH", "海天味业"),
        _SectorSpec("电力", "600900.SH", "长江电力"),
        _SectorSpec("煤炭", "601088.SH", "中国神华"),
        _SectorSpec("有色金属", "601899.SH", "紫金矿业"),
        _SectorSpec("军工", "600893.SH", "航发动力"),
    ),
    "us_share": (
        _SectorSpec("消费电子", "AAPL", "苹果"),
        _SectorSpec("软件", "MSFT", "微软"),
        _SectorSpec("人工智能", "NVDA", "英伟达"),
        _SectorSpec("互联网", "GOOGL", "谷歌"),
        _SectorSpec("电子商务", "AMZN", "亚马逊"),
        _SectorSpec("社交平台", "META", "脸书母公司"),
        _SectorSpec("银行", "JPM", "摩根大通"),
        _SectorSpec("支付", "V", "维萨"),
        _SectorSpec("创新药", "LLY", "礼来"),
        _SectorSpec("综合制药", "JNJ", "强生"),
        _SectorSpec("零售", "WMT", "沃尔玛"),
        _SectorSpec("饮料", "KO", "可口可乐"),
        _SectorSpec("新能源汽车", "TSLA", "特斯拉"),
        _SectorSpec("能源", "XOM", "埃克森美孚"),
        _SectorSpec("工业设备", "CAT", "卡特彼勒"),
        _SectorSpec("航空制造", "BA", "波音"),
        _SectorSpec("通信", "TMUS", "美国移动通信"),
        _SectorSpec("半导体", "AVGO", "博通"),
        _SectorSpec("物流地产", "PLD", "安博"),
        _SectorSpec("公用事业", "NEE", "新纪元能源公司"),
    ),
}
_WATCHLIST_PAGE_SIZE = 4
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class MarketCommandResult:
    handled: bool
    text: str
    reason: str
    structured: StructuredReply | None = None


class MarketCommandService:
    def __init__(
        self,
        *,
        repository: StockWatchRepository,
        providers: dict[str, MarketDataProvider],
        default_alert_threshold_percent: float = 3.0,
        command_timeout_seconds: float = 20.0,
    ) -> None:
        self._repository = repository
        self._providers = providers
        self._default_threshold = default_alert_threshold_percent
        self._command_timeout_seconds = command_timeout_seconds

    async def handle(self, message: NormalizedMessage) -> MarketCommandResult | None:
        text = " ".join(message.text.strip().split())
        if text in {"#A股", "#美股"}:
            market = "a_share" if text == "#A股" else "us_share"
            return await self._overview(market, text.removeprefix("#"))
        if text.startswith("#股票添加 "):
            return await self._add(message, text.removeprefix("#股票添加 "))
        if text.startswith("#股票删除 "):
            return await self._delete(message, text.removeprefix("#股票删除 "))
        watch_match = re.fullmatch(
            r"#我的股票(?:\s+(详情))?(?:\s+--page\s+(-?\d+))?",
            text,
        )
        if watch_match is not None:
            page = int(watch_match.group(2) or "1")
            if page <= 0:
                return MarketCommandResult(
                    True,
                    "页码必须从 1 开始",
                    "stock_list_invalid_page",
                )
            return await self._list(
                message,
                details=watch_match.group(1) is not None,
                page=page,
            )
        return None

    async def _overview(self, market: str, label: str) -> MarketCommandResult:
        provider = self._providers.get(market)
        if provider is None:
            return MarketCommandResult(True, f"{label}行情功能未配置", "market_unconfigured")
        started = time.perf_counter()
        sectors = _SECTOR_SPECS[market]
        try:
            async with asyncio.timeout(self._command_timeout_seconds):
                quotes = list(
                    await asyncio.gather(
                        *(
                            provider.quote(market, sector.symbol)
                            for sector in sectors
                        ),
                        return_exceptions=True,
                    )
                )
        except TimeoutError:
            elapsed = time.perf_counter() - started
            return MarketCommandResult(
                True,
                f"{label}行情获取超时：命令总时限 {self._command_timeout_seconds:g} 秒（耗时 {elapsed:.2f} 秒）",
                "market_timeout",
            )
        except Exception:
            elapsed = time.perf_counter() - started
            return MarketCommandResult(
                True,
                f"{label}行情获取失败：数据源暂不可用（耗时 {elapsed:.2f} 秒）",
                "market_failed",
            )
        elapsed = time.perf_counter() - started
        successful = [quote for quote in quotes if isinstance(quote, MarketQuote)]
        if not successful:
            return MarketCommandResult(
                True,
                f"{label}行情获取失败：数据源暂不可用（耗时 {elapsed:.2f} 秒）",
                "market_failed",
            )
        if len(successful) < len(sectors):
            return MarketCommandResult(
                True,
                (
                    f"{label}行情数据不完整：仅获取 {len(successful)}/{len(sectors)} 个板块，"
                    f"本次不发送不完整股报（耗时 {elapsed:.2f} 秒）"
                ),
                "market_incomplete",
            )
        blocks = [
            _format_sector_report(index, sector, quote)
            for index, (sector, quote) in enumerate(zip(sectors, quotes, strict=True), start=1)
        ]
        observed_at = _latest_observed_at(successful)
        structured = build_structured_reply(
            header=f"{label}20板块核心股报｜查询截止 {observed_at}｜耗时 {elapsed:.2f} 秒",
            blocks=blocks,
            page_size=len(blocks),
            footer="共 20 个板块；数据可能延迟，仅供参考，不用于自动交易",
            fallback_message=_build_market_brief(label, sectors, quotes, elapsed),
        )
        return MarketCommandResult(
            True,
            structured.text,
            "market_overview",
            structured=structured,
        )

    async def _add(self, message: NormalizedMessage, arguments: str) -> MarketCommandResult:
        parts = arguments.split()
        if not parts:
            return MarketCommandResult(True, "用法：#股票添加 代码 [成本=价格] [数量=数量] [预警=3%]", "stock_add_invalid")
        try:
            symbol, market = _normalize_symbol(parts[0])
            options = _parse_options(parts[1:])
            threshold = options.get("alert", self._default_threshold)
            if threshold <= 0 or threshold > 100:
                raise ValueError("threshold")
            item = await self._repository.upsert(
                user_id=message.user_id,
                scope_type=message.scope_type,
                scope_id=message.scope_id,
                symbol=symbol,
                market=market,
                cost_price=options.get("cost"),
                quantity=options.get("quantity"),
                alert_threshold_percent=threshold,
            )
        except ValueError:
            return MarketCommandResult(True, "股票代码或参数格式不正确", "stock_add_invalid")
        return MarketCommandResult(
            True,
            f"已添加自选股 {item.symbol}，预警阈值 {item.alert_threshold_percent:g}%",
            "stock_added",
        )

    async def _delete(self, message: NormalizedMessage, value: str) -> MarketCommandResult:
        try:
            symbol, _ = _normalize_symbol(value.strip())
        except ValueError:
            return MarketCommandResult(True, "股票代码格式不正确", "stock_delete_invalid")
        deleted = await self._repository.delete(
            message.user_id,
            message.scope_type,
            message.scope_id,
            symbol,
        )
        return MarketCommandResult(
            True,
            f"已删除自选股 {symbol}" if deleted else f"自选股中没有 {symbol}",
            "stock_deleted" if deleted else "stock_not_found",
        )

    async def _list(
        self,
        message: NormalizedMessage,
        *,
        details: bool,
        page: int,
    ) -> MarketCommandResult:
        items = await self._repository.list_for_scope(
            message.user_id,
            message.scope_type,
            message.scope_id,
        )
        if not items:
            return MarketCommandResult(True, "你在当前会话还没有自选股", "stock_list_empty")
        start = (page - 1) * _WATCHLIST_PAGE_SIZE
        selected_items = items[start : start + _WATCHLIST_PAGE_SIZE]
        blocks = [""] * len(items)
        for offset, item in enumerate(selected_items):
            provider = self._providers.get(item.market)
            if provider is None:
                blocks[start + offset] = f"{item.symbol}：行情功能未配置"
                continue
            try:
                quote = await provider.quote(item.market, item.symbol)
            except Exception:
                blocks[start + offset] = f"{item.symbol}：行情获取失败"
                continue
            blocks[start + offset] = _format_watch_item(
                item,
                quote,
                details=details,
            )
        structured = build_structured_reply(
            header="我的股票",
            blocks=blocks,
            page=page,
            page_size=_WATCHLIST_PAGE_SIZE,
            next_command=(
                f"#我的股票{' 详情' if details else ''} --page {page + 1}"
            ),
            footer="数据可能延迟，仅供参考，不用于自动交易",
        )
        return MarketCommandResult(
            True,
            structured.text,
            "stock_list",
            structured=structured,
        )


def _normalize_symbol(raw: str) -> tuple[str, str]:
    value = raw.strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        return value, "a_share"
    if re.fullmatch(r"\d{6}", value):
        suffix = "SH" if value.startswith(("5", "6", "9")) else "SZ"
        return f"{value}.{suffix}", "a_share"
    if re.fullmatch(r"[A-Z^][A-Z0-9.^-]{0,14}", value):
        return value, "us_share"
    raise ValueError("invalid symbol")


def _parse_options(parts: list[str]) -> dict[str, float]:
    result = {}
    names = {"成本": "cost", "数量": "quantity", "预警": "alert"}
    for part in parts:
        if "=" not in part:
            raise ValueError("invalid option")
        name, raw_value = part.split("=", 1)
        key = names.get(name)
        if key is None or key in result:
            raise ValueError("invalid option")
        value = float(raw_value.removesuffix("%"))
        if value <= 0:
            raise ValueError("invalid option")
        result[key] = value
    return result


def _format_sector_report(
    index: int,
    sector: _SectorSpec,
    quote: MarketQuote | BaseException,
) -> str:
    if not isinstance(quote, MarketQuote):
        return f"{index}. {sector.name}：核心股{sector.company}；板块数据暂缺"
    return (
        f"{index}. {sector.name}：核心股{sector.company}；板块参考{_sector_tendency(quote)}；"
        f"数据时间{_format_market_time(quote.observed_at)}\n来源：{quote.source}"
    )


def _sector_tendency(quote: MarketQuote) -> str:
    change = _quote_change_percent(quote)
    if change is None or abs(change) < 0.5:
        return "震荡"
    return "偏强" if change > 0 else "偏弱"


def _format_market_time(value: str | None) -> str:
    parsed = _parse_market_time(value)
    if parsed is None:
        return "未知"
    local = parsed.astimezone(_BEIJING_TIMEZONE)
    return f"{local.year}年{local.month}月{local.day}日 {local.hour:02d}:{local.minute:02d}（北京时间）"


def _latest_observed_at(quotes: list[MarketQuote]) -> str:
    known = [
        parsed
        for quote in quotes
        if (parsed := _parse_market_time(quote.observed_at)) is not None
    ]
    if not known:
        return "时间未知"
    return _format_market_time(max(known).isoformat())


def _parse_market_time(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _build_market_brief(
    label: str,
    sectors: tuple[_SectorSpec, ...],
    quotes: list[MarketQuote | BaseException],
    elapsed: float,
) -> str:
    lines = [f"{label}板块核心股简报（二次汇总）"]
    for index, (sector, quote) in enumerate(zip(sectors, quotes, strict=True), start=1):
        tendency = _sector_tendency(quote) if isinstance(quote, MarketQuote) else "数据暂缺"
        lines.append(f"{index}. {sector.name}：{sector.company}，{tendency}")
    lines.append(f"共 20 个板块，耗时 {elapsed:.2f} 秒；完整版本超过平台单次发送上限，已汇总一次。")
    return "\n".join(lines)


def _format_watch_item(
    item: StockWatchItem,
    quote: MarketQuote,
    *,
    details: bool,
) -> str:
    parts = [f"{item.symbol}：{quote.price:.2f}"]
    if item.cost_price:
        profit_percent = (quote.price - item.cost_price) / item.cost_price * 100
        parts.append(f"持仓盈亏率 {profit_percent:+.2f}%")
    change = _quote_change_percent(quote)
    if change is not None:
        parts.append(f"当日涨跌 {change:+.2f}%")
    if details and item.quantity is not None:
        parts.append(f"数量 {item.quantity:g}")
    parts.append(f"{quote.source}，{quote.observed_at or '时间未知'}")
    return "，".join(parts)


def _quote_change_percent(quote: MarketQuote) -> float | None:
    if quote.change_percent is not None:
        return quote.change_percent
    if quote.previous_close:
        return (quote.price - quote.previous_close) / quote.previous_close * 100
    return None


async def run_market_alert_once(
    bot,
    repository: StockWatchRepository,
    providers: dict[str, MarketDataProvider],
    *,
    trading_date: date,
    can_send_group: Callable[[str], Awaitable[bool]] | None = None,
) -> int:
    sent = 0
    for item in await repository.list_enabled():
        if (
            item.scope_type == "group"
            and can_send_group is not None
            and not await can_send_group(item.scope_id)
        ):
            continue
        provider = providers.get(item.market)
        if provider is None:
            continue
        try:
            quote = await provider.quote(item.market, item.symbol)
        except Exception:
            continue
        change = _quote_change_percent(quote)
        if change is None or abs(change) < item.alert_threshold_percent:
            continue
        direction = "up" if change > 0 else "down"
        claimed = await repository.record_alert_once(
            watch_item_id=item.id,
            trading_date=trading_date,
            direction=direction,
            last_price=quote.price,
        )
        if not claimed:
            continue
        text = (
            f"自选股预警：{item.symbol} {change:+.2f}% ，现价 {quote.price:.2f}"
            f"（{quote.source}，{quote.observed_at or '时间未知'}；数据可能延迟）"
        )
        try:
            if item.scope_type == "group":
                await bot.call_api(
                    "send_group_msg",
                    group_id=int(item.scope_id),
                    message=text,
                )
            else:
                await bot.call_api(
                    "send_private_msg",
                    user_id=int(item.user_id),
                    message=text,
                )
        except Exception:
            await repository.release_alert(
                watch_item_id=item.id,
                trading_date=trading_date,
                direction=direction,
            )
            continue
        sent += 1
    return sent


async def market_alert_worker(
    bot,
    repository: StockWatchRepository,
    providers: dict[str, MarketDataProvider],
    *,
    poll_seconds: float = 300.0,
    timezone: str = "Asia/Shanghai",
) -> None:
    zone = ZoneInfo(timezone)
    while True:
        await run_market_alert_once(
            bot,
            repository,
            providers,
            trading_date=datetime.now(zone).date(),
        )
        await asyncio.sleep(poll_seconds)
