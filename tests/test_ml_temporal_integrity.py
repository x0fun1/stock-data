"""Temporal isolation and optional-dependency behavior for Research C."""

import builtins
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from quant_research.researchers import ml
from quant_research.snapshot import freeze_snapshot
from research_fixture import collection


class MLTemporalIntegrityTests(unittest.TestCase):
    def test_calibration_and_holdout_keep_strict_temporal_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "collection.json"
            source.write_text(json.dumps(collection()), encoding="utf-8")
            snapshot = freeze_snapshot(source, root / "snapshots")

            optional_imports = []
            original_import = builtins.__import__

            def block_optional_boosters(name, *args, **kwargs):
                root_name = name.split(".", 1)[0]
                if root_name in {"xgboost", "lightgbm"}:
                    optional_imports.append(root_name)
                    raise AssertionError(f"unexpected optional booster import: {root_name}")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=block_optional_boosters):
                result = ml.run(snapshot)

        validation = result["validation"]
        calibration = validation["calibration_training"]
        holdout = validation["final_holdout"]
        self.assertFalse(optional_imports)
        self.assertFalse(calibration["final_holdout_used"])
        self.assertEqual(calibration["input"], "strict causal walk-forward OOS predictions from the pre-holdout training range")
        self.assertIn(calibration["status"], {"calibrated", "uncalibrated"})
        if calibration["prediction_records"]:
            self.assertLess(calibration["latest_label_end_session"], holdout["start_session"])
            for row in calibration["prediction_records"]:
                self.assertLess(row["training_label_end_session"], row["decision_session"])
                self.assertLess(row["training_label_end_session"], holdout["start_session"])
                self.assertLess(row["label_end_session"], holdout["start_session"])
        if calibration["status"] == "calibrated":
            self.assertTrue(validation["probability_calibration"]["calibrated"])
            self.assertGreaterEqual(calibration["observations"], 40)
        else:
            self.assertEqual(validation["probability_calibration"]["method"], "none")

        self.assertTrue(holdout["one_time_chronological_holdout"])
        self.assertLess(holdout["training_label_end_session"], holdout["start_session"])
        self.assertGreater(holdout["oos_evaluable_samples"], 0)
        for row in holdout["oos_forecasts"]:
            self.assertGreaterEqual(row["decision_session"], holdout["start_session"])
            self.assertLess(row["training_label_end_session"], row["decision_session"])
            self.assertLessEqual(row["label_end_session"], "2024-03-15")
        self.assertEqual(validation["model_selection"]["configured_default"], "native deterministic L2 logistic regression")
        self.assertEqual(validation["model_selection"]["xgboost_status"], "not_implemented_not_loaded")
        self.assertEqual(validation["model_selection"]["lightgbm_status"], "not_implemented_not_loaded")
        self.assertFalse(validation["model_selection"]["boosted_models_enabled"])
        performance = validation["model_training_performance"]
        self.assertGreater(performance["attempts"], 0)
        self.assertLessEqual(performance["successful_fits"], performance["attempts"])
        self.assertGreater(performance["wall_seconds"], 0)
        self.assertGreaterEqual(performance["calibration_wall_seconds"], 0)
        self.assertEqual(validation["accelerated_models"]["xgboost"], "not_implemented_not_loaded")


if __name__ == "__main__":
    unittest.main()
