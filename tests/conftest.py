from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def security_rows(*tickers, market: str = "US"):
    exchange = "XNAS" if market == "US" else "XHKG"
    return [
        {
            "security_id": f"{market}:{exchange}:{ticker}",
            "ticker": ticker,
            "market": market,
            "exchange": exchange,
            "security_type": "common_stock",
            "currency": "USD" if market == "US" else "HKD",
            "valid_from": "2000-01-01",
            "valid_to": None,
            "name": ticker,
            "industry": "technology",
            "source_id": "test",
        }
        for ticker in tickers
    ]
