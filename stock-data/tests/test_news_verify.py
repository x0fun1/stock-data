"""Offline fixture tests for scripts/news_verify.py (stdlib unittest)."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))

import news_verify as nv  # noqa: E402

FIXTURES = ROOT / "fixtures"
AS_OF = "2026-11-01T21:00:00Z"


def _fx(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _candidate(article_id: str, title: str, *, source_id="finnhub_rest", tier="B_media",
               published="2026-10-29T20:50:00Z", url=None) -> dict:
    return {"article_id": article_id, "title": title, "url": url or f"https://example.invalid/{article_id}",
            "published_at": published, "source": "Example Wire", "source_id": source_id,
            "source_tier": tier}


def _candidates(rows: list[dict]) -> dict:
    return {"ticker": "GOOGL", "as_of": AS_OF, "articles": rows}


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.refs = nv.load_sources()
        self.limiter = nv.RateLimiter(max_calls=1000, window_seconds=1, sleep=lambda _x: None)

    def test_missing_key_is_not_assessed_without_requests(self):
        calls = []

        def fetch(method, url, headers, body):
            calls.append(url)
            return 200, {}, None

        result = nv.verify(candidates=_candidates([_candidate("f1", "Alphabet wins a new cloud contract")]),
                           key=None, refs=self.refs, fetch_json=fetch)
        self.assertEqual(result["tinyfish_status"], "not_assessed")
        self.assertEqual(result["items"][0]["status"], "not_assessed")
        self.assertEqual(calls, [])

    def test_primary_and_ir_are_confirmed_without_requests(self):
        calls = []

        def fetch(method, url, headers, body):
            calls.append(url)
            return 200, {}, None

        rows = [_candidate("s1", "8-K filing", source_id="sec_edgar", tier="A_primary"),
                _candidate("i1", "Alphabet press release", source_id="ir_rss", tier="A_primary")]
        result = nv.verify(candidates=_candidates(rows), key="fixture-token", refs=self.refs,
                           fetch_json=fetch)
        self.assertEqual([row["status"] for row in result["items"]], ["primary_confirmed", "primary_confirmed"])
        self.assertEqual(calls, [])

    def test_multi_source_and_fetch_text_not_stored(self):
        def fetch(method, url, headers, body):
            if method == "GET":
                return 200, _fx("tinyfish_search_multi_source.json"), None
            return 200, _fx("tinyfish_fetch_result.json"), None

        result = nv.verify(candidates=_candidates([_candidate("f1", "Alphabet wins a new cloud contract")]),
                           key="fixture-token", refs=self.refs, fetch_json=fetch,
                           search_limiter=self.limiter, fetch_limiter=self.limiter)
        item = result["items"][0]
        self.assertEqual(item["status"], "multi_source")
        self.assertEqual(len(item["evidence_links"]), 2)
        self.assertNotIn("FIXTURE FETCH BODY", json.dumps(result))
        self.assertNotIn("Fixture snippet", json.dumps(result))
        self.assertEqual(result["requests"]["search"], 1)

    def test_primary_search_result(self):
        def fetch(method, url, headers, body):
            if method == "GET":
                return 200, _fx("tinyfish_search_primary.json"), None
            return 200, _fx("tinyfish_fetch_result.json"), None

        result = nv.verify(candidates=_candidates([_candidate("f1", "Alphabet announces third quarter 2026 results")]),
                           key="fixture-token", refs=self.refs, fetch_json=fetch,
                           search_limiter=self.limiter, fetch_limiter=self.limiter)
        self.assertEqual(result["items"][0]["status"], "primary_confirmed")

    def test_fetch_whitelist_rejects_forbidden_domains(self):
        for url in ("https://finance.yahoo.com/quote/GOOGL/news/",
                    "https://www.economist.com/business",
                    "https://www.reuters.com/technology",
                    "https://apnews.com/article/x",
                    "https://news.google.com/articles/x"):
            self.assertFalse(nv.fetch_allowed(url, self.refs), url)
        self.assertTrue(nv.fetch_allowed("https://www.bloomberg.com/news/articles/x", self.refs))

    def test_item_cap(self):
        rows = [_candidate(f"f{i}", f"Alphabet story {i}") for i in range(35)]

        def fetch(method, url, headers, body):
            return 200, {"results": [], "total_results": 0, "page": 0}, None

        result = nv.verify(candidates=_candidates(rows), key="fixture-token", refs=self.refs,
                           fetch_json=fetch, search_limiter=self.limiter, fetch_limiter=self.limiter)
        self.assertEqual(result["requests"]["search"], 30)
        self.assertEqual(result["counts"]["not_assessed"], 5)

    def test_post_hoc_and_rate_limited(self):
        def fetch(method, url, headers, body):
            return 429, None, "HTTP 429"

        result = nv.verify(candidates=_candidates([_candidate("f1", "Alphabet news")]),
                           as_of="2026-10-30T21:00:00Z", key="fixture-token", refs=self.refs,
                           fetch_json=fetch, today=date(2026, 11, 2),
                           search_limiter=self.limiter, fetch_limiter=self.limiter)
        self.assertEqual(result["verification_pit"], "post_hoc")
        self.assertEqual(result["items"][0]["search_status"], "rate_limited")
        self.assertEqual(result["items"][0]["status"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
