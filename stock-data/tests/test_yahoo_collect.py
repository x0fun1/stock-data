"""Offline consistency checks for yahoo_collect currency/unit and calendar disclosure."""

from __future__ import annotations

import datetime as dt
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))

import yahoo_collect as yc  # noqa: E402
from quant_research import data_checks  # noqa: E402


def _bars(count: int = 260, seed: int = 5, end: str = "2026-10-30") -> list[dict]:
    rng = random.Random(seed)
    day = dt.date.fromisoformat(end)
    days = []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    days.reverse()
    price = 100.0
    rows = []
    for session in days:
        price *= math.exp(rng.gauss(0.0004, 0.01))
        rows.append({"date": session.isoformat(), "open": round(price * 0.997, 4),
                     "high": round(price * 1.01, 4), "low": round(price * 0.99, 4),
                     "close": round(price, 4), "volume": 1_000_000})
    return rows


def _envelope(symbol: str, *, currency: str = "USD", unit=None) -> dict:
    bars = _bars()
    asof = f"{bars[-1]['date']}T21:00:00Z"
    return {
        "status": "success", "actual_source": "Yahoo Finance", "source_symbol": symbol,
        "currency": currency, "unit": unit, "frequency": "1d", "adjustment": "provider",
        "source_timestamp": asof, "fetched_at_utc": "2026-10-30T22:00:00Z",
        "data": {"symbol": symbol, "currency": currency, "frequency": "1d",
                 "bars": bars, "source_symbol": symbol, "source_timestamp": asof,
                 "fetched_at_utc": "2026-10-30T22:00:00Z",
                 "history_window": {"start": bars[0]["date"], "end": bars[-1]["date"],
                                    "selection": "trailing 52 weeks"}},
    }


class YahooMetadataTests(unittest.TestCase):
    def test_currency_is_recorded_and_missing_unit_is_not_ineligible(self):
        for ticker in ("GOOGL", "QQQ"):
            envelope = _envelope(ticker)
            bars = envelope["data"]["bars"]
            request = {"ticker": ticker, "market": "US", "horizon": "60D", "intent": "news",
                       "asof": f"{bars[-1]['date']}T21:00:00Z"}
            block = yc._market(envelope, request, bars[0]["date"], bars[-1]["date"], None)
            self.assertEqual(block["currency"], "USD")
            self.assertIsNone(block["unit"])
            self.assertEqual(block["currency_evidence"]["value"], "USD")
            self.assertNotEqual(block["policy_status"], "ineligible")
            self.assertTrue(any("unit" in warning for warning in block["warnings"]))
            gate = data_checks.check_market(block, request, captured_at="2026-10-30T22:10:00Z",
                                            allow_library_calendar=False)
            self.assertNotIn("PRICE_BASIS_UNVERIFIED", gate["reason_codes"])
            self.assertNotIn("MARKET_UNITS_CONFLICT", gate["reason_codes"])

    def test_missing_calendar_library_is_explicit(self):
        original_lib, original_status = data_checks._library_calendar, data_checks.calendar_library_available
        data_checks._library_calendar = lambda *_args, **_kwargs: None
        data_checks.calendar_library_available = lambda: False
        try:
            bars = _bars(25)
            findings = data_checks._Findings()
            data_checks._resolve_session_axis(
                {}, {"market": "US"}, bars, bars[0]["date"], bars[-1]["date"],
                dt.datetime(2026, 10, 30, 21, tzinfo=dt.timezone.utc),
                dt.date(2026, 10, 30), None, True, findings,
            )
            self.assertIn("CALENDAR_LIB_MISSING", findings.reason_codes)
        finally:
            data_checks._library_calendar = original_lib
            data_checks.calendar_library_available = original_status


if __name__ == "__main__":
    unittest.main()
