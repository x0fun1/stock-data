"""Offline contract tests for the post-Quant News/Sentiment stage."""

from __future__ import annotations

from dataclasses import fields
from datetime import date, timedelta
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))

from quant_research.contracts import researcher_result
from quant_research.news import analyze_news, prepare_news_input, run_news
from quant_research.news.classify import direction_for
from quant_research.news.contracts import NewsInput
from quant_research.news.decay import time_decay
from quant_research.news.deduplicate import deduplicate_articles
from quant_research.news.normalize import normalize_articles, normalize_source_sentiment
from quant_research.news.reaction import validate_reaction
from quant_research.orchestrator import run_analysis
from quant_research.snapshot import DOMAINS, freeze_snapshot
from quant_research.synthesis import build_synthesis


ASOF = "2024-03-15T21:00:00Z"


def make_bars(count: int = 55) -> list[dict[str, object]]:
    sessions: list[date] = []
    cursor = date(2024, 3, 15)
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    sessions.reverse()
    bars: list[dict[str, object]] = []
    previous = 100.0
    for index, day in enumerate(sessions):
        close = 100.0 + index * 0.25
        bars.append({
            "date": day.isoformat(), "open": previous, "high": close + 1,
            "low": min(previous, close) - 1, "close": close,
            "volume": 1000 + index * 7,
        })
        previous = close
    return bars


def article(
    title: str,
    *,
    published_at: str = "2024-03-14T21:00:00Z",
    source: str = "Wire A",
    record_type: str = "event",
    article_id: str | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "article_id": article_id,
        "url": None,
        "title": title,
        "summary": None,
        "source": source,
        "source_type": "social" if record_type == "social" else "news",
        "record_type": record_type,
        "published_at": published_at,
        "tickers": ["INTC"],
        **extra,
    }


def sample_articles() -> list[dict[str, object]]:
    return [
        article(
            "Intel raises guidance on strong AI demand", article_id="wire-1", source="Wire A",
            event_type="GUIDANCE_RAISE", event_strength=0.8, relevance=0.9,
            magnitude=0.7, novelty=0.75, source_quality=0.8,
        ),
        article(
            "Intel raises guidance after strong AI demand", article_id="wire-2", source="Wire B",
            event_type="GUIDANCE_RAISE", event_strength=0.8, relevance=0.9,
            magnitude=0.7, novelty=0.75, source_quality=0.8,
        ),
        article(
            "INTC is bullish after strong demand", source="Forum", record_type="social",
            published_at="2024-03-14T19:00:00Z",
        ),
        article(
            "Intel cuts guidance after weak demand", source="Late Wire", article_id="future",
            published_at="2024-03-15T22:00:00Z",
        ),
        article(
            "Intel guidance was cut", source="Updated Wire", article_id="updated",
            updated_at="2024-03-15T22:00:00Z",
        ),
        article("Un-timestamped Intel headline", source="No Date", published_at=""),
    ]


class NewsFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot_dir = self._make_snapshot(sample_articles())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_snapshot(self, articles: list[dict[str, object]], source_sentiment: dict[str, object] | None = None) -> Path:
        domains = {name: {"status": "unavailable", "actual_source": None, "data": None} for name in DOMAINS}
        domains["market"] = {
            "status": "complete", "actual_source": "fixture", "adjustment": "adjusted",
            "last_bar_closed": True,
        "data": {"bars": make_bars(), "history_window": {"start": "2024-01-22", "end": "2024-03-15", "selection": "fixture"}},
        }
        news_data: dict[str, object] = {"articles": articles}
        if source_sentiment is not None:
            news_data["source_sentiment"] = source_sentiment
        domains["news"] = {"status": "complete", "actual_source": "fixture", "fetched_at_utc": ASOF, "data": news_data}
        collected = self.root / f"collection-{len(list(self.root.iterdir()))}.json"
        collected.write_text(json.dumps({
            "request": {"ticker": "INTC", "market": "US", "horizon": "5D", "asof": ASOF, "mode": "standard"},
            "captured_at_utc": ASOF,
            "domains": domains,
        }), encoding="utf-8")
        return freeze_snapshot(collected, self.root / "snapshots")


class NewsPointInTimeTests(NewsFixture):
    def test_future_updated_and_untimestamped_records_are_excluded(self):
        normalized, excluded, _ = normalize_articles(
            {"articles": sample_articles()}, ticker="INTC", asof_timestamp=ASOF,
        )
        self.assertEqual(len(normalized), 3)
        self.assertEqual(excluded["after_asof"], 1)
        self.assertEqual(excluded["updated_after_asof"], 1)
        self.assertEqual(excluded["missing_or_invalid_published_at"], 1)
        self.assertTrue(all(row["published_at"] <= ASOF for row in normalized))

    def test_signed_source_sentiment_is_preserved_without_event_score_clamping(self):
        normalized, _, _ = normalize_articles({"articles": [article(
            "INTC looks weak", record_type="social", sentiment_score=-0.44,
        )]}, ticker="INTC", asof_timestamp=ASOF)
        self.assertEqual(normalized[0]["sentiment_score"], -0.44)
        self.assertIsNone(normalized[0]["event_strength"])

    def test_half_life_can_be_overridden_by_event_type(self):
        decay = time_decay(
            "2024-03-14T21:00:00Z", make_bars(), "GUIDANCE", "5D",
            {"half_life_sessions_by_event_type": {"GUIDANCE": 1}},
        )
        self.assertEqual(decay["half_life_sessions"], 1)
        self.assertAlmostEqual(decay["time_decay"], 0.5)

    def test_empty_news_is_not_assessed_instead_of_inferred(self):
        snapshot_dir = self._make_snapshot([])
        result = analyze_news(prepare_news_input(snapshot_dir))
        self.assertEqual(result["status"], "not_assessed")
        self.assertEqual(result["overall_direction"], "unknown")
        self.assertEqual(result["market_confirmation"], "not_assessed")


class NewsDeduplicationTests(NewsFixture):
    def test_syndicated_headlines_become_one_canonical_cluster(self):
        news_input = prepare_news_input(self.snapshot_dir)
        clusters, duplicate_count = deduplicate_articles(list(news_input.articles))
        events = [cluster for cluster in clusters if not cluster["is_opinion"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["article_count"], 2)
        self.assertEqual(events[0]["sources"], ["Wire A", "Wire B"])
        self.assertEqual(duplicate_count, 1)

    def test_same_url_with_conflicting_record_roles_does_not_cross_merge(self):
        records, _, _ = normalize_articles({"articles": [
            article("Intel raises guidance", record_type="event", article_id="fact", url="https://example.test/story"),
            article("Intel raises guidance", record_type="social", source="Forum", article_id="opinion", url="https://example.test/story"),
        ]}, ticker="INTC", asof_timestamp=ASOF)
        clusters, duplicate_count = deduplicate_articles(records)
        self.assertEqual(duplicate_count, 0)
        self.assertEqual({cluster["role"] for cluster in clusters}, {"event", "opinion"})


class EventOpinionTests(NewsFixture):
    def test_event_facts_and_social_opinion_are_separate(self):
        result = analyze_news(prepare_news_input(self.snapshot_dir))
        self.assertEqual(result["coverage"]["canonical_event_count"], 1)
        self.assertEqual(result["coverage"]["canonical_opinion_count"], 1)
        self.assertEqual(result["major_events"][0]["event_type"], "GUIDANCE")
        self.assertEqual(result["major_events"][0]["direction"], "positive")
        self.assertEqual(result["sentiment"]["label"], "moderately_bullish")
        self.assertAlmostEqual(
            result["major_events"][0]["effective_event_score"],
            0.8 * 0.9 * 0.75 * 0.8 * result["major_events"][0]["time_decay"],
        )
        self.assertNotIn("prob_up", result)

    def test_provider_aggregate_is_kept_separate_from_opinion_text_score(self):
        snapshot_dir = self._make_snapshot(sample_articles(), {
            "source": "Finnhub", "period": "7d", "sentiment_source": "Finnhub",
            "sentiment_score": 0.42, "bullish_percent": 0.64, "bearish_percent": 0.36,
        })
        result = analyze_news(prepare_news_input(snapshot_dir))
        self.assertEqual(result["sentiment"]["evidence_basis"], "opinion_text_lexicon")
        self.assertEqual(result["sentiment"]["score"], 1.0)
        self.assertEqual(result["sentiment"]["provider_aggregate"]["sentiment_score"], 0.42)

    def test_ambiguous_record_is_not_silently_counted_as_event_or_opinion(self):
        snapshot_dir = self._make_snapshot([
            article("Intel draws attention from investors", record_type="unknown", article_id="ambiguous"),
        ])
        result = analyze_news(prepare_news_input(snapshot_dir))
        self.assertEqual(result["coverage"]["canonical_event_count"], 0)
        self.assertEqual(result["coverage"]["canonical_opinion_count"], 0)
        self.assertEqual(result["coverage"]["unclassified_record_count"], 1)
        self.assertEqual(result["unclassified_records"][0]["headline"], "Intel draws attention from investors")

    def test_conflicting_event_and_opinion_evidence_is_kept_mixed(self):
        snapshot_dir = self._make_snapshot([
            article("Intel raises guidance on strong demand", record_type="event", article_id="event"),
            article("INTC is bearish after weak demand", record_type="social", source="Forum", article_id="opinion"),
        ])
        news = analyze_news(prepare_news_input(snapshot_dir))
        synthesis = build_synthesis({"consensus": {"direction": "bullish", "prob_up": 0.62, "confidence": "medium"}}, news)
        self.assertEqual(news["overall_direction"], "positive")
        self.assertEqual(news["sentiment"]["consensus_direction"], "negative")
        self.assertEqual(synthesis["alignment"], "MIXED")
        self.assertEqual(synthesis["alignment_basis"], "event_sentiment_conflict")


class NewsIsolationTests(NewsFixture):
    def test_news_input_and_analyst_signature_have_no_quant_fields(self):
        input_names = {item.name for item in fields(NewsInput)}
        self.assertEqual(input_names, {
            "ticker", "market", "horizon", "asof_timestamp", "snapshot_id",
            "news_status", "source_sentiment", "articles", "bars", "warnings",
        })
        signature = inspect.signature(analyze_news)
        self.assertEqual(list(signature.parameters), ["news_input", "policy"])
        self.assertNotIn("quant", repr(prepare_news_input(self.snapshot_dir)).lower())

    def test_provider_aggregate_is_preserved_without_rescaling(self):
        raw = {"source_sentiment": {
            "source": "Finnhub", "period": "7d", "sentiment_source": "Finnhub",
            "sentiment_score": 0.42, "bullish_percent": 0.64, "bearish_percent": 0.36,
        }}
        normalized, warnings = normalize_source_sentiment(raw, available_at=ASOF, asof_timestamp=ASOF)
        self.assertEqual(normalized["sentiment_score"], 0.42)
        self.assertEqual(normalized["bullish_percent"], 0.64)
        self.assertEqual(warnings, [])
        self.assertIsNone(normalize_source_sentiment({"source_sentiment": {}}, available_at=None, asof_timestamp=ASOF)[0])
        future, future_warnings = normalize_source_sentiment(
            raw, available_at="2024-03-15T22:00:00Z", asof_timestamp=ASOF,
        )
        self.assertIsNone(future)
        self.assertIn("after the snapshot as-of", future_warnings[0])


class MarketValidationTests(NewsFixture):
    def test_reaction_uses_supplied_frozen_bars_and_maps_after_close_forward(self):
        bars = make_bars()
        published_at = "2024-03-14T22:30:00Z"  # after the US local close
        reaction = validate_reaction(
            published_at=published_at, event_direction="positive", market="US", bars=bars,
        )
        self.assertEqual(reaction["market_session_date"], "2024-03-15")
        self.assertEqual(reaction["price_direction"], "positive")
        self.assertEqual(reaction["status"], "confirmed_positive")
        self.assertEqual(reaction["sector_relative_return"], None)

    def test_snapshot_pipeline_uses_frozen_market_and_has_no_provider_import(self):
        result = analyze_news(prepare_news_input(self.snapshot_dir))
        self.assertEqual(result["method"]["market_data"], "snapshot_frozen_OHLCV_only")
        self.assertEqual(result["coverage"]["analyzed_market_bar_count"], 55)
        self.assertIn("daily OHLCV cannot isolate intraday reaction", result["major_events"][0]["market_reaction"]["warning"])


class SynthesisTests(NewsFixture):
    def _quant_result(self) -> dict[str, object]:
        return {"consensus": {"direction": "bullish", "prob_up": 0.62, "confidence": "medium"}}

    def test_bullish_quant_and_news_align(self):
        synthesis = build_synthesis(self._quant_result(), {
            "overall_direction": "positive", "confidence": "medium", "market_confirmation": "positive",
            "sentiment": {"label": "moderately_bullish"}, "risks": [],
        })
        self.assertEqual(synthesis["alignment"], "STRONG_ALIGNMENT")
        self.assertEqual(synthesis["quant_prob_up"], 0.62)
        self.assertTrue(synthesis["quant_probability_preserved"])

    def test_bullish_quant_and_confirmed_bearish_news_diverge_without_blending(self):
        synthesis = build_synthesis(self._quant_result(), {
            "overall_direction": "negative", "confidence": "medium", "market_confirmation": "negative",
            "sentiment": {"label": "moderately_bearish"}, "risks": [],
        })
        self.assertEqual(synthesis["alignment"], "STRONG_DIVERGENCE")
        self.assertEqual(synthesis["quant_prob_up"], 0.62)
        self.assertEqual(synthesis["probability_blending"], "prohibited_in_v1")
        self.assertEqual(synthesis["overall_confidence"], "low")

    def test_divergence_never_raises_an_already_very_low_quant_confidence(self):
        quant = {"consensus": {"direction": "bullish", "prob_up": 0.62, "confidence": "very_low"}}
        news = {"overall_direction": "negative", "confidence": "medium", "market_confirmation": "negative", "sentiment": {}, "risks": []}
        synthesis = build_synthesis(quant, news)
        self.assertEqual(synthesis["alignment"], "STRONG_DIVERGENCE")
        self.assertEqual(synthesis["overall_confidence"], "very_low")


class OrchestrationTests(NewsFixture):
    def test_news_runs_after_quant_freeze_and_synthesis_uses_frozen_results(self):
        from quant_research import orchestrator

        fake_results = {}
        for researcher_id in ("quant", "factor", "ml", "factor_backtest"):
            fake_results[researcher_id] = researcher_result(
                researcher_id, "INTC", "5D", "fixture-snapshot", status="partial",
                result_role="diagnostic" if researcher_id == "factor_backtest" else "forecast",
                prob_up=None if researcher_id == "factor_backtest" else 0.62,
                probability_source=None if researcher_id == "factor_backtest" else "fixture",
            )
        consensus = {
            "status": "ensemble", "prob_up": 0.62, "direction": "bullish", "confidence": "medium",
            "agreement": "high", "probability_range": [0.62, 0.62], "diversity": "medium",
            "warnings": [],
        }
        audit = {"status": "pass", "may_report_direction": True, "vetoes": [], "warnings": [], "bias_audit": {}}
        real_run_news = orchestrator.run_news

        def fake_news(news_input: NewsInput, policy: dict | None = None):
            quant_outputs = list((self.root / "research").glob("*/quant_result.json"))
            self.assertTrue(quant_outputs)
            self.assertTrue(quant_outputs[0].with_name("quant_result.freeze.json").is_file())
            self.assertIsInstance(news_input, NewsInput)
            self.assertEqual(len(inspect.signature(real_run_news).parameters), 2)
            return real_run_news(news_input, policy)

        with patch.dict(orchestrator.RUNNERS, {key: (lambda _path, value=value: value) for key, value in fake_results.items()}), \
             patch.object(orchestrator, "build_consensus", return_value=consensus), \
             patch.object(orchestrator, "audit", return_value=audit), \
             patch.object(orchestrator, "run_news", side_effect=fake_news):
            out = run_analysis(self.snapshot_dir, self.root / "research")

        final = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(final["consensus"]["prob_up"], 0.62)
        self.assertEqual(final["synthesis"]["quant_prob_up"], 0.62)
        self.assertTrue(final["quant_result_freeze"]["sha256"])
        self.assertTrue(final["news_result_freeze"]["sha256"])
        self.assertIn("## News & Event Analysis", (out / "report.md").read_text(encoding="utf-8"))
        self.assertIn("## Quant / News Cross-Check", (out / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
