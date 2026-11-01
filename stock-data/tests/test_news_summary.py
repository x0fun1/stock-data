"""Offline tests for the strength_v1 descriptive summary and delivery contract."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))

from quant_research.delivery import _validate_summary, validate_response_text  # noqa: E402
from quant_research.news.summary import build_summary, derive_strength  # noqa: E402
from quant_research.report import render_final_response  # noqa: E402

AS_OF = "2026-10-30T12:00:00Z"
REFS = json.loads((ROOT.parent / "references" / "news-sources.json").read_text(encoding="utf-8"))


def _article(article_id: str, *, headline: str, published: str, tier: str = "A_primary",
             source_id: str = "ir_rss", summary: str | None = None) -> dict:
    return {"article_id": article_id, "title": headline, "url": f"https://example.invalid/{article_id}",
            "source": "Company IR" if tier == "A_primary" else "Example Wire", "source_id": source_id,
            "source_tier": tier, "published_at": published, "summary": summary, "tickers": ["GOOGL"]}


def _collected(articles: list[dict], *, partial: bool = True) -> dict:
    return {
        "ticker": "GOOGL", "market": "US", "as_of": AS_OF, "pit_status": "published_at_only",
        "window": {"start_exclusive": "2026-08-31T12:00:00Z", "end_inclusive": AS_OF},
        "articles": articles,
        "sources": {
            "sec_edgar": {"status": "ok", "count": 0, "partial_window": False},
            "ir_rss": {"status": "ok", "count": len(articles), "partial_window": partial,
                       "observed_first": "2026-10-01T00:00:00Z", "observed_last": "2026-10-29T00:00:00Z"},
            "finnhub_rest": {"status": "ok", "count": 1, "partial_window": False},
        },
        "counts": {"collected": len(articles) + 1, "deduplicated": 1, "kept": len(articles),
                   "recent_30_days": len([row for row in articles if row["published_at"] > "2026-09-30T12:00:00Z"]),
                   "days_31_60": len([row for row in articles if row["published_at"] <= "2026-09-30T12:00:00Z"]),
                   "by_tier": {"A_primary": len(articles), "B_media": 0, "C_aggregator": 0}},
        "form4_count": 2, "form144_count": 1,
        "reference_links": {"yahoo_finance_news": "https://finance.yahoo.com/quote/GOOGL/news/",
                            "economist": "https://www.economist.com/"},
    }


def _verification(articles: list[dict], *, pit: str = "live", status: str = "primary_confirmed") -> dict:
    return {"tinyfish_status": "ok",
            "limits": {"max_items": 30},
            "counts": {"primary_confirmed": len(articles) if status == "primary_confirmed" else 0,
                       "multi_source": 0, "single_source": 0, "unverified": 0, "not_assessed": 0},
            "items": [{"article_id": row["article_id"], "status": status, "verification_pit": pit}
                      for row in articles]}


def _ai(articles: list[dict], *, lean: str = "neutral", event_type: str = "earnings_results") -> list[dict]:
    return [{"news_id": row["article_id"], "headline": row["title"], "url": row["url"],
             "published_at": row["published_at"], "source": row["source"], "lean": lean,
             "reason": "标题陈述该事项", "target_relevance": "direct", "event_type": event_type}
            for row in articles]


class StrengthTests(unittest.TestCase):
    def test_strength_rules(self):
        article = _article("a1", headline="Q3 revenue rises to $2 billion", published="2026-10-29T12:00:00Z")
        item = {"news_id": "a1", "target_relevance": "direct", "event_type": "earnings_results",
                "headline": article["title"], "reason": "标题陈述金额"}
        strong, basis = derive_strength(item, article, {"status": "primary_confirmed", "verification_pit": "live"}, REFS)
        self.assertEqual(strong, "strong", basis)
        post_hoc, basis = derive_strength(item, article, {"status": "primary_confirmed", "verification_pit": "post_hoc"}, REFS)
        self.assertEqual(post_hoc, "weak", basis)
        single, basis = derive_strength(item, article, {"status": "single_source", "verification_pit": "live"}, REFS)
        self.assertEqual(single, "weak", basis)
        medium_event = {**item, "event_type": "analyst_rating"}
        medium, basis = derive_strength(medium_event, article, {"status": "multi_source", "verification_pit": "live"}, REFS)
        self.assertEqual(medium, "medium", basis)
        no_quantity_article = {**article, "title": "Alphabet reports results"}
        medium2, basis = derive_strength({**item, "headline": no_quantity_article["title"]},
                                         no_quantity_article, {"status": "primary_confirmed", "verification_pit": "live"}, REFS)
        self.assertEqual(medium2, "medium", basis)


class SummaryTests(unittest.TestCase):
    def test_matrix_and_partial_window(self):
        articles = [
            _article("a1", headline="Q3 revenue rises to $2 billion", published="2026-10-29T12:00:00Z"),
            _article("a2", headline="Alphabet announces cloud partnership", published="2026-10-20T12:00:00Z"),
            _article("a3", headline="Analyst rating update", published="2026-09-01T12:00:00Z"),
        ]
        summary = build_summary(collected=_collected(articles), verification=_verification(articles,
                                  status="primary_confirmed"), ai_items=_ai(articles), ai_status="assessed",
                                  snapshot={"ticker": "GOOGL", "market": "US", "asset_type": "stock"},
                                  news_result={}, refs=REFS, analysis_at="2026-11-02 09:00")
        self.assertEqual(summary["direct_count"], 3)
        self.assertIn("partial_window", summary["lines"]["source_status"])
        self.assertEqual(summary["strength_version"], "strength_v1")
        self.assertIn("样本过少", summary["lines"]["matrix"])
        total = sum(summary["matrices"]["recent_30_days"][lean][strength]
                    for lean in summary["matrices"]["recent_30_days"] for strength in ("strong", "medium", "weak"))
        total += sum(summary["matrices"]["days_31_60"][lean][strength]
                     for lean in summary["matrices"]["days_31_60"] for strength in ("strong", "medium", "weak"))
        self.assertEqual(total, 3)

    def test_no_ai_has_no_matrix(self):
        articles = [_article("a1", headline="Q3 revenue rises to $2 billion", published="2026-10-29T12:00:00Z")]
        summary = build_summary(collected=_collected(articles), verification=_verification(articles),
                                ai_items=[], ai_status="not_assessed",
                                snapshot={"ticker": "GOOGL", "market": "US", "asset_type": "stock"},
                                news_result={}, refs=REFS, analysis_at="2026-11-02 09:00")
        self.assertIn("未完成", summary["lines"]["ai_assessment"])
        self.assertIn("未输出", summary["lines"]["matrix"])


class DeliveryMutationTests(unittest.TestCase):
    def _result(self):
        articles = [
            _article("a1", headline="Q3 revenue rises to $2 billion", published="2026-10-29T12:00:00Z"),
            _article("a2", headline="Alphabet announces cloud partnership", published="2026-10-20T12:00:00Z"),
            _article("a3", headline="Analyst rating update", published="2026-10-01T12:00:00Z"),
            _article("a4", headline="Alphabet publishes annual report", published="2026-09-15T12:00:00Z"),
            _article("a5", headline="Alphabet names new CFO", published="2026-09-10T12:00:00Z"),
        ]
        summary = build_summary(collected=_collected(articles), verification=_verification(articles),
                                ai_items=_ai(articles), ai_status="assessed",
                                snapshot={"ticker": "GOOGL", "market": "US", "asset_type": "stock"},
                                news_result={}, refs=REFS, analysis_at="2026-11-02 09:00")
        return {"snapshot": {"ticker": "GOOGL", "market": "US", "asset_type": "stock",
                             "asof_timestamp": AS_OF},
                "news_summary": summary, "news_result": {"status": "complete"}}

    def test_valid_response_passes(self):
        result = self._result()
        validate_response_text(result, render_final_response(result))
        _validate_summary(result["news_summary"])

    def test_mutations_are_rejected(self):
        result = self._result()
        text = render_final_response(result)
        mutations = [
            text.replace("强1", "强2"),
            text.replace("https://example.invalid/a1", ""),
            text + "\n" + "上涨" + "概率" + "：55%",
            "\n".join(line for line in text.splitlines() if not line.startswith("局限")),
            text.replace("partial_window", "完整覆盖"),
        ]
        for mutated in mutations:
            try:
                validate_response_text(result, mutated)
            except ValueError:
                continue
            raise AssertionError(f"mutation was accepted: {mutated[:120]!r}")

    def test_post_hoc_strong_is_rejected(self):
        result = self._result()
        summary = result["news_summary"]
        summary["direct_items"][0]["verification_pit"] = "post_hoc"
        summary["direct_items"][0]["strength"] = "strong"
        with self.assertRaises(ValueError):
            _validate_summary(summary)


if __name__ == "__main__":
    unittest.main()
