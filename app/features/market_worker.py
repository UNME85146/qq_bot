from __future__ import annotations

import json
import sys
from dataclasses import asdict

from app.features.market_providers import AkShareMarketProvider, YFinanceMarketProvider
from app.features.provider_health import classify_provider_error


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        provider = str(payload.get("provider") or "")
        market = str(payload.get("market") or "")
        symbol = str(payload.get("symbol") or "")
        symbols = payload.get("symbols")
        if isinstance(symbols, list):
            requested_symbols = [str(item) for item in symbols]
        else:
            requested_symbols = []
        if provider == "akshare":
            quotes = (
                AkShareMarketProvider._quotes_sync(market, requested_symbols)
                if requested_symbols
                else [AkShareMarketProvider._quote_sync(market, symbol)]
            )
        elif provider == "yfinance":
            quotes = (
                YFinanceMarketProvider._quotes_sync(market, requested_symbols)
                if requested_symbols
                else [YFinanceMarketProvider._quote_sync(market, symbol)]
            )
        else:
            raise ValueError("unsupported market provider")
        result = {"ok": True, "quotes": [asdict(quote) for quote in quotes]}
        if not requested_symbols:
            result["quote"] = result["quotes"][0]
        return_code = 0
    except Exception as exc:
        result = {"ok": False, "category": classify_provider_error(exc)}
        return_code = 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
