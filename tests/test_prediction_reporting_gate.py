"""Regression coverage for prediction/reporting gate separation."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from quant_research.audit import audit
from quant_research.consensus import build_consensus
from quant_research.orchestrator import run_analysis
from quant_research.snapshot import freeze_snapshot, load_snapshot
from quant_research.contracts import researcher_result
from quant_research.gate_policy import aggregate_gate_telemetry, classify_reasons
from research_fixture import collection


class PredictionReportingGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.value = copy.deepcopy(collection())

    def freeze(self):
        path = self.root / "collection.json"
        path.write_text(json.dumps(self.value), encoding="utf-8")
        return freeze_snapshot(path, self.root / "snapshots")

    def test_installed_calendar_schedule_is_pinned_in_snapshot(self):
        market = self.value["domains"]["market"]
        supplied = copy.deepcopy(market.pop("session_calendar"))
        supplied["source"] = "exchange_calendars:test-version"
        with patch("quant_research.data_checks._library_calendar", return_value=supplied) as resolver:
            snapshot = self.freeze()
            manifest, _ = load_snapshot(snapshot)
        self.assertEqual(resolver.call_count, 1)  # freeze resolves once; reload uses the pinned schedule
        self.assertEqual(manifest["schema_version"], "1.2")
        self.assertEqual(manifest["data_validation"]["calendar_verification"], "library_verified")
        self.assertEqual(manifest["data_validation"]["resolved_calendar_evidence"]["source"], "exchange_calendars:test-version")
        self.assertTrue(manifest["data_validation"]["forecast_eligible"])

    def test_complete_calendar_adjustment_and_cross_source_close_pass_data_gate(self):
        market = self.value["domains"]["market"]
        bar = market["data"]["bars"][-1]
        market["latest_price_observations"] = [{
            "source": "Finnhub", "session_date": bar["date"], "price": bar["close"],
            "currency": "USD", "kind": "daily_close",
        }]
        manifest, _ = load_snapshot(self.freeze())
        gate = manifest["data_validation"]
        self.assertTrue(gate["forecast_eligible"])
        self.assertEqual(gate["latest_close_status"], "cross_source_verified")
        self.assertEqual(gate["reporting_status"], "pass")
        self.assertEqual(gate["adjustment_status"], "provider_declared")
        self.assertEqual(gate["corporate_action_status"], "clear")

    def test_fallback_calendar_and_unknown_adjustment_allow_quant_degraded(self):
        self.value["request"]["mode"] = "strict"
        market = self.value["domains"]["market"]
        market.pop("session_calendar")
        market.pop("adjustment")
        market.pop("adjustment_evidence")
        snapshot = self.freeze()
        manifest, _ = load_snapshot(snapshot)
        gate = manifest["data_validation"]
        self.assertTrue(gate["forecast_eligible"])
        self.assertEqual(gate["prediction_eligibility"]["status"], "eligible")
        self.assertEqual(gate["reporting_status"], "degraded")
        self.assertEqual(gate["calendar_verification"], "observation_fallback")
        self.assertEqual(gate["horizon_type"], "estimated_observed_sessions")
        self.assertEqual(gate["adjustment_status"], "unknown")
        output = run_analysis(snapshot, self.root / "research")
        report = json.loads((output / "report.json").read_text())
        self.assertGreater(report["consensus"]["available_paths"], 0)
        self.assertIsNotNone(report["consensus"]["prob_up"])
        self.assertEqual(report["consensus"]["reporting_status"], "degraded")
        self.assertIn(report["consensus"]["horizon_type"], {"estimated_observed_sessions", "exchange_sessions"})

    def test_matching_same_session_close_works_without_official_calendar(self):
        market = self.value["domains"]["market"]
        market.pop("session_calendar")
        bar = market["data"]["bars"][-1]
        market["latest_price_observations"] = [{
            "source": "Finnhub", "session_date": bar["date"], "price": bar["close"] * 1.001,
            "currency": "USD", "kind": "daily_close",
        }]
        manifest, _ = load_snapshot(self.freeze())
        gate = manifest["data_validation"]
        self.assertEqual(gate["latest_close_status"], "cross_source_verified")
        self.assertIn("LATEST_CLOSE_CROSS_VERIFIED", gate["reason_codes"])
        self.assertTrue(gate["forecast_eligible"])
        self.assertEqual(gate["reporting_status"], "degraded")

    def test_conflicting_same_session_close_vetoes(self):
        market = self.value["domains"]["market"]
        market.pop("session_calendar")
        bar = market["data"]["bars"][-1]
        market["latest_price_observations"] = [{
            "source": "Finnhub", "session_date": bar["date"], "price": bar["close"] * 1.02,
            "currency": "USD", "kind": "daily_close",
        }]
        manifest, _ = load_snapshot(self.freeze())
        gate = manifest["data_validation"]
        self.assertFalse(gate["forecast_eligible"])
        self.assertEqual(gate["reporting_status"], "veto")
        self.assertIn("LATEST_PRICE_CONFLICT", gate["reason_codes"])

    def test_pairwise_provider_conflict_vetoes_even_when_each_nearly_matches_primary(self):
        market = self.value["domains"]["market"]
        market.pop("session_calendar")
        bar = market["data"]["bars"][-1]
        market["latest_price_observations"] = [
            {"source": "Finnhub", "session_date": bar["date"], "price": bar["close"] * 0.9971,
             "currency": "USD", "kind": "daily_close"},
            {"source": "Polygon", "session_date": bar["date"], "price": bar["close"] * 1.0029,
             "currency": "USD", "kind": "daily_close"},
        ]
        manifest, _ = load_snapshot(self.freeze())
        self.assertIn("LATEST_PRICE_CONFLICT", manifest["data_validation"]["reason_codes"])
        self.assertFalse(manifest["data_validation"]["forecast_eligible"])

    def test_confirmed_split_in_evidenced_unadjusted_window_vetoes(self):
        last_day = self.value["domains"]["market"]["data"]["bars"][-1]["date"]
        for adjustment in ("unadjusted", None):
            self.value = copy.deepcopy(collection())
            market = self.value["domains"]["market"]
            if adjustment is None:
                market.pop("adjustment", None)
            else:
                market["adjustment"] = adjustment
            market["adjustment_evidence"] = {
                "source": "corporate-actions-provider", "price_basis": "unadjusted",
                "checked_through": last_day, "actions": [{"date": last_day, "type": "split"}],
            }
            manifest, _ = load_snapshot(self.freeze())
            gate = manifest["data_validation"]
            self.assertFalse(gate["forecast_eligible"])
            self.assertIn("CORPORATE_ACTION_CONTAMINATION", gate["reason_codes"])

    def test_missing_strategy_audits_degrade_but_do_not_cancel_probability(self):
        manifest, _ = load_snapshot(self.freeze())
        result = researcher_result(
            "ml", manifest["ticker"], manifest["horizon"], manifest["snapshot_id"],
            status="partial", prob_up=0.62, probability_source="raw logistic output",
            raw_probability=0.62,
            validation={"leakage_audit": {
                "features_use_data_through_decision_close_only": True,
                "target_excluded_from_features": True,
            }, "final_holdout": {"observations": 12}},
        )
        consensus = build_consensus([result], manifest)
        reviewed = audit(manifest, [result], consensus)
        self.assertEqual(consensus["prob_up"], 0.62)
        self.assertIsNone(consensus["calibrated_probability"])
        self.assertEqual(consensus["reportable_probability"], 0.62)
        self.assertTrue(reviewed["may_report_direction"])
        self.assertEqual(reviewed["reporting_status"], "degraded")
        self.assertEqual(reviewed["strategy_eligibility"]["status"], "not_validated")
        self.assertFalse(reviewed["bias_audit"]["prediction_veto"])
        self.assertTrue(reviewed["bias_audit"]["strategy_veto"])
        self.assertIn("PBO_NOT_ASSESSED", reviewed["reason_codes"])
        self.assertIn("DSR_NOT_ASSESSED", reviewed["reason_codes"])
        self.assertIn("TRANSACTION_COST_NOT_ASSESSED", reviewed["reason_codes"])

    def test_unknown_reason_code_fails_closed_and_batch_telemetry_counts_abstentions(self):
        self.assertEqual(classify_reasons(["UNREGISTERED_GATE_CODE"])["status"], "veto")
        self.assertEqual(classify_reasons(["PBO_NOT_ASSESSED", "DSR_NOT_ASSESSED"])["status"], "pass")
        telemetry = aggregate_gate_telemetry([
            {"prediction_available": True, "reporting_status": "degraded"},
            {"prediction_available": False, "reporting_status": "veto"},
            {"prediction_available": True, "reporting_status": "pass"},
        ])
        self.assertEqual(telemetry["sample_count"], 3)
        self.assertAlmostEqual(telemetry["prediction_availability_rate"], 2 / 3, places=3)
        self.assertAlmostEqual(telemetry["abstain_rate"], 1 / 3, places=3)
        self.assertAlmostEqual(telemetry["degraded_rate"], 1 / 3, places=3)
        self.assertAlmostEqual(telemetry["pass_rate"], 1 / 3, places=3)

    def test_detected_leakage_vetoes_but_missing_calibration_does_not(self):
        manifest, _ = load_snapshot(self.freeze())
        result = researcher_result(
            "ml", manifest["ticker"], manifest["horizon"], manifest["snapshot_id"],
            status="partial", prob_up=0.62, probability_source="raw logistic output",
            raw_probability=0.62,
            validation={"leakage_audit": {
                "features_use_data_through_decision_close_only": False,
                "target_excluded_from_features": True,
            }},
        )
        consensus = build_consensus([result], manifest)
        self.assertEqual(consensus["path_assessments"]["ml"]["oos_validation"]["status"], "invalid")
        reviewed = audit(manifest, [result], consensus)
        self.assertFalse(reviewed["may_report_direction"])
        self.assertEqual(reviewed["reporting_status"], "veto")
        self.assertIn("LEAKAGE_DETECTED", reviewed["reason_codes"])


if __name__ == "__main__":
    unittest.main()