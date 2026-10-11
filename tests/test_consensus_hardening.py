"""Malformed path inputs cannot duplicate votes or bypass explicit assessments."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))

from quant_research.consensus import build_consensus, forecast_assessment
from quant_research.contracts import researcher_result


class ConsensusHardeningTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"ticker": "TEST", "horizon": "1D", "snapshot_id": "snap",
                         "data_validation": {"forecast_eligible": True},
                         "data_quality": {"confidence_score": 100}}

    def path(self, name, probability, family="momentum"):
        return researcher_result(
            name, "TEST", "1D", "snap", status="success", prob_up=probability,
            probability_source="synthetic test estimate", evidence={"positive": [{"family": family}], "negative": []},
            validation={"oos_predictions": 100, "holdout_predictions": 100,
                        "final_holdout": {"observations": 100},
                        "probability_calibration": {"calibrated": True},
                        "leakage_audit": {"features_use_data_through_decision_close_only": True,
                                          "future_returns_used_only_as_labels": True}},
        )

    def test_missing_assessment_excludes_path_without_crashing(self):
        quant, factor = self.path("quant", .8), self.path("factor", .2)
        result = build_consensus([quant, factor], self.manifest,
                                 assessments={"factor": forecast_assessment(factor, self.manifest)})
        self.assertEqual(result["path_ids"], ["factor"])
        self.assertEqual(result["prob_up"], .2)
        self.assertEqual(result["path_input_rejections"][0]["reason"], "path_assessment_missing_or_invalid")
        unavailable = build_consensus([quant], self.manifest, assessments={})
        self.assertEqual(unavailable["status"], "unavailable")

    def test_duplicate_ids_exclude_all_ambiguous_records_without_first_or_last_bias(self):
        first, conflicting = self.path("quant", .8), self.path("quant", .1)
        factor = self.path("factor", .2)
        for paths in ([first, factor, conflicting], [conflicting, factor, first]):
            result = build_consensus(paths, self.manifest)
            self.assertEqual(result["available_paths"], 1)
            self.assertEqual(result["path_ids"], ["factor"])
            self.assertEqual(result["prob_up"], .2)
            self.assertEqual(result["path_input_rejections"][0]["reason"], "duplicate_researcher_id")

    def test_nonfinite_probability_cannot_enter_with_overoptimistic_external_assessment(self):
        result = build_consensus([{**self.path("quant", .5), "prob_up": float("nan")}], self.manifest,
                                 assessments={"quant": {"forecast_eligible": True}})
        self.assertEqual(result["status"], "unavailable")

    def test_external_assessment_cannot_erase_declared_feature_leakage(self):
        path = self.path("ml", .6)
        path["validation"]["leakage_audit"]["features_use_data_through_decision_close_only"] = False
        result = build_consensus([path], self.manifest, assessments={"ml": {"forecast_eligible": True}})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["oos_probability_quality"]["current_forecast_paths"], [])

    def test_best_current_evidence_score_is_capped_by_two_disclosed_penalties(self):
        paths = [self.path(name, .6, family) for name, family in
                 (("quant", "momentum"), ("factor", "trend"), ("ml", "volatility"))]
        quality = {"algorithm_policy_replay": {"predicted_dates": 100,
                                               "brier_skill_vs_expanding_base_rate": .2}}
        with patch("quant_research.consensus.build_probability_validation", return_value=(quality, {})):
            best = build_consensus(paths, self.manifest)
            typical = build_consensus(paths, {**self.manifest, "data_quality": {"confidence_score": 85}})
        self.assertEqual(best["confidence_score"], 80)
        self.assertEqual(typical["confidence_score"], 65)
        self.assertEqual(best["confidence_reasons"], ["ensemble_calibration_unavailable",
                                                     "policy_oos_skill_uncertainty_unestimated"])


if __name__ == "__main__":
    unittest.main()
