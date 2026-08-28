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
        _SectorSpec("医疗器械", "ISRG", "直觉外科"),
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

_A_SHARE_SECTOR_SYMBOLS = (
    ("600519.SH", "000858.SZ", "000568.SZ", "002304.SZ", "603369.SH", "000596.SZ", "600809.SH", "600779.SH", "600702.SH", "603198.SH"),
    ("601398.SH", "601939.SH", "601288.SH", "600036.SH", "601328.SH", "601166.SH", "000001.SZ", "600000.SH", "601818.SH", "601988.SH"),
    ("601318.SH", "601601.SH", "601336.SH", "601628.SH", "600061.SH", "000617.SZ", "601319.SH", "601339.SH", "601186.SH", "601390.SH"),
    ("600030.SH", "601211.SH", "600958.SH", "600109.SH", "601881.SH", "600837.SH", "000166.SZ", "600999.SH", "601375.SH", "601555.SH"),
    ("002594.SZ", "002812.SZ", "300014.SZ", "002709.SZ", "601127.SH", "002050.SZ", "002238.SZ", "600733.SH", "603178.SH", "002850.SZ"),
    ("300750.SZ", "002460.SZ", "300438.SZ", "300073.SZ", "688819.SH", "002340.SZ", "603659.SH", "688005.SH", "688567.SH", "688772.SH"),
    ("601012.SH", "688599.SH", "600438.SH", "002129.SZ", "300274.SZ", "601865.SH", "300393.SZ", "600732.SH", "002459.SZ", "603806.SH"),
    ("688981.SH", "603986.SH", "688012.SH", "688008.SH", "688396.SH", "002371.SZ", "300223.SZ", "603501.SH", "688256.SH", "688082.SH"),
    ("002475.SZ", "002351.SZ", "000049.SZ", "002241.SZ", "002600.SZ", "300782.SZ", "002456.SZ", "688036.SH", "002273.SZ", "002402.SZ"),
    ("600941.SH", "000063.SZ", "600050.SH", "601728.SH", "300628.SZ", "002281.SZ", "600522.SH", "600289.SH", "300921.SZ", "000988.SZ"),
    ("603019.SH", "002230.SZ", "300418.SZ", "688111.SH", "300496.SZ", "300229.SZ", "002920.SZ", "688327.SH", "300474.SZ", "688561.SH"),
    ("600588.SH", "300033.SZ", "600570.SH", "002410.SZ", "300454.SZ", "600845.SH", "300369.SZ", "688078.SH", "688318.SH", "300523.SZ"),
    ("600276.SH", "000538.SZ", "603259.SH", "300003.SZ", "600196.SH", "600332.SH", "002007.SZ", "000963.SZ", "300122.SZ", "600085.SH"),
    ("300760.SZ", "300015.SZ", "300529.SZ", "603658.SH", "688271.SH", "002223.SZ", "300298.SZ", "688389.SH", "603392.SH", "300406.SZ"),
    ("000333.SZ", "000651.SZ", "600690.SH", "000921.SZ", "603355.SH", "002032.SZ", "002508.SZ", "600854.SH", "000016.SZ", "600060.SH"),
    ("603288.SH", "600887.SH", "000895.SZ", "002557.SZ", "603027.SH", "600600.SH", "000729.SZ", "002568.SZ", "603866.SH", "600305.SH"),
    ("600900.SH", "600025.SH", "600011.SH", "600027.SH", "600886.SH", "601991.SH", "000591.SZ", "600905.SH", "000883.SZ", "601985.SH"),
    ("601088.SH", "601898.SH", "600188.SH", "601666.SH", "000983.SZ", "600123.SH", "600397.SH", "000937.SZ", "601101.SH", "600348.SH"),
    ("601899.SH", "603799.SH", "600547.SH", "000878.SZ", "002466.SZ", "600362.SH", "000630.SZ", "601600.SH", "600489.SH", "000962.SZ"),
    ("600893.SH", "600760.SH", "600151.SH", "000768.SZ", "600372.SH", "600118.SH", "600038.SH", "600435.SH", "300527.SZ", "688586.SH"),
)

_US_SHARE_SECTOR_SYMBOLS = (
    ("AAPL", "SONY", "HPQ", "DELL", "LOGI", "QCOM", "TXN", "MU", "STX", "WDC"),
    ("MSFT", "ORCL", "CRM", "ADBE", "INTU", "NOW", "SNOW", "PLTR", "SAP", "IBM"),
    ("NVDA", "AMD", "AVGO", "ARM", "TSM", "ASML", "MRVL", "SMCI", "AI", "ON"),
    ("GOOGL", "META", "SNAP", "PINS", "RDDT", "BIDU", "BABA", "JD", "PDD", "SE"),
    ("AMZN", "SHOP", "MELI", "ETSY", "EBAY", "W", "WMT", "COST", "TGT", "CVS"),
    ("SPOT", "NFLX", "DIS", "TME", "ROKU", "PARA", "FOXA", "MTCH", "LYV", "UBER"),
    ("JPM", "BAC", "C", "WFC", "GS", "MS", "SCHW", "BLK", "USB", "PNC"),
    ("V", "MA", "PYPL", "AXP", "XYZ", "FIS", "HOOD", "GPN", "ADYEY", "COF"),
    ("LLY", "JNJ", "PFE", "MRK", "ABBV", "BMY", "GILD", "AMGN", "REGN", "NVO"),
    ("ISRG", "SYK", "MDT", "BSX", "EW", "ABT", "DXCM", "ZBH", "BDX", "TMO"),
    ("KR", "HD", "LOW", "TJX", "ROST", "DG", "DLTR", "ORLY", "AZO", "SBUX"),
    ("KO", "PEP", "MNST", "MDLZ", "KHC", "GIS", "CAG", "HSY", "CL", "EL"),
    ("TSLA", "RIVN", "LI", "NIO", "XPEV", "GM", "F", "TM", "HMC", "STLA"),
    ("XOM", "CVX", "COP", "SLB", "EOG", "OXY", "PSX", "VLO", "MPC", "HAL"),
    ("CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC", "GD", "ETN", "EMR"),
    ("BA", "TDG", "HWM", "JOBY", "TXT", "HII", "LDOS", "KTOS", "RKLB", "ACHR"),
    ("TMUS", "VZ", "T", "CHTR", "CMCSA", "LUMN", "ERIC", "NOK", "TIGO", "ASTS"),
    ("INTC", "ADI", "NXPI", "MCHP", "MPWR", "KLAC", "LRCX", "AMAT", "TER", "SWKS"),
    ("PLD", "AMT", "EQIX", "CCI", "O", "SPG", "PSA", "WELL", "DLR", "VICI"),
    ("NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "ED", "XEL", "WEC"),
)

_BATCH_SECTOR_SYMBOLS = {
    "a_share": dict(zip((spec.name for spec in _SECTOR_SPECS["a_share"]), _A_SHARE_SECTOR_SYMBOLS, strict=True)),
    "us_share": dict(zip((spec.name for spec in _SECTOR_SPECS["us_share"]), _US_SHARE_SECTOR_SYMBOLS, strict=True)),
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
        stock_match = re.fullmatch(r"#([^\s#]{1,20})", text)
        if stock_match is not None:
            query = _normalize_lookup_query(stock_match.group(1))
            if query is not None:
                return await self._lookup_stock(query)
        return None

    async def _lookup_stock(self, query: str) -> MarketCommandResult:
        provider = self._providers.get("a_share")
        if provider is None:
            return MarketCommandResult(
                True,
                "A股行情功能未配置",
                "stock_lookup_unconfigured",
            )
        started = time.perf_counter()
        try:
            quote = await asyncio.wait_for(
                provider.quote("a_share", query),
                timeout=self._command_timeout_seconds,
            )
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - started
            return MarketCommandResult(
                True,
                f"个股查询超时：命令总时限 {self._command_timeout_seconds:g} 秒（耗时 {elapsed:.2f} 秒）",
                "stock_lookup_timeout",
            )
        except Exception:
            elapsed = time.perf_counter() - started
            return MarketCommandResult(
                True,
                f"个股查询失败：未找到匹配股票或数据源暂不可用（耗时 {elapsed:.2f} 秒）",
                "stock_lookup_failed",
            )
        if not isinstance(quote, MarketQuote):
            return MarketCommandResult(
                True,
                "个股查询失败：数据源返回格式无效",
                "stock_lookup_failed",
            )
        name = quote.name or query
        change = _quote_change_percent(quote)
        change_text = f"，涨跌 {change:+.2f}%" if change is not None else ""
        previous_text = (
            f"，昨收 {quote.previous_close:.2f}"
            if quote.previous_close is not None
            else ""
        )
        text = (
            f"{name}（{quote.symbol}）：现价 {quote.price:.2f}"
            f"{change_text}{previous_text}\n"
            f"来源：{quote.source}，数据时间{_format_market_time(quote.observed_at)}；"
            "数据可能延迟，仅供参考，不用于自动交易"
        )
        return MarketCommandResult(True, text, "stock_lookup")

    async def _overview(self, market: str, label: str) -> MarketCommandResult:
        provider = self._providers.get(market)
        if provider is None:
            return MarketCommandResult(True, f"{label}行情功能未配置", "market_unconfigured")
        batch_quote = getattr(provider, "quote_many", None)
        if callable(batch_quote) and getattr(provider, "supports_quote_many", True):
            return await self._batched_overview(market, label, batch_quote)
        started = time.perf_counter()
        sectors = _SECTOR_SPECS[market]
        tasks: list[asyncio.Task[MarketQuote]] = [
            asyncio.create_task(provider.quote(market, sector.symbol))
            for sector in sectors
        ]
        timed_out = False
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self._command_timeout_seconds,
                return_when=asyncio.ALL_COMPLETED,
            )
            timed_out = bool(pending)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            quotes: list[object] = [None] * len(tasks)
            task_indexes = {task: index for index, task in enumerate(tasks)}
            for task in done:
                index = task_indexes[task]
                try:
                    quotes[index] = task.result()
                except BaseException as exc:
                    quotes[index] = exc
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
            if timed_out:
                return MarketCommandResult(
                    True,
                    f"{label}行情获取超时：命令总时限 {self._command_timeout_seconds:g} 秒（耗时 {elapsed:.2f} 秒）",
                    "market_timeout",
                )
            return MarketCommandResult(
                True,
                f"{label}行情获取失败：数据源暂不可用（耗时 {elapsed:.2f} 秒）",
                "market_failed",
            )
        successful_pairs = [
            (sector, quote)
            for sector, quote in zip(sectors, quotes, strict=True)
            if isinstance(quote, MarketQuote)
        ]
        blocks = [
            _format_sector_report(index, sector, quote)
            for index, (sector, quote) in enumerate(successful_pairs, start=1)
        ]
        observed_at = _latest_observed_at(successful)
        footer = (
            "共 20 个板块；数据可能延迟，仅供参考，不用于自动交易"
            if len(successful_pairs) == len(sectors) and not timed_out
            else (
                f"仅获取 {len(successful_pairs)}/{len(sectors)} 个板块；"
                + (
                    "其余板块未能在命令总时限内完成，"
                    if timed_out
                    else "其余板块暂不可用，"
                )
                + "数据可能延迟，仅供参考，不用于自动交易"
            )
        )
        structured = build_structured_reply(
            header=f"{label}20板块核心股报｜查询截止 {observed_at}｜耗时 {elapsed:.2f} 秒",
            blocks=blocks,
            page_size=len(blocks),
            footer=footer,
            fallback_message=_build_market_brief(label, sectors, quotes, elapsed),
        )
        return MarketCommandResult(
            True,
            structured.text,
            "market_overview"
            if len(successful_pairs) == len(sectors) and not timed_out
            else "market_overview_partial",
            structured=structured,
        )

    async def _batched_overview(
        self,
        market: str,
        label: str,
        quote_many: Callable[[str, list[str]], Awaitable[list[MarketQuote]]],
    ) -> MarketCommandResult:
        started = time.perf_counter()
        sectors = _SECTOR_SPECS[market]
        symbols = [symbol for sector in sectors for symbol in _BATCH_SECTOR_SYMBOLS[market][sector.name]]
        timed_out = False
        try:
            quotes = await asyncio.wait_for(
                quote_many(market, symbols),
                timeout=self._command_timeout_seconds,
            )
        except asyncio.TimeoutError:
            quotes = []
            timed_out = True
        except Exception:
            quotes = []
        elapsed = time.perf_counter() - started
        by_symbol = {
            str(quote.symbol).upper(): quote
            for quote in quotes
            if isinstance(quote, MarketQuote)
        }
        blocks: list[str] = []
        complete = not timed_out
        for sector in sectors:
            sector_quotes = [
                by_symbol[symbol.upper()]
                for symbol in _BATCH_SECTOR_SYMBOLS[market][sector.name]
                if symbol.upper() in by_symbol
            ]
            gainers, losers = _select_gain_loser_quotes(sector_quotes)
            if len(gainers) < 5 or len(losers) < 5:
                complete = False
            if sector_quotes:
                blocks.append(
                    _format_batched_sector_report(
                        label,
                        sector,
                        sector_quotes,
                    )
                )
        if not blocks:
            reason = "market_timeout" if timed_out else "market_failed"
            status = "超时" if timed_out else "失败"
            return MarketCommandResult(
                True,
                f"{label}行情获取{status}：数据源暂不可用（耗时 {elapsed:.2f} 秒）",
                reason,
            )
        structured = StructuredReply(
            messages=tuple(blocks),
            fallback_messages=tuple(
                _compact_batched_sector_report(block) for block in blocks
            ),
        )
        return MarketCommandResult(
            True,
            structured.text,
            "market_overview" if complete else "market_overview_partial",
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


def _normalize_lookup_query(raw: str) -> str | None:
    value = raw.strip()
    if re.fullmatch(r"\d{5}", value):
        value = value.zfill(6)
    if re.fullmatch(r"\d{6}", value):
        return _normalize_symbol(value)[0]
    if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", value.upper()):
        return value.upper()
    if re.fullmatch(r"[\u4e00-\u9fff]{2,20}", value):
        return value
    return None


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


def _format_batched_sector_report(
    label: str,
    sector: _SectorSpec,
    quotes: list[MarketQuote],
) -> str:
    gainers, losers = _select_gain_loser_quotes(quotes)
    relative = len(gainers) < 5 or len(losers) < 5
    if relative and len(quotes) >= 10:
        ranked = sorted(
            quotes,
            key=lambda quote: _quote_change_percent(quote) or 0.0,
            reverse=True,
        )
        gainers = ranked[:5]
        losers = list(reversed(ranked[-5:]))
    selected = [*gainers, *losers]
    if relative and len(selected) == 10:
        heading = f"{label}｜{sector.name}：5只相对领涨，5只相对领跌"
    else:
        heading = f"{label}｜{sector.name}：{len(gainers)}只上涨，{len(losers)}只下跌"
    lines = [heading]
    for index, quote in enumerate(selected, start=1):
        change = _quote_change_percent(quote)
        change_text = f"{change:+.2f}%" if change is not None else "未知"
        name = quote.name or quote.symbol
        currency = "￥" if quote.market == "a_share" else "$"
        previous = (
            f"{quote.previous_close:.2f}{currency}"
            if quote.previous_close is not None
            else "未知"
        )
        price = f"{currency}{quote.price:.2f}" if currency == "$" else f"{quote.price:.2f}￥"
        lines.append(
            f"{index}、{name} 股票代码 {quote.symbol} 昨日收盘：{previous}，"
            f"当前价格：{price}，涨跌：{change_text}"
        )
    if len(selected) < 10:
        lines.append(f"数据暂缺：本板块仅获取 {len(selected)}/10 只可核验股票")
    lines.append("数据可能延迟，仅供参考，不用于自动交易")
    return "\n".join(lines)


def _select_gain_loser_quotes(
    quotes: list[MarketQuote],
) -> tuple[list[MarketQuote], list[MarketQuote]]:
    gainers = sorted(
        (
            quote
            for quote in quotes
            if (_quote_change_percent(quote) or 0.0) > 0
        ),
        key=lambda quote: _quote_change_percent(quote) or 0.0,
        reverse=True,
    )[:5]
    losers = sorted(
        (
            quote
            for quote in quotes
            if (_quote_change_percent(quote) or 0.0) < 0
        ),
        key=lambda quote: _quote_change_percent(quote) or 0.0,
    )[:5]
    return gainers, losers


def _compact_batched_sector_report(block: str) -> str:
    lines = block.splitlines()
    return "\n".join(lines[:1] + [line for line in lines[1:] if "股票代码" in line][:10] + lines[-1:])


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
    successful_pairs = [
        (sector, quote)
        for sector, quote in zip(sectors, quotes, strict=True)
        if isinstance(quote, MarketQuote)
    ]
    for index, (sector, quote) in enumerate(successful_pairs, start=1):
        tendency = _sector_tendency(quote)
        lines.append(f"{index}. {sector.name}：{sector.company}，{tendency}")
    if len(successful_pairs) == len(sectors):
        footer = f"共 20 个板块，耗时 {elapsed:.2f} 秒；完整版本超过平台单次发送上限，已汇总一次。"
    else:
        footer = (
            f"仅获取 {len(successful_pairs)}/{len(sectors)} 个板块，耗时 {elapsed:.2f} 秒；"
            "其余板块暂不可用，已汇总可核验内容。"
        )
    lines.append(footer)
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
