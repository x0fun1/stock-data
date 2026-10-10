"""Point-in-time boundaries for the default conditional-frequency path."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from quant_research.market_data import forward_returns
from quant_research.researchers.quant import _matured_matching_indices, run
from quant_research.snapshot import freeze_snapshot
from research_fixture import collection


class QuantTemporalIntegrityTests(unittest.TestCase):
    def test_future_prices_do_not_change_history_available_at_prior_decision(self):
        closes = [100.0]
        for index in range(1, 120):
            closes.append(closes[-1] * (1 + .002 * (1 if (index // 9) % 2 else -1) + .0001 * (index % 5)))
        decision, horizon = 75, 5
        before = _matured_matching_indices(closes, forward_returns(closes, horizon), decision, horizon)
        revised = list(closes)
        for index in range(decision + 1, len(revised)):
            revised[index] *= 1.5 + index / 10000
        after = _matured_matching_indices(revised, forward_returns(revised, horizon), decision, horizon)
        self.assertEqual(before, after)
        self.assertTrue(all(index + horizon <= decision for index in after))
        self.assertTrue(all(right > left + horizon for left, right in zip(after, after[1:])))

    def test_quant_oos_records_prove_training_labels_mature_before_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "collection.json"
            source.write_text(json.dumps(collection()), encoding="utf-8")
            snapshot = freeze_snapshot(source, root / "snapshots")
            result = run(snapshot)
        validation = result["validation"]
        self.assertEqual(validation["prior_policy"]["bayesian_shrinkage"], "not_implemented")
        self.assertEqual(validation["n_raw"], validation["n_effective"])
        self.assertEqual(validation["n_oos"], validation["oos_predictions"])
        self.assertEqual(validation["oos_predictions"], sum(row["probability"] is not None for row in validation["oos_forecasts"]))
        for row in validation["oos_forecasts"]:
            self.assertLessEqual(row["training_label_end_session"], row["decision_session"])
            self.assertLessEqual(row["label_end_session"], "2024-03-15")
            self.assertEqual(row["target_definition"], validation["target_definition"])
        self.assertEqual(validation["leakage_audit"]["training_labels_matured_by_decision"], True)


if __name__ == "__main__":
    unittest.main()
