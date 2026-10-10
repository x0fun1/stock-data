"""OOS time-series IC inference and explicit multiplicity scope."""

import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from quant_research.math_utils import (benjamini_yekutieli,
                                       circular_block_bootstrap_spearman,
                                       circular_shift_spearman_p_value)


class FactorInferenceTests(unittest.TestCase):
    def test_benjamini_yekutieli_matches_reference_and_counts_full_trial_family(self):
        adjusted = benjamini_yekutieli({"a": .01, "b": .03, "c": .04, "unavailable": None}, trials=4)
        harmonic = 1 + 1 / 2 + 1 / 3 + 1 / 4
        self.assertAlmostEqual(adjusted["a"], .01 * 4 * harmonic)
        self.assertAlmostEqual(adjusted["b"], .04 * 4 * harmonic / 3)
        self.assertAlmostEqual(adjusted["c"], .04 * 4 * harmonic / 3)
        self.assertIsNone(adjusted["unavailable"])

    def test_block_bootstrap_is_reproducible_and_labels_interval_as_approximate(self):
        x = [math.sin(i / 4) + i / 500 for i in range(80)]
        y = [math.sin((i - 2) / 4) + .01 * math.cos(i) for i in range(80)]
        first = circular_block_bootstrap_spearman(x, y, block_length=5, replicates=80, seed=19)
        second = circular_block_bootstrap_spearman(x, y, block_length=5, replicates=80, seed=19)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "exploratory")
        self.assertEqual(first["method"], "circular_moving_block_bootstrap_percentile")
        self.assertFalse(first["coverage_validated"])
        self.assertEqual(len(first["interval"]), 2)

    def test_circular_shift_p_value_discloses_untested_stationarity_assumption(self):
        x = [math.sin(i / 4) for i in range(80)]
        y = [math.sin((i - 2) / 4) + .01 * math.cos(i) for i in range(80)]
        result = circular_shift_spearman_p_value(x, y, block_length=5)
        self.assertEqual(result["status"], "exploratory")
        self.assertEqual(result["method"], "circular_shift_test_stationarity_assumption")
        self.assertFalse(result["stationarity_assumption_validated"])
        self.assertEqual(result["permutations"], 71)

    def test_insufficient_window_does_not_emit_adjusted_significance(self):
        adjusted = benjamini_yekutieli({"a": None, "b": None}, trials=2)
        self.assertEqual(adjusted, {"a": None, "b": None})
        result = circular_shift_spearman_p_value(list(range(12)), list(range(12)), block_length=5)
        self.assertIsNone(result["p_value"])
        self.assertEqual(result["status"], "insufficient_samples")


if __name__ == "__main__":
    unittest.main()
