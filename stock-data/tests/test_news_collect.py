"""Offline fixture tests for scripts/news_collect.py (stdlib unittest)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))

import news_collect as nc  # noqa: E402

FIXTURES = ROOT / "fixtures"
AS_OF = "2026-11-01T21:00:00Z"
# Fixture-only tokens: never real credentials and never written to output.
TEST_ENV = {"SEC_USER_AGENT": "stock-data-skill/4.0 (offline-fixture@example.invalid)", "FINNHUB_API_KEY": "fixture-token"}


def _body(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_fetch(url: str, headers: dict[str, str], *, finnhub_status: int = 200,
                  error: str | None = None):
    if error:
        return None, None, error
    if url == nc.SEC_TICKERS_URL:
        return 200, _body("sec_company_tickers.json"), None
    if url.startswith("https://data.sec.gov/submissions/"):
        return 200, _body("sec_submissions_1652044.json"), None
    if "abc.xyz" in url:
        return 200, _body("ir_alphabet.xml"), None
    if "finnhub.io" in url:
        if finnhub_status != 200:
            return finnhub_status, None, f"HTTP {finnhub_status}"
        assert headers.get("X-Finnhub-Token") == "fixture-token"
        assert "token=" not in url
        return 200, _body("finnhub_company_news_GOOGL.json"), None
    return 404, None, "HTTP 404"


class CollectTests(unittest.TestCase):
    def collect(self, **kwargs):
        return nc.collect(
            ticker="GOOGL", as_of=kwargs.pop("as_of", AS_OF), market=kwargs.pop("market", "US"),
            aliases=["Alphabet Inc."], fetch=kwargs.pop("fetch", fixture_fetch),
            env=kwargs.pop("env", TEST_ENV), refs=nc.load_sources(),
            min_intervals={"sec": 0, "finnhub": 0}, **kwargs,
        )

    def test_window_status_and_dedupe(self):
        result = self.collect()
        self.assertEqual(result["pit_status"], "published_at_only")
        self.assertEqual(result["sources"]["sec_edgar"]["status"], "ok")
        self.assertEqual(result["sources"]["ir_rss"]["status"], "ok")
        self.assertEqual(result["sources"]["finnhub_rest"]["status"], "ok")
        self.assertEqual(result["form4_count"], 1)
        self.assertEqual(result["form144_count"], 1)
        self.assertGreaterEqual(result["sources"]["sec_edgar"]["dropped_outside_window"], 1)
        start, end = result["window"]["start_exclusive"], result["window"]["end_inclusive"]
        for row in result["articles"]:
            self.assertLessEqual(row["published_at"], end)
            self.assertGreater(row["published_at"], start)
        titles = [row["title"] for row in result["articles"]]
        self.assertFalse(any("annual report" in title.lower() for title in titles))
        duplicate = [row for row in result["articles"]
                     if row["title"] == "Alphabet announces third quarter 2026 results"]
        self.assertEqual(len(duplicate), 1, duplicate)
        self.assertEqual(duplicate[0]["source_id"], "ir_rss")
        self.assertIn("Example Wire", duplicate[0]["also_seen_in"])
        self.assertTrue(result["sources"]["ir_rss"]["partial_window"])
        self.assertIn("yahoo_finance_news", result["reference_links"])
        self.assertIn("economist", result["reference_links"])

    def test_earlier_as_of_has_no_later_record(self):
        result = self.collect(as_of="2026-10-28T18:00:00Z")
        for row in result["articles"]:
            self.assertLessEqual(row["published_at"], "2026-10-28T18:00:00Z")
        self.assertFalse(any("third quarter" in row["title"].lower() for row in result["articles"]))

    def test_missing_credentials_do_not_crash(self):
        result = self.collect(env={})
        self.assertEqual(result["sources"]["sec_edgar"]["status"], "not_configured")
        self.assertEqual(result["sources"]["finnhub_rest"]["status"], "not_configured")
        self.assertEqual(result["sources"]["ir_rss"]["status"], "ok")

    def test_hk_sec_not_applicable(self):
        result = self.collect(market="HK", env={})
        self.assertEqual(result["sources"]["sec_edgar"]["status"], "not_applicable")
        self.assertEqual(result["sources"]["ir_rss"]["status"], "not_configured_for_ticker")

    def test_rate_limited_is_not_retried(self):
        def fetch(url, headers):
            return fixture_fetch(url, headers, finnhub_status=429)

        result = self.collect(fetch=fetch)
        self.assertEqual(result["sources"]["finnhub_rest"]["status"], "rate_limited")

    def test_network_error_is_reported(self):
        def fetch(url, headers):
            return fixture_fetch(url, headers, error="network_error: URLError")

        result = self.collect(fetch=fetch)
        self.assertEqual(result["sources"]["finnhub_rest"]["status"], "error")
        self.assertNotEqual(result["status"], "error")

    def test_rate_limiter_min_interval(self):
        slept = []
        limiter = nc.RateLimiter(max_calls=10, window_seconds=60, min_interval_seconds=0.5,
                                 sleep=slept.append)
        limiter.wait()
        limiter.wait()
        self.assertTrue(slept and slept[0] > 0, slept)


if __name__ == "__main__":
    unittest.main()
