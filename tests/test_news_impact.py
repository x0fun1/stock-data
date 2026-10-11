"""Deterministic News impact grading; no network, no Quant input, no composite score."""

from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))

from quant_research.news import impact


def _walk(length: int = 80, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    price, closes = 100.0, []
    for _ in range(length):
        price *= (1 + rng.gauss(0.0004, 0.015))
        closes.append(price)
    return closes


class MagnitudeTests(unittest.TestCase):
    def test_bands_are_ordered_and_missing_return_is_not_assessed(self):
        self.assertEqual(impact.magnitude_band(0.12)["band"], "severe")
        self.assertEqual(impact.magnitude_band(0.06)["band"], "major")
        self.assertEqual(impact.magnitude_band(0.021)["band"], "moderate")
        self.assertEqual(impact.magnitude_band(0.006)["band"], "minor")
        self.assertEqual(impact.magnitude_band(0.001)["band"], "negligible")
        self.assertEqual(impact.magnitude_band(0.0)["band"], "negligible")
        for missing in (None, float("nan"), float("inf")):
            self.assertEqual(impact.magnitude_band(missing)["band"], "not_assessed")

    def test_every_band_is_disclosed_as_uncalibrated(self):
        self.assertEqual(impact.magnitude_band(0.06)["calibration_status"], "heuristic_uncalibrated")


class PatternTests(unittest.TestCase):
    def test_gradient_from_amplified_through_dampened(self):
        closes = _walk()
        expected = {0.06: "amplified", 0.012: "consistent", 0.002: "dampened"}
        for observed, band in expected.items():
            result = impact.pattern_comparison(
                event_direction="positive",
                reaction={"status": "confirmed_positive", "daily_close_return": observed},
                closes=closes, session_index=60)
            self.assertEqual(result["pattern"], band, observed)

    def test_divergence_is_inverse_and_intraday_is_not_assessed(self):
        closes = _walk()
        opposed = impact.pattern_comparison(
            event_direction="positive",
            reaction={"status": "event_price_divergence_negative", "daily_close_return": -0.02},
            closes=closes, session_index=60)
        self.assertEqual(opposed["pattern"], "inverse")
        intraday = impact.pattern_comparison(
            event_direction="positive",
            reaction={"status": "intraday_daily_association", "daily_close_return": 0.09},
            closes=closes, session_index=60)
        self.assertEqual(intraday["pattern"], "not_assessed")

    def test_degenerate_or_short_history_never_reports_amplified(self):
        constant = [100 * (1.01 ** i) for i in range(40)]
        self.assertEqual(impact.pattern_comparison(
            event_direction="positive",
            reaction={"status": "confirmed_positive", "daily_close_return": 0.06},
            closes=constant, session_index=30)["pattern"], "not_assessed")
        self.assertEqual(impact.pattern_comparison(
            event_direction="positive",
            reaction={"status": "confirmed_positive", "daily_close_return": 0.06},
            closes=[100.0, 101.0, 102.0], session_index=2)["pattern"], "not_assessed")

    def test_unusable_reaction_and_missing_session_are_not_assessed(self):
        closes = _walk()
        self.assertEqual(impact.pattern_comparison(
            event_direction="positive", reaction={"status": "unavailable"},
            closes=closes, session_index=60)["pattern"], "not_assessed")
        self.assertEqual(impact.pattern_comparison(
            event_direction="positive",
            reaction={"status": "confirmed_positive", "daily_close_return": 0.06},
            closes=closes, session_index=None)["pattern"], "not_assessed")


class ScopeAndTierTests(unittest.TestCase):
    def test_breadth_is_declared_scope_not_observed(self):
        self.assertEqual(impact.breadth_scope("MACRO")["declared_scope"], "cross_asset_candidate")
        self.assertEqual(impact.breadth_scope("EARNINGS")["declared_scope"], "company_and_sector_candidate")
        self.assertEqual(impact.breadth_scope("LEGAL")["declared_scope"], "company_specific_candidate")
        self.assertEqual(impact.breadth_scope(None)["declared_scope"], "company_specific_candidate")
        for event_type in ("MACRO", "EARNINGS", "LEGAL"):
            self.assertEqual(impact.breadth_scope(event_type)["observed_breadth"], "not_assessed")

    def test_source_tiers_never_promote_unknown_publishers(self):
        self.assertEqual(impact.source_tier(["Reuters"])["tier"], "tier2_major_financial_news")
        self.assertEqual(impact.source_tier(["ChartMill"])["tier"], "tier3_specialized_real_time")
        self.assertEqual(impact.source_tier(["Yahoo Finance"])["tier"], "tier4_analysis_and_aggregator")
        self.assertEqual(impact.source_tier(["SomeUnknownBlog"])["tier"], "unclassified_publisher")
        self.assertEqual(impact.source_tier([])["tier"], "unclassified_publisher")

    def test_weakest_publisher_in_a_cluster_wins(self):
        result = impact.source_tier(["Reuters", "ChartMill", "UnknownWire"])
        self.assertEqual(result["tier"], "unclassified_publisher")


class ContractTests(unittest.TestCase):
    def test_bundle_never_emits_a_composite_score_or_probability(self):
        bundle = impact.assess(
            event_type="EARNINGS", event_direction="positive",
            reaction={"status": "confirmed_positive", "daily_close_return": 0.06},
            publishers=["Reuters"], closes=_walk(), session_index=60)
        self.assertEqual(bundle["composite_impact_score"], "not_implemented")
        self.assertEqual(bundle["breadth_scope"]["observed_breadth"], "not_assessed")
        self.assertIn("calibration_status", bundle["magnitude"])
        flat = repr(bundle)
        for forbidden in ("prob_up", "probability", "confidence_score"):
            self.assertNotIn(forbidden, flat)

    def test_attribution_caveats_are_always_present(self):
        bundle = impact.assess(
            event_type="MACRO", event_direction="negative",
            reaction={"status": "unavailable"}, publishers=[], closes=[], session_index=None)
        self.assertTrue(bundle["attribution_caveats"])
        self.assertTrue(all(isinstance(item, str) for item in bundle["attribution_caveats"]))


class PipelineIntegrationTests(unittest.TestCase):
    """The frozen News result must carry the impact block and stay probability-free."""

    def test_event_card_exposes_impact_without_any_composite_or_probability(self):
        import copy
        import json
        import tempfile
        from unittest.mock import patch

        sys.path.insert(0, str(ROOT / "tests"))
        from quant_research.orchestrator import run_analysis
        from quant_research.snapshot import freeze_snapshot
        from quant_research.stock_data_adapter import StockDataAdapter
        import yahoo_collect
        from research_fixture import article, collection

        value = collection(140)
        market = copy.deepcopy(value["domains"]["market"])
        market["status"] = "success"
        market["source_symbol"] = "INTC"
        market["provider_library"] = "yfinance"
        market["provider_version"] = "fixture"
        market["data"]["bars"][-1]["timestamp_utc"] = market["source_timestamp"]
        request = copy.deepcopy(value["request"])
        request["history_window"] = {"start": market["data"]["bars"][0]["date"],
                                     "end": market["data"]["bars"][-1]["date"]}

        class Provider:
            def call(self, name, **params):
                return copy.deepcopy(market)

        # News now arrives from the host's Finnhub MCP, not from the collector.
        responses_news = {"status": "complete", "actual_source": "Finnhub MCP",
                          "source_timestamp": value["captured_at_utc"],
                          "fetched_at_utc": value["captured_at_utc"],
                          "data": {"articles": [article(source="Reuters")]}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("yahoo_collect._calendar", return_value=market["session_calendar"]):
                responses = yahoo_collect.collect(request, provider=Provider())
            responses["news"] = responses_news
            receipt = StockDataAdapter().make_receipt(request, responses)
            snapshot = freeze_snapshot(yahoo_collect.write_responses(receipt, root / "collection.json"),
                                       root / "snapshots")
            report = run_analysis(snapshot, root / "research")
            news = json.loads((report / "news_result.json").read_text(encoding="utf-8"))

        self.assertEqual(news["status"], "complete")
        cards = news["major_events"]
        self.assertTrue(cards, "fixture produced no eligible event card")
        impact = cards[0]["impact"]
        self.assertEqual(impact["composite_impact_score"], "not_implemented")
        self.assertEqual(impact["breadth_scope"]["observed_breadth"], "not_assessed")
        self.assertEqual(impact["source_tier"]["tier"], "tier2_major_financial_news")
        self.assertIn(impact["magnitude"]["band"],
                      {"severe", "major", "moderate", "minor", "negligible", "not_assessed"})
        self.assertEqual(impact["magnitude"]["calibration_status"], "heuristic_uncalibrated")
        # News must never leak a probability into the impact block.
        for forbidden in ("prob_up", "reportable_probability", "confidence_score"):
            self.assertNotIn(forbidden, repr(impact))


if __name__ == "__main__":
    unittest.main()