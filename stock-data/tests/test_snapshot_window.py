"""Offline tests for the 52-week descriptive window (stdlib unittest)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))

from quant_research.snapshot import DESCRIPTIVE_WINDOW_BARS, _truncate_descriptive_window  # noqa: E402


def _bars(count: int) -> list[dict]:
    return [{"date": f"2025-01-{(index % 28) + 1:02d}", "close": 100 + index} for index in range(count)]


class DescriptiveWindowTests(unittest.TestCase):
    def test_more_than_250_bars_are_truncated_and_annotated(self):
        market = {"data": {"bars": _bars(410), "history_window": {"start": "2025-01-01", "end": "2026-01-01"}}}
        updated, warning = _truncate_descriptive_window(market)
        self.assertEqual(len(updated["data"]["bars"]), DESCRIPTIVE_WINDOW_BARS)
        self.assertEqual(updated["data"]["bars"][0]["date"], _bars(410)[-DESCRIPTIVE_WINDOW_BARS]["date"])
        self.assertTrue(updated["history_truncated"])
        self.assertEqual(updated["history_truncation"]["omitted_bar_count"], 410 - DESCRIPTIVE_WINDOW_BARS)
        self.assertEqual(updated["data"]["history_window"]["selection"], "trailing 52 weeks")
        self.assertIn("truncated", warning)
        self.assertEqual(len(market["data"]["bars"]), 410)

    def test_short_history_is_untouched(self):
        market = {"data": {"bars": _bars(120)}}
        updated, warning = _truncate_descriptive_window(market)
        self.assertIs(updated, market)
        self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
