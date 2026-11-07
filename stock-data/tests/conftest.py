from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from stock_research.contracts import Market, PriceBar, SecurityIdentity  # noqa: E402
from stock_research.identity_calendar import build_us_calendar  # noqa: E402


@pytest.fixture
def us_calendar():
    return build_us_calendar(2023, 2026)


def make_days(start: str = "2024-01-02", count: int = 300):
    cal = build_us_calendar(2023, 2026)
    return cal.sessions(date.fromisoformat(start), date(2026, 12, 31))[:count]


def price_rows(
    security_id: str,
    days,
    start_price: float = 100.0,
    drift: float = 0.0003,
    vol: float = 0.012,
    seed: int = 7,
    market: str = "US",
    volume: float = 1_000_000.0,
):
    rng = np.random.default_rng(seed)
    price = start_price
    rows = []
    for day in days:
        price *= 1.0 + drift + rng.normal(0.0, vol)
        rows.append(
            {
                "security_id": security_id,
                "market": market,
                "date": day.isoformat(),
                "open": price * 0.999,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": volume,
                "amount": price * volume,
                "currency": "USD" if market == "US" else "HKD",
                "source_id": "test",
            }
        )
    return rows


def bars_from_rows(rows, security_id: str):
    return [
        PriceBar(
            security_id=security_id,
            date=date.fromisoformat(row["date"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row.get("volume"),
            amount=row.get("amount"),
            currency=row.get("currency", ""),
            source_id=row.get("source_id", ""),
        )
        for row in rows
    ]


def write_runtime(tmp_path: Path, prices: pd.DataFrame, securities: list[dict], extra: dict[str, pd.DataFrame] | None = None):
    from stock_research.storage import CuratedStore

    store = CuratedStore(tmp_path)
    store.save("prices", prices, {"market": "US"})
    store.save("securities", pd.DataFrame(securities), {"market": "US"})
    empty = pd.DataFrame(
        columns=[
            "security_id",
            "ex_date",
            "action_type",
            "q",
            "cash_old",
            "currency",
            "available_at",
            "confirmed",
            "supported",
        ]
    )
    store.save("actions", empty, {"market": "US"})
    store.save("action_coverage", pd.DataFrame(columns=["security_id", "start", "end", "complete", "evidence", "source_id"]), {"market": "US"})
    for name, frame in (extra or {}).items():
        store.save(name, frame, {"market": "US"})
    return store


def security_rows(*tickers, market: str = "US"):
    rows = []
    for ticker in tickers:
        exchange = "XNAS" if market == "US" else "XHKG"
        rows.append(
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
        )
    return rows
