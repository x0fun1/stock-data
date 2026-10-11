"""Comparable OOS probability metrics and time-isolated consensus-weight diagnostics."""

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from quant_research.validation.probability_quality import (
    audit_oos_result,
    build_brier_skill_weight_diagnostic,
    build_oos_probability_quality,
    build_probability_validation,
)


def _fixture(*, changed_evaluation_targets=False, shifted_windows=False, abstain_one=False):
    rows = {name: [] for name in ("quant", "factor", "ml")}
    targets = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]
    start = date(2024, 1, 2)
    starts = {"quant": 0, "factor": 2 if shifted_windows else 0, "ml": 0}
    for name in rows:
        for i in range(starts[name], 10):
            decision = start + timedelta(days=i * 2)
            forecast_target = targets[i]
            target = forecast_target
            if changed_evaluation_targets and i >= 5:
                target = 1 - target
            if name == "quant":
                probability = .9 if forecast_target else .1
            elif name == "factor":
                probability = .8 if forecast_target else .2
            else:
                probability = .5
            if abstain_one and name == "factor" and i == 4:
                probability = None
            rows[name].append({
                "decision_session": decision.isoformat(),
                "label_end_session": (decision + timedelta(days=1)).isoformat(),
                "target": target,
                "probability": probability,
                "target_definition": "close[t+1] / close[t] - 1 > 0",
            })
    researchers = [{
        "researcher_id": name, "result_role": "forecast", "ticker": "TEST", "horizon": "1D", "snapshot_id": "snap",
        "validation": {"target_definition": "close[t+1] / close[t] - 1 > 0", "oos_forecasts": values},
    } for name, values in rows.items()]
    manifest = {"ticker": "TEST", "horizon": "1D", "snapshot_id": "snap",
                "latest_confirmed_close": {"session_date": "2024-02-10"}}
    return researchers, manifest


class ProbabilityQualityTests(unittest.TestCase):
    def test_frozen_oos_audit_rejects_wrong_labels_training_boundary_and_duplicates(self):
        bars = [{"date": (date(2024, 1, 1) + timedelta(days=i)).isoformat(), "close": close}
                for i, close in enumerate((100, 102, 101, 103, 100, 104))]
        manifest = {"ticker": "TEST", "horizon": "1D", "snapshot_id": "snap",
                    "latest_confirmed_close": {"session_date": bars[-1]["date"]}}
        valid = {"decision_session": bars[2]["date"], "label_end_session": bars[3]["date"],
                 "target": 1, "probability": .7,
                 "training_label_end_session": bars[1]["date"],
                 "target_definition": "close[t+1] / close[t] - 1 > 0"}
        tampered = [
            {**valid, "decision_session": bars[3]["date"], "label_end_session": bars[4]["date"], "target": 1},
            {**valid, "decision_session": bars[4]["date"], "label_end_session": bars[4]["date"], "target": 1},
            {**valid, "decision_session": bars[1]["date"], "label_end_session": bars[2]["date"],
             "target": 0, "training_label_end_session": bars[3]["date"]},
            {**valid},
        ]
        result = {"researcher_id": "quant", "result_role": "forecast", **{key: manifest[key]
                  for key in ("ticker", "horizon", "snapshot_id")},
                  "validation": {"oos_forecasts": [valid, *tampered]}}
        audit = audit_oos_result(result, manifest, bars)
        self.assertEqual(audit["valid_prediction_count"], 0)
        self.assertEqual(audit["rejected_samples"], 5)
        self.assertIn("duplicate_decision_session", audit["rejection_reasons"])
        self.assertIn("label_horizon_does_not_match_frozen_sessions", audit["rejection_reasons"])
        self.assertIn("training_label_not_matured_by_decision", audit["rejection_reasons"])
        self.assertIn("target_does_not_match_frozen_close_prices", audit["rejection_reasons"])
        quality = build_oos_probability_quality([result], manifest, bars=bars)
        self.assertEqual(quality["paths"], {})

    def test_current_path_with_no_valid_oos_cannot_disappear_from_common_comparison(self):
        researchers, manifest = _fixture()
        researchers[1]["validation"]["oos_forecasts"] = []
        quality = build_oos_probability_quality(researchers, manifest)
        self.assertEqual(quality["current_forecast_paths"], ["factor", "ml", "quant"])
        self.assertEqual(quality["common_oos"]["common_oos_samples"], 0)
        self.assertEqual(quality["common_oos"]["missing_path_ids"], ["factor"])
        self.assertEqual(quality["status"], "partial")

    def test_detected_leakage_does_not_support_policy_replay_skill(self):
        bars = [{"date": (date(2024, 1, 1) + timedelta(days=i)).isoformat(), "close": 100 + i}
                for i in range(8)]
        manifest = {"ticker": "TEST", "horizon": "1D", "snapshot_id": "snap",
                    "latest_confirmed_close": {"session_date": bars[-1]["date"]}}
        rows = [{"decision_session": bars[i]["date"], "label_end_session": bars[i + 1]["date"],
                 "target": 1, "training_label_end_session": bars[1]["date"],
                 "target_definition": "close[t+1] / close[t] - 1 > 0"}
                for i in (3, 4, 5)]
        paths = [{"researcher_id": name, "result_role": "forecast", "ticker": "TEST",
                  "horizon": "1D", "snapshot_id": "snap", "reason_codes": reasons,
                  "validation": {"oos_forecasts": [{**row, "probability": probability} for row in rows]}}
                 for name, probability, reasons in (("quant", .5, []),
                                                    ("ml", 1.0, ["LEAKAGE_DETECTED"]))]
        quality = build_oos_probability_quality(paths, manifest, bars=bars)
        replay = quality["algorithm_policy_replay"]
        self.assertEqual(replay["predicted_dates"], 3)
        self.assertIn("union_of_submitted_valid_dated_oos_records", replay["coverage_denominator_scope"])
        self.assertAlmostEqual(replay["forecast_metrics"]["brier_score"], .25)
        self.assertTrue(all(row["available_path_ids"] == ["quant"] for row in replay["active_path_mask"]))

    def test_quality_reports_brier_logloss_reliability_coverage_and_abstention(self):
        researchers, manifest = _fixture(abstain_one=True)
        result = build_oos_probability_quality(researchers, manifest)
        metrics = result["paths"]["factor"]
        self.assertEqual(metrics["all_evaluable_oos_samples"], 10)
        self.assertEqual(metrics["valid_probability_predictions"], 9)
        self.assertEqual(metrics["oos_coverage"], 9 / 10)
        self.assertAlmostEqual(metrics["abstain_rate"], 1 / 10)
        self.assertIsNotNone(metrics["brier_score"])
        self.assertIsNotNone(metrics["log_loss"])
        self.assertEqual(len(metrics["reliability_diagram"]), 10)
        self.assertEqual(metrics["calibration_uncertainty"]["interval_method"], "not_computed")
        self.assertEqual(result["common_oos"]["common_oos_samples"], 10)
        self.assertEqual(result["common_oos"]["all_paths_valid_prediction_samples"], 9)

    def test_skill_weights_use_an_earlier_estimation_window_then_later_evaluation(self):
        researchers, manifest = _fixture()
        result = build_brier_skill_weight_diagnostic(researchers, manifest, epsilon=.05, minimum_per_window=3)
        self.assertEqual(result["status"], "exploratory_temporal_validation")
        self.assertEqual(result["common_oos_samples"], 10)
        self.assertEqual(result["estimation_samples"], 5)
        self.assertEqual(result["evaluation_samples"], 5)
        self.assertLess(result["weight_estimation_label_end"], result["evaluation_start"])
        self.assertAlmostEqual(sum(result["candidate_weights"].values()), 1.0)
        self.assertGreater(result["candidate_weights"]["quant"], result["candidate_weights"]["ml"])
        self.assertFalse(result["current_weighting_enabled"])
        self.assertEqual(result["deployment_status"], "disabled_pending_independent_real_snapshot_oos_and_performance_acceptance")

    def test_later_evaluation_labels_cannot_change_estimated_weights(self):
        first, manifest = _fixture()
        changed, _ = _fixture(changed_evaluation_targets=True)
        before = build_brier_skill_weight_diagnostic(first, manifest, minimum_per_window=3)
        after = build_brier_skill_weight_diagnostic(changed, manifest, minimum_per_window=3)
        self.assertEqual(before["candidate_weights"], after["candidate_weights"])
        self.assertEqual(before["path_brier"], after["path_brier"])
        self.assertNotEqual(before["evaluation"]["weighted_brier"], after["evaluation"]["weighted_brier"])

    def test_nonmatching_oos_windows_use_only_the_date_intersection(self):
        researchers, manifest = _fixture(shifted_windows=True)
        quality = build_oos_probability_quality(researchers, manifest)
        weights = build_brier_skill_weight_diagnostic(researchers, manifest, minimum_per_window=3)
        self.assertEqual(quality["common_oos"]["common_oos_samples"], 8)
        self.assertEqual(weights["common_oos_samples"], 8)
        self.assertEqual(weights["status"], "exploratory_temporal_validation")
        self.assertEqual(weights["weight_estimation_start"], "2024-01-06")

    def test_old_results_without_optional_oos_records_degrade_to_equal_weight(self):
        researchers = [{"researcher_id": "quant", "ticker": "TEST", "horizon": "1D", "snapshot_id": "snap", "prob_up": .6}]
        manifest = {"ticker": "TEST", "horizon": "1D", "snapshot_id": "snap"}
        quality, weights = build_probability_validation(researchers, manifest)
        self.assertEqual(quality["status"], "not_assessed")
        self.assertEqual(weights["weighting"], "equal")
        self.assertFalse(weights["current_weighting_enabled"])
        self.assertIsNone(weights["baseline_brier"])


if __name__ == "__main__":
    unittest.main()
