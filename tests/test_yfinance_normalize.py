"""Offline deterministic mappings; frame doubles need no optional dependency."""
import json
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stock-data" / "scripts"))
import yfinance_normalize as n
from quant_research.stock_data_adapter import StockDataAdapter
from quant_research.news.normalize import normalize_articles

try:
    import pandas as pd
except ImportError:
    pd = None


class Frame:
    def __init__(self, rows, columns=None):
        self.rows = rows
        self.columns = columns or list(rows[0][1]) if rows else (columns or [])
    def iterrows(self):
        return iter(self.rows)


class Series:
    index = [0, 1]
    def items(self):
        return iter([(datetime(2026, 1, 1), 0), (datetime(2026, 1, 2), float("nan"))])


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.meta = {"symbol": "0700.HK", "exchangeTimezoneName": "Asia/Hong_Kong", "currency": "HKD"}
        self.stamp = datetime(2026, 10, 6, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        self.row = {"Open": 100, "High": 110, "Low": 90, "Close": 100,
                    "Adj Close": 50, "Volume": 0, "Dividends": 0, "Stock Splits": 0}
        self.frame = Frame([(self.stamp, self.row)])

    def test_json_safe_axes_null_and_zero(self):
        encoded = n.json_safe({"frame": self.frame, "series": Series(), "null": float("nan"),
                               "infinity": float("inf"), "zero": 0, "negative": -2,
                               datetime(2026, 1, 1): Decimal("0")})
        json.dumps(encoded, allow_nan=False)
        self.assertEqual(encoded["zero"], 0)
        self.assertIsNone(encoded["null"])
        self.assertEqual(encoded["series"]["rows"][0]["value"], 0)
        self.assertIsNone(encoded["series"]["rows"][1]["value"])
        self.assertEqual(encoded["frame"]["rows"][0]["values"][5], 0)

    def test_provider_and_adjusted_hong_kong_date(self):
        provider = n.normalize_history(self.frame, self.meta)
        adjusted = n.normalize_history(self.frame, self.meta, price_basis="adjusted")
        p, a = provider["data"]["bars"][0], adjusted["data"]["bars"][0]
        self.assertEqual(p["date"], "2026-10-06")
        self.assertEqual(p["timestamp_utc"], "2026-10-05T16:00:00Z")
        self.assertEqual(p["close"], 100)
        self.assertEqual(a["close"], 50)
        self.assertEqual(a["provider_ohlc"]["close"], 100)
        self.assertEqual(a["volume"], 0)
        self.assertFalse(adjusted["adjustment_evidence"]["action_coverage_confirmed"])
        self.assertIsNone(adjusted["last_bar_closed"])

    def test_missing_and_invalid_ratios(self):
        for adj in (None, float("nan"), float("inf"), 0, -1):
            row = {**self.row, "Adj Close": adj}
            result = n.normalize_history(Frame([(self.stamp, row)]), self.meta, price_basis="adjusted")
            self.assertEqual(result["status"], "partial")
            self.assertIsNone(result["data"]["bars"][0]["close"])
            self.assertEqual(result["data"]["bars"][0]["provider_ohlc"]["close"], 100)
            json.dumps(result, allow_nan=False)

    def test_intraday_no_collapse_and_dst(self):
        zone = ZoneInfo("America/New_York")
        result = n.normalize_history(Frame([(datetime(2026, 3, 9, 9, 30, tzinfo=zone), self.row),
                                            (datetime(2026, 3, 9, 10, 30, tzinfo=zone), self.row)]),
                                     {"exchangeTimezoneName": "America/New_York"}, interval="1h")
        bars = result["data"]["bars"]
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["timestamp_utc"], "2026-03-09T13:30:00Z")
        self.assertNotEqual(bars[0]["timestamp_utc"], bars[1]["timestamp_utc"])

    def test_naive_timezone_is_not_invented(self):
        result = n.normalize_history(Frame([(datetime(2026, 1, 1), self.row)]), {})
        self.assertIsNone(result["source_timestamp"])
        self.assertIn("exchange_timezone_unconfirmed", result["warnings"])

    def test_adapter_bar_timestamp_and_confirmed_identity(self):
        meta = {**self.meta, "source_symbol": "0700.HK"}
        meta.pop("symbol")
        result = n.normalize_history(self.frame, meta, symbol="REQUEST_ONLY")
        domain = StockDataAdapter().make_receipt({"ticker": "0700.HK", "market": "HK"}, {"market": result})["domains"]["market"]
        bars = domain["data"]["bars"]
        self.assertEqual(domain["data"]["source_symbol"], "0700.HK")
        self.assertEqual(domain["data"]["symbol"], "0700.HK")
        self.assertEqual(bars[0]["timestamp"], "2026-10-05T16:00:00Z")
        self.assertEqual(bars[0]["date"], "2026-10-06")
        self.assertEqual(result["data"]["bars"][0]["timestamp"], result["data"]["bars"][0]["timestamp_utc"])
        unconfirmed = n.normalize_history(self.frame, {"identity": "REQUEST_ONLY"}, symbol="REQUEST_ONLY")
        self.assertIsNone(unconfirmed["source_symbol"])
        self.assertIsNone(unconfirmed["data"]["symbol"])

    def test_adapter_news_canonical_dates_tags_and_update_preserved(self):
        # Yahoo news routes were removed; this covers the Finnhub MCP article shape
        # (headline/source/datetime_utc) reaching the same adapter and PIT filter.
        articles = [{"id": "one", "title": "Report", "summary": None,
                     "publisher": "Publisher", "source": "Publisher",
                     "url": "https://example.com/report",
                     "published_at": "2026-10-06T12:00:00Z",
                     "updated_at": "2026-10-07T12:00:00Z",
                     "tickers": ["AAPL"], "record_type": "news"}]
        domain = StockDataAdapter().make_receipt(
            {"ticker": "AAPL"},
            {"news": {"status": "success", "data": {"articles": articles}}})["domains"]["news"]
        raw = domain["data"]["articles"][0]
        articles_in, excluded, _ = normalize_articles(domain["data"], ticker="AAPL", asof_timestamp="2026-10-06T20:00:00Z")
        self.assertEqual(articles_in, [])
        self.assertEqual(excluded["updated_after_asof"], 1)
        articles_in, _, _ = normalize_articles(domain["data"], ticker="AAPL", asof_timestamp="2026-10-08T20:00:00Z")
        mapped = articles_in[0]
        self.assertEqual(mapped["published_at"], "2026-10-06T12:00:00Z")
        self.assertEqual(raw["updated_at"], "2026-10-07T12:00:00Z")
        self.assertEqual(mapped["tickers"], ["AAPL"])
        self.assertEqual(mapped["record_type"], "news")

    @unittest.skipIf(pd is None, "pandas not installed in this interpreter")
    def test_real_pandas_nullable_and_timestamp_mapping(self):
        frame = pd.DataFrame([self.row, {**self.row, "Volume": pd.NA, "Adj Close": float("nan")}],
                             index=pd.DatetimeIndex([self.stamp, self.stamp.replace(day=7)]))
        mapped = n.normalize_history(frame, self.meta, price_basis="adjusted")
        self.assertEqual(mapped["data"]["bars"][0]["volume"], 0)
        self.assertIsNone(mapped["data"]["bars"][1]["volume"])
        self.assertIsNone(mapped["data"]["bars"][1]["close"])
        safe = n.json_safe({"frame": frame, "series": pd.Series([0, pd.NA], dtype="Int64"), "nat": pd.NaT})
        json.dumps(safe, allow_nan=False)
        self.assertIsNone(safe["nat"])
        self.assertIsNone(safe["series"]["rows"][1]["value"])

    def test_repair_adj_close_only_and_currency(self):
        repaired = Frame([(self.stamp, {**self.row, "Adj Close": 51, "Repaired?": False})])
        result = n.repair_diff(self.frame, repaired, {"currency": "USD"}, {"currency": "HKD"})
        self.assertTrue(result["changed"])
        self.assertTrue(any(row["field"] == "Adj Close" for row in result["differences"]))

    def test_alignment_and_source_missing_are_separate(self):
        second = datetime(2026, 10, 7, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        empty_row = {key: None for key in self.row}
        frames = {"A": Frame([(self.stamp, self.row), (second, empty_row)]),
                  "B": Frame([(self.stamp, empty_row)])}
        result = n.normalize_history_batch(frames, {"A": self.meta}, source_indices={"A": [self.stamp]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["symbols"]["A"]["bar_count"], 1)
        self.assertEqual(len(result["symbols"]["A"]["alignment"]["excluded_slots"]), 1)
        self.assertEqual(result["symbols"]["B"]["status"], "empty")
        unknown = n.normalize_history_batch({"A": frames["A"]}, {"A": self.meta})
        self.assertEqual(unknown["status"], "partial")
        self.assertEqual(unknown["symbols"]["A"]["bar_count"], 2)
        self.assertIn("unconfirmed", unknown["symbols"]["A"]["alignment"]["null_origin"])

    def test_financial_currency_period_and_partial(self):
        end = datetime(2025, 12, 31)
        income = Frame([("TotalRevenue", {end: 0}), ("NetIncome", {end: -10})], [end])
        result = n.normalize_financials({"income": income, "cashflow": Frame([])}, "quarterly", "USD")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["records"][0]["value"], 0)
        self.assertEqual(result["records"][1]["value"], -10)
        self.assertIsNone(result["records"][0]["published_at"])
        self.assertEqual(result["records"][0]["currency"], "USD")
        ttm = n.normalize_financials({"balance_sheet": income}, "ttm")
        self.assertEqual(ttm["statements"]["balance_sheet"], "unsupported")

    def test_options_and_funds(self):
        options = n.normalize_options({"calls": Frame([(0, {"lastTradeDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
                                                              "bid": 0, "openInterest": None})]), "puts": Frame([])})
        self.assertEqual(options["calls"][0]["bid"], 0)
        self.assertIsNone(options["calls"][0]["openInterest"])
        self.assertEqual(options["calls"][0]["lastTradeDate"], "2026-01-01T00:00:00Z")
        funds = n.normalize_funds({"top_holdings": self.frame})
        self.assertIsNone(funds["holdings_as_of"])
        self.assertTrue(funds["warnings"])


if __name__ == "__main__":
    unittest.main()
