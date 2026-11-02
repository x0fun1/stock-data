"""Offline news-research acceptance suite; no accounts, network or market claims."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent


class DistributionChecks(unittest.TestCase):
    def test_deleted_capabilities_are_not_shipped(self):
        deleted = ("quant_research", "quant_research.py", "momentum_ab.py", "selection_ab.py",
                   "research_b_ab.py", "research_candidates.py", "yahoo_collect.py")
        for name in deleted:
            self.assertFalse((SCRIPTS / name).exists(), name)
        self.assertIsNone(importlib.util.find_spec("quant_research"))
        forbidden = {"calc_ma", "calc_macd", "calc_rsi", "calc_kdj", "calc_boll", "_ema", "unusual_activity",
                     "short_volume_ranking", "frame_ranking", "frame_screen", "yahoo_screen"}
        for path in SCRIPTS.rglob("*.py"):
            if path == Path(__file__).resolve() or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertNotIn(node.name, forbidden, str(path))
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("quant_research"), str(path))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name.startswith("quant_research") for alias in node.names), str(path))

    def test_markdown_links_resolve(self):
        for path in [SKILL.parent / "README.md", SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]:
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                self.assertTrue((path.parent / target.split("#")[0]).exists(), f"{path}: {target}")


def main() -> int:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(DistributionChecks))
    suite.addTests(unittest.defaultTestLoader.discover(str(SCRIPTS / "tests"), pattern="test_*.py"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
