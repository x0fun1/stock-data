"""Compatibility checks for snapshot schemas and older analysis artifacts."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
from quant_research.consensus import build_consensus
from quant_research.news.contracts import NewsInput
from quant_research.snapshot import freeze_snapshot, load_snapshot
from quant_research.synthesis import build_synthesis
from research_fixture import collection


class SchemaCompatibilityTests(unittest.TestCase):
    def test_current_snapshot_remains_schema_12_and_loadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "collection.json"
            source.write_text(json.dumps(collection()), encoding="utf-8")
            destination = freeze_snapshot(source, root / "snapshots")
            manifest, market = load_snapshot(destination)
            self.assertEqual(manifest["schema_version"], "1.2")
            self.assertTrue(market["data"]["bars"])

    def test_schema_10_and_11_snapshots_remain_readable_but_noneligible(self):
        for schema_version in ("1.0", "1.1"):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "collection.json"
                source.write_text(json.dumps(collection()), encoding="utf-8")
                destination = freeze_snapshot(source, root / "snapshots")
                manifest_path = destination / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["schema_version"] = schema_version
                manifest.pop("manifest_sha256", None)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                loaded, market = load_snapshot(destination)
                self.assertEqual(loaded["schema_version"], schema_version)
                self.assertTrue(market["data"]["bars"])
                self.assertFalse(loaded["data_validation"]["forecast_eligible"])

    def test_legacy_forecasts_default_to_equal_weight_and_missing_costs(self):
        identity = {"ticker": "TEST", "horizon": "5D", "snapshot_id": "legacy-snapshot"}
        manifest = {
            **identity,
            "data_validation": {"prediction_eligibility": {"status": "eligible"}, "forecast_eligible": True},
            "data_quality": {"overall": "medium", "confidence_score": 65},
        }
        researchers = [
            {"researcher_id": name, **identity, "status": "partial", "prob_up": probability,
             "probability_source": "legacy quantitative estimate", "validation": {}}
            for name, probability in (("quant", .2), ("factor", .4), ("ml", .6))
        ]
        consensus = build_consensus(researchers, manifest)
        self.assertAlmostEqual(consensus["prob_up"], .4)
        self.assertEqual(consensus["weighting"], "equal")
        self.assertEqual(consensus["oos_probability_quality"]["status"], "not_assessed")
        self.assertEqual(consensus["skill_weight_validation"]["status"], "fallback_equal_weight")
        self.assertFalse(consensus["skill_weight_validation"]["current_weighting_enabled"])

        old_consensus = {"prob_up": .42, "direction": "bearish", "confidence": "low", "agreement": "medium"}
        synthesis = build_synthesis(
            {"snapshot": identity, "consensus": old_consensus},
            {**identity, "status": "complete", "overall_direction": "positive", "sentiment": {}, "confidence": "low"},
        )
        self.assertEqual(synthesis["quant_prob_up"], .42)
        self.assertTrue(synthesis["quant_probability_preserved"])
        self.assertEqual(synthesis["economic_viability"]["status"], "not_assessed")

    def test_news_input_contract_excludes_new_quant_diagnostic_fields(self):
        fields = set(NewsInput.__dataclass_fields__)
        for name in ("economic_viability", "oos_probability_quality", "skill_weight_validation", "model_training_performance"):
            self.assertNotIn(name, fields)


if __name__ == "__main__":
    unittest.main()
