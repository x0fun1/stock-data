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
        self.assertIn(impact["magnitude"]["band"],
                      {"severe", "major", "moderate", "minor", "negligible", "not_assessed"})
        self.assertEqual(impact["breadth_scope"]["observed_breadth"], "not_assessed")
        self.assertEqual(impact["source_tier"]["tier"], "tier2_major_financial_news")
        self.assertIn(impact["magnitude"]["band"],
                      {"severe", "major", "moderate", "minor", "negligible", "not_assessed"})
        self.assertEqual(impact["magnitude"]["calibration_status"], "heuristic_uncalibrated")
        # News must never leak a probability into the impact block.
        for forbidden in ("prob_up", "reportable_probability", "confidence_score"):
            self.assertNotIn(forbidden, repr(impact))


class ReviewFixTests(unittest.TestCase):
    """Regressions for the four defects found in the 2.9.0 impact review."""

    def test_band_edges_are_symmetric_across_positive_and_negative_moves(self):
        # 90/100-1 is -0.09999999999999998 while 110/100-1 is +0.10000000000000009.
        # Without an explicit tolerance the same 10% move graded differently by sign.
        pairs = [(100.0, 90.0, "severe"), (100.0, 110.0, "severe"),
                 (100.0, 100.5, "minor"), (100.0, 99.5, "minor"),
                 (100.0, 95.0, "major"), (100.0, 105.0, "major"),
                 (100.0, 102.0, "moderate"), (100.0, 98.0, "moderate")]
        for base, moved, expected in pairs:
            with self.subTest(move=moved):
                self.assertEqual(impact.magnitude_band(moved / base - 1)["band"], expected)
        self.assertEqual(impact.magnitude_band(0.0049)["band"], "negligible")
        self.assertGreater(impact.magnitude_band(0.1)["band_tolerance"], 0)

    def test_trailing_sigma_uses_twenty_returns_not_nineteen(self):
        import statistics
        closes = [100.0 * (1.0 + 0.001 * i) for i in range(40)]
        index = 25
        segment = closes[index - 21:index]
        self.assertEqual(len(segment) - 1, 20)
        expected = statistics.stdev([segment[i] / segment[i - 1] - 1 for i in range(1, 21)])
        self.assertAlmostEqual(impact._trailing_sigma(closes, index), expected, places=12)

    def test_unknown_publishers_are_never_promoted_by_substring(self):
        for name in ("Security Finance Blog", "Capital.com", "Securities Times",
                     "AP News Blog", "Capital Markets Weekly"):
            with self.subTest(publisher=name):
                self.assertEqual(impact.source_tier([name])["tier"], "unclassified_publisher")

    def test_syndicated_credit_prefers_the_named_original_outlet(self):
        self.assertEqual(impact.source_tier(["Reuters - Yahoo Finance"])["tier"],
                         "tier2_major_financial_news")
        self.assertEqual(impact.source_tier(["Reuters via Yahoo"])["tier"],
                         "tier2_major_financial_news")
        self.assertEqual(impact.source_tier(["Yahoo Finance"])["tier"],
                         "tier4_analysis_and_aggregator")

    def test_exact_aliases_still_resolve_after_normalization(self):
        self.assertEqual(impact.source_tier(["  Reuters  "])["tier"], "tier2_major_financial_news")
        self.assertEqual(impact.source_tier(["Associated Press"])["tier"], "tier2_major_financial_news")
        self.assertEqual(impact.source_tier(["ChartMill"])["tier"], "tier3_specialized_real_time")
        self.assertEqual(impact.source_tier(["SEC"])["tier"], "tier1_official_primary")

    def test_match_rule_is_disclosed(self):
        self.assertEqual(impact.source_tier(["Reuters"])["match_rule"], "exact_normalized_alias_only")


class RetainedArtifactTests(unittest.TestCase):
    """Impact grading must survive a JSON cleanup, which keeps only Markdown."""

    def _run(self, root):
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

        responses_news = {"status": "complete", "actual_source": "Finnhub MCP",
                          "source_timestamp": value["captured_at_utc"],
                          "fetched_at_utc": value["captured_at_utc"],
                          "data": {"articles": [article(source="Reuters")]}}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with patch("yahoo_collect._calendar", return_value=market["session_calendar"]):
                responses = yahoo_collect.collect(request, provider=Provider())
            responses["news"] = responses_news
            receipt = StockDataAdapter().make_receipt(request, responses)
            snapshot = freeze_snapshot(
                yahoo_collect.write_responses(receipt, work / "collection.json"), work / "snapshots")
            report_dir = run_analysis(snapshot, work / "research")
            # Read inside the temp directory: a cleanup run keeps only the two
            # Markdown files, so this is exactly what survives cleanup.
            return (Path(report_dir / "report.md").read_text(encoding="utf-8"),
                    json.loads(Path(report_dir / "agent_summary.json").read_text(encoding="utf-8")))

    def test_markdown_and_summary_carry_impact_grade_and_limits(self):
        report_md, summary = self._run(None)
        for token in ("Impact grading", "magnitude", "source tier", "declared breadth"):
            self.assertIn(token, report_md)
        self.assertIn("composite impact score", report_md)
        self.assertIn("No composite impact score is produced", report_md)
        events = summary["sections"]["news"]["events"]
        self.assertTrue(events, "summary lost its event cards")
        impact = events[0]["impact"]
        self.assertIn(impact["magnitude_band"],
                      {"severe", "major", "moderate", "minor", "negligible", "not_assessed"})
        self.assertEqual(impact["composite_impact_score"], "not_implemented")
        self.assertEqual(impact["observed_breadth"], "not_assessed")
        self.assertEqual(impact["source_tier"], "tier2_major_financial_news")
        self.assertIn(impact["pattern"], {"amplified", "consistent", "dampened", "inverse", "not_assessed"})
        # Per-event caveats stay out of the bounded summary; limits are emitted once.
        self.assertNotIn("attribution_caveats", impact)
        marker = summary["sections"]["news"]["impact_rubric"]
        self.assertIn("no composite impact score", marker)
        self.assertIn("declared not observed", marker)


if __name__ == "__main__":
    unittest.main()