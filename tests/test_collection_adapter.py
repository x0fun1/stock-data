"""Regressions for normalization, source failure, provenance and data gates."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stock-data" / "scripts"))
from quant_research.contracts import normalize_request
from quant_research.snapshot import freeze_snapshot, load_snapshot
from quant_research.stock_data_adapter import StockDataAdapter
from research_fixture import ASOF, collection


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.value = collection(100)

    def freeze(self, value=None):
        path = self.root / "input.json"
        path.write_text(json.dumps(value or self.value), encoding="utf-8")
        return freeze_snapshot(path, self.root / "snapshots")

    def test_finnhub_normalized_bars_and_articles_survive_raw_envelope(self):
        receipt = StockDataAdapter().make_receipt(self.value["request"], self.value["domains"], captured_at_utc=ASOF)
        self.assertEqual(receipt["domains"]["market"]["data"]["bars"], self.value["domains"]["market"]["data"]["bars"])
        self.assertNotIn("next_actions", receipt["domains"]["market"]["gateway_envelope"])
        news = self.value["domains"]["news"]
        news["gateway_envelope"] = {"is_success": True, "data": {"top_headlines": []}}
        receipt = StockDataAdapter().make_receipt(self.value["request"], self.value["domains"])
        self.assertTrue(receipt["domains"]["news"]["data"]["articles"])

    def test_outer_inner_failures_and_empty_status_cannot_be_wrapped_as_success(self):
        for envelope in ({"status": "error"}, {"is_success": False}, {"is_success": "true"}, {"isError": True}, {"structuredContent": {"is_success": False}}, {"error": {"message": "provider failed"}}):
            with self.subTest(envelope=envelope):
                block = {"status": "complete", "data": {"bars": [1]}, "gateway_envelope": envelope}
                result = StockDataAdapter().make_receipt(self.value["request"], {"market": block})["domains"]["market"]
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["data"])
        block = {"status": "complete", "data": {"bars": [1]}, "gateway_envelope": {"status": "empty"}}
        self.assertIsNone(StockDataAdapter().make_receipt(self.value["request"], {"market": block})["domains"]["market"]["data"])

    def test_direct_freeze_does_not_bypass_provider_failure(self):
        self.value["domains"]["market"]["gateway_envelope"] = {"is_success": False}
        with self.assertRaisesRegex(ValueError, "missing or failed"):
            self.freeze()

    def test_valid_source_evidence_passes_and_has_confirmed_price(self):
        manifest, _ = load_snapshot(self.freeze())
        self.assertTrue(manifest["data_validation"]["forecast_eligible"])
        self.assertEqual(manifest["latest_confirmed_close"]["close_at"], "2024-03-15T20:00:00Z")

    def test_symbol_frequency_window_and_invalid_timestamps_are_rejected(self):
        for mutation in (lambda b: b["data"].update(symbol="MSFT"), lambda b: b["data"].update(frequency="1wk"), lambda b: b.update(fetched_at_utc="invalid"), lambda b: b.update(source_timestamp="2099-01-01T00:00:00Z"), lambda b: b["data"]["history_window"].update(start="2099-01-01")):
            original = copy.deepcopy(self.value)
            mutation(self.value["domains"]["market"])
            with self.assertRaises(ValueError):
                self.freeze()
            self.value = original

    def test_missing_calendar_or_adjustment_evidence_blocks_forecast(self):
        for field in ("session_calendar", "adjustment_evidence"):
            value = copy.deepcopy(self.value)
            value["domains"]["market"].pop(field)
            manifest, _ = load_snapshot(self.freeze(value))
            self.assertFalse(manifest["data_validation"]["forecast_eligible"])

    def test_capture_timestamp_cannot_be_missing_or_after_snapshot_creation(self):
        value = copy.deepcopy(self.value)
        value.pop("captured_at_utc")
        self.assertFalse(load_snapshot(self.freeze(value))[0]["data_validation"]["forecast_eligible"])
        self.value["captured_at_utc"] = "2099-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "capture occurs after"):
            self.freeze()

    def test_pre_close_fetch_or_source_time_cannot_confirm_a_daily_close(self):
        for field in ("fetched_at_utc", "source_timestamp"):
            value = copy.deepcopy(self.value)
            value["domains"]["market"][field] = "2024-03-15T19:00:00Z"
            if field == "fetched_at_utc":
                value["domains"]["market"]["source_timestamp"] = "2024-03-15T19:00:00Z"
            manifest, _ = load_snapshot(self.freeze(value))
            self.assertFalse(manifest["data_validation"]["forecast_eligible"])
            self.assertEqual(manifest["latest_confirmed_close"]["kind"], "historical_bar_close_unverified")

    def test_missing_session_is_not_compressed_into_daily_horizon(self):
        self.value["domains"]["market"]["data"]["bars"].pop(20)
        manifest, _ = load_snapshot(self.freeze())
        self.assertFalse(manifest["data_validation"]["forecast_eligible"])
        self.assertTrue(any("axis" in item for item in manifest["data_validation"]["blockers"]))

    def test_latest_missing_session_blocks_forecast(self):
        data = self.value["domains"]["market"]["data"]
        data["bars"].pop()
        data["history_window"]["end"] = data["bars"][-1]["date"]
        manifest, _ = load_snapshot(self.freeze())
        self.assertFalse(manifest["data_validation"]["forecast_eligible"])

    def test_calendar_close_overrides_untrusted_last_bar_closed_true(self):
        self.value["request"]["asof"] = "2024-03-15T13:00:00Z"
        self.value["domains"]["market"]["source_timestamp"] = self.value["request"]["asof"]
        manifest, market = load_snapshot(self.freeze())
        self.assertEqual(market["data"]["bars"][-1]["date"], "2024-03-14")
        self.assertEqual(manifest["latest_confirmed_close"]["session_date"], "2024-03-14")
        self.assertTrue(manifest["data_validation"]["forecast_eligible"])

    def test_historical_asof_is_valid_even_when_fetched_later(self):
        self.value["captured_at_utc"] = "2026-10-07T00:00:00Z"
        self.value["domains"]["market"]["fetched_at_utc"] = self.value["captured_at_utc"]
        self.assertTrue(load_snapshot(self.freeze())[0]["data_validation"]["forecast_eligible"])

    def test_weekend_and_special_close_use_supplied_calendar(self):
        self.value["request"]["asof"] = "2024-03-16T12:00:00Z"
        self.value["domains"]["market"]["session_calendar"]["coverage_end"] = "2024-03-16"
        self.value["domains"]["market"]["session_calendar"]["sessions"][-1]["close_at"] = "2024-03-15T17:00:00Z"
        manifest, _ = load_snapshot(self.freeze())
        self.assertTrue(manifest["data_validation"]["forecast_eligible"])
        self.assertEqual(manifest["latest_confirmed_close"]["close_at"], "2024-03-15T17:00:00Z")

    def test_unadjusted_split_and_boolean_prices_are_not_accepted_for_forecast(self):
        block = self.value["domains"]["market"]
        block["adjustment"] = block["adjustment_evidence"]["price_basis"] = "unadjusted"
        block["adjustment_evidence"]["actions"] = [{"type": "split", "date": "2024-03-01"}]
        self.assertFalse(load_snapshot(self.freeze())[0]["data_validation"]["forecast_eligible"])
        block["data"]["bars"][-1]["volume"] = True
        with self.assertRaisesRegex(ValueError, "numeric volume"):
            self.freeze()

    def test_manifest_quality_tampering_fails_integrity_check(self):
        destination = self.freeze()
        path = destination / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update(data_quality={"overall": "high", "score": 1}, warnings=[])
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
            load_snapshot(destination)

    def test_legacy_snapshot_is_readable_but_not_new_gate_verified(self):
        destination = self.freeze()
        path = destination / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = "1.0"
        value.pop("manifest_sha256")
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(load_snapshot(destination)[0]["data_validation"]["forecast_eligible"])

    def test_request_contract_preserves_user_window_and_rejects_unsafe_or_unknown_fields(self):
        value = normalize_request({**self.value["request"], "history_start": "2024-01-01", "history_end": "2024-03-15"})
        self.assertEqual(value["history_window"]["start"], "2024-01-01")
        for changes in ({"ticker": "INTC;command"}, {"ticker": 123}, {"depth": "secretly_ignored"}, {"history_start": "2025-01-01", "history_end": "2024-01-01"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_request({**self.value["request"], **changes})

    def test_receipt_output_refuses_overwrite_and_secrets_are_redacted(self):
        block = self.value["domains"]["market"]
        block["gateway_envelope"]["api_key"] = "ARTIFICIAL-SECRET"
        output = self.root / "receipt.json"
        adapter = StockDataAdapter()
        adapter.write_receipt(self.value["request"], self.value["domains"], output)
        self.assertNotIn("ARTIFICIAL-SECRET", output.read_text(encoding="utf-8"))
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            adapter.write_receipt(self.value["request"], self.value["domains"], output)
        self.assertEqual(output.read_bytes(), before)
