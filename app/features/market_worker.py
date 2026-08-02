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
        if provider == "akshare":
            quote = AkShareMarketProvider._quote_sync(market, symbol)
        elif provider == "yfinance":
            quote = YFinanceMarketProvider._quote_sync(market, symbol)
        else:
            raise ValueError("unsupported market provider")
        result = {"ok": True, "quote": asdict(quote)}
        return_code = 0
    except Exception as exc:
        result = {"ok": False, "category": classify_provider_error(exc)}
        return_code = 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
