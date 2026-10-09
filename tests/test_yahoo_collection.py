"""Offline collection fixtures; no dependencies installed or network calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from yahoo_collect import collect, write_responses, main
from quant_research.stock_data_adapter import StockDataAdapter
from quant_research.snapshot import freeze_snapshot, load_snapshot
from quant_research.orchestrator import run_analysis
from research_fixture import collection, article


class Provider:
    def __init__(self, history=None, news=None):
        self.history = history or {"status": "empty", "data": {"bars": []}}
        self.news = news or {"status": "empty", "data": {"articles": []}}
        self.calls = []

    def call(self, name, **params):
        self.calls.append((name, params))
        return copy.deepcopy(self.history if name == "yahoo_history" else self.news)


def fixture():
    value = collection(100)
    market = copy.deepcopy(value["domains"]["market"])
    market["status"] = "success"
    market["source_symbol"] = "INTC"
    market["provider_library"] = "yfinance"
    market["provider_version"] = "fixture"
    market["data"]["bars"][-1]["timestamp_utc"] = market["source_timestamp"]
    request = copy.deepcopy(value["request"])
    request["history_window"] = {"start": market["data"]["bars"][0]["date"], "end": market["data"]["bars"][-1]["date"]}
    return request, market, market["session_calendar"]


class CollectionTests(unittest.TestCase):
    def test_nested_metadata_and_provider_provenance_preserved(self):
        request, market, cal = fixture()
        for key in ("source_symbol", "currency", "adjustment", "unit"):
            market["data"][key] = market.pop(key)
        fetched = market.pop("fetched_at_utc")
        market["provenance"] = {"fetched_at_utc": fetched, "provider_library": "yfinance", "provider_version": "fixture"}
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(market), domains=["market"])["market"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["fetched_at_utc"], fetched)

    def test_fallback_provenance_survives_collection_and_adapter(self):
        request, market, cal = fixture()
        market.update(fallback_attempted=True, fallback_used=True,
                      fallback_route="stock_kline_yahoo",
                      fallback_reason={"category": "NetworkError"})
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(market), domains=["market"])
        block = result["market"]
        self.assertEqual(block["fallback_route"], "stock_kline_yahoo")
        self.assertEqual(block["fallback_reason"]["category"], "NetworkError")
        receipt = StockDataAdapter().make_receipt(request, result)
        self.assertTrue(receipt["domains"]["market"]["fallback_attempted"])
        self.assertTrue(receipt["domains"]["market"]["fallback_used"])
        self.assertEqual(receipt["domains"]["market"]["fallback_route"], "stock_kline_yahoo")

    def test_cli_news_failure_is_partial_success_not_market_failure(self):
        request, market, cal = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            request_path, output = Path(tmp) / "request.json", Path(tmp) / "responses.json"
            request_path.write_text(json.dumps(request))
            responses = {"market": {"status": "complete"}, "news": {"status": "failed"}}
            args = ["--request", str(request_path), "--output", str(output), "--cache-dir", str(Path(tmp) / "cache")]
            with patch("yahoo_collect.collect", return_value=responses) as mocked:
                self.assertEqual(main(args), 0)
                self.assertEqual(json.loads(output.read_text()), responses)
                self.assertEqual(main(args), 1)
                self.assertEqual(mocked.call_count, 1)

    def test_explicit_inclusive_end_and_only_requested_domain(self):
        request, market, cal = fixture()
        provider = Provider(market)
        with patch("yahoo_collect._calendar", return_value=cal):
            responses = collect(request, provider=provider, domains=["market"])
        self.assertEqual(set(responses), {"market"})
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][1]["end"], "2024-03-16")
        self.assertEqual(provider.calls[0][1]["price_basis"], "adjusted")
        self.assertTrue(responses["market"]["last_bar_closed"])
        self.assertEqual(responses["market"]["status"], "complete")
        self.assertEqual(request["history_window"]["end"], "2024-03-15")

    def test_calendar_unknown_is_not_closed_by_fixed_clock(self):
        request, market, _ = fixture()
        market.pop("session_calendar")
        with patch("yahoo_collect._calendar", return_value=None):
            result = collect(request, provider=Provider(market), domains=["market"])
        self.assertIsNone(result["market"]["last_bar_closed"])
        self.assertEqual(result["market"]["status"], "partial")

    def test_unclosed_and_outside_bars_removed_and_timestamp_not_faked(self):
        request, market, cal = fixture()
        request["asof"] = "2024-03-15T19:00:00Z"
        market["data"]["bars"].append({"date": "2024-03-16", "close": 12})
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(market), domains=["market"])["market"]
        self.assertEqual(result["data"]["bars"][-1]["date"], "2024-03-14")
        self.assertNotIn("source_timestamp", result)
        reasons = {r["reason"] for r in result["collection_provenance"]["excluded_bars"]}
        self.assertEqual(reasons, {"session_not_closed_at_asof", "outside_requested_window"})

    def test_bar_midnight_timestamp_keeps_real_semantics(self):
        request, market, cal = fixture()
        market["data"]["bars"][-1]["timestamp_utc"] = "2024-03-15T04:00:00Z"
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(market), domains=["market"])["market"]
        self.assertEqual(result["source_timestamp"], "2024-03-15T04:00:00Z")
        self.assertEqual(result["source_timestamp_kind"], "bar_label_not_trade_time")
        self.assertEqual(result["collection_provenance"]["source_timestamp_kind"], "bar_label_not_trade_time")

    def test_provider_error_code_and_native_library_provenance_are_preserved(self):
        request, market, cal = fixture()
        market.pop("provider_library", None)
        market.pop("provider_version", None)
        market["provenance"] = {"library": "yfinance", "version": "1.7.0"}
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(market), domains=["market"])["market"]
        self.assertEqual(result["provider_library"], "yfinance")
        self.assertEqual(result["provider_version"], "1.7.0")

        class DependencyFailure(Exception):
            code = "DependencyUnavailable"

        class FailingProvider:
            def call(self, name, **params):
                raise DependencyFailure("sensitive details are redacted")

        failed = collect(request, provider=FailingProvider(), domains=["news"])["news"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["gateway_envelope"]["error"]["category"], "DependencyUnavailable")
        self.assertNotIn("sensitive details", json.dumps(failed))

    def test_news_failure_does_not_clear_market(self):
        request, market, cal = fixture()
        provider = Provider(market, {"status": "error", "error": {"category": "RateLimited"}})
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=provider)
        self.assertEqual(result["market"]["status"], "complete")
        self.assertEqual(result["news"]["status"], "failed")
        self.assertEqual(len(result["market"]["data"]["bars"]), 100)

    def test_missing_identity_currency_adjustment_not_guessed(self):
        request, market, cal = fixture()
        for k in ("source_symbol", "currency", "adjustment"):
            market.pop(k)
        market["data"].pop("symbol")
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(market), domains=["market"])["market"]
        self.assertNotIn("source_symbol", result)
        self.assertNotIn("currency", result)
        self.assertNotIn("adjustment", result)
        self.assertEqual(result["status"], "partial")

    def test_news_keeps_real_tags_timestamps_and_partial_feed(self):
        request, _, _ = fixture()
        source = article()
        result = collect(request, provider=Provider(news={"status": "success", "data": {"articles": [source]}}), domains=["news"])
        self.assertEqual(result["news"]["status"], "partial")
        self.assertEqual(result["news"]["data"]["articles"], [source])
        self.assertNotIn("source_symbol", result["news"])

    def test_empty_failed_and_invalid_are_distinct(self):
        request, _, _ = fixture()
        with patch("yahoo_collect._calendar", return_value=None):
            result = collect(request, provider=Provider(history={"status": "success", "data": {"bars": []}}))
            invalid = collect(request, provider=Provider(history={"status": "success", "data": {}}), domains=["market"])
        self.assertEqual(result["market"]["status"], "unavailable")
        self.assertEqual(invalid["market"]["status"], "failed")

    def test_json_redaction_no_overwrite_and_offline_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.json"
            write_responses({"cookie": "secret", "number": float("nan"), "zero": 0, "negative": -2}, path)
            value = json.loads(path.read_text())
            self.assertEqual(value["cookie"], "[redacted]")
            self.assertIsNone(value["number"])
            self.assertEqual(value["negative"], -2)
            with self.assertRaises(FileExistsError):
                write_responses({}, path)
        result = subprocess.run([sys.executable, str(ROOT / "stock-data/scripts/yahoo_collect.py"), "--help"], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_actual_adapt_freeze_analyze_with_same_schema_and_provenance(self):
        request, market, cal = fixture()
        with patch("yahoo_collect._calendar", return_value=cal):
            responses = collect(request, provider=Provider(market, {"status": "success", "data": {"articles": [article()]}}))
        value = StockDataAdapter().make_receipt(request, responses)
        self.assertEqual(value["request"]["history_window"], request["history_window"])
        self.assertEqual(value["domains"]["market"]["provider_library"], "yfinance")
        self.assertIn("collection_provenance", value["domains"]["market"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_responses(value, root / "collection.json")
            snapshot = freeze_snapshot(path, root / "snapshots")
            self.assertEqual(load_snapshot(snapshot)[0]["schema_version"], "1.2")
            report = run_analysis(snapshot, root / "research")
            self.assertTrue((report / "quant_result.freeze.json").is_file())
            self.assertTrue((report / "news_result.json").is_file())

    def test_default_three_years_uses_latest_confirmed_closed_session(self):
        request, market, cal = fixture()
        request.pop("history_window")
        request["asof"] = "2024-03-15T19:00:00Z"
        provider = Provider(market)
        with patch("yahoo_collect._calendar", return_value=cal):
            collect(request, provider=provider, domains=["market"])
        self.assertEqual(provider.calls[0][1]["start"], "2021-03-14")
        self.assertEqual(provider.calls[0][1]["end"], "2024-03-15")

    def test_hk_exchange_session_date_not_utc_shifted(self):
        request = {"ticker": "0700.HK", "market": "HK", "asof": "2026-10-02T09:00:00Z", "history_window": {"start": "2026-10-02", "end": "2026-10-02"}}
        history = {"status": "success", "data": {"bars": [{"date": "2026-10-02", "timestamp_utc": "2026-10-01T16:00:00Z", "close": 10}], "currency": "HKD", "source_symbol": "0700.HK", "adjustment": "adjusted", "unit": "HKD/share"}}
        cal = {"source": "fixture", "market": "HK", "timezone": "Asia/Hong_Kong", "coverage_start": "2026-10-02", "coverage_end": "2026-10-02", "sessions": [{"date": "2026-10-02", "open_at": "2026-10-02T01:30:00Z", "close_at": "2026-10-02T08:00:00Z"}]}
        with patch("yahoo_collect._calendar", return_value=cal):
            result = collect(request, provider=Provider(history), domains=["market"])["market"]
        self.assertEqual(result["data"]["bars"][0]["date"], "2026-10-02")
        self.assertEqual(result["source_timestamp"], "2026-10-01T16:00:00Z")
        self.assertTrue(result["last_bar_closed"])


if __name__ == "__main__":
    unittest.main()
