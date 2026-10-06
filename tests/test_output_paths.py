"""Offline regressions for untrusted snapshot IDs and output containment."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))

from quant_research.orchestrator import run_analysis
from quant_research.snapshot import DOMAINS, freeze_snapshot, load_snapshot


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stock-output-path-test-")
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        domains = {name: {"status": "unavailable", "data": None} for name in DOMAINS}
        domains["market"] = {
            "status": "complete", "actual_source": "offline fixture", "adjustment": "adjusted",
            "last_bar_closed": True, "data": {"bars": [
                {"date": f"2024-03-{day}", "open": 100, "high": 102, "low": 99,
                 "close": 101, "volume": 1000}
                for day in (13, 14, 15)
            ]},
        }
        self.collection = {
            "request": {"ticker": "INTC", "market": "US", "horizon": "5D", "asof": "2024-03-15T21:00:00Z"},
            "domains": domains,
        }
        self.input_path = self.root / "collection.json"
        self.input_path.write_text(json.dumps(self.collection), encoding="utf-8")
        self.snapshot_dir = freeze_snapshot(self.input_path, self.root / "snapshots")
        self.manifest_path = self.snapshot_dir / "manifest.json"
        self.original_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.suffix = self.original_manifest["snapshot_content_sha256"][:10]

    def tearDown(self):
        # All links made by these tests target another directory in this same fixture.
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.temporary.cleanup()

    def _set_snapshot_id(self, value):
        manifest = {**self.original_manifest, "snapshot_id": value}
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _directory_link(self, link: Path, target: Path):
        self.assertTrue(link.parent.resolve().is_relative_to(self.root))
        self.assertTrue(target.resolve().is_relative_to(self.root))
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                self.skipTest(f"directory symlinks unavailable: {error}")
            # Windows junctions do not require the privilege needed for symlinks.
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
            )
            if result.returncode:
                self.skipTest(f"directory links unavailable: {result.stderr!r}")
        self.assertEqual(link.resolve(), target.resolve())

    def test_snapshot_ids_with_path_syntax_are_rejected_before_any_output(self):
        suffix = self.suffix
        bad_ids = [
            f"../escaped_{suffix}", f"..\\escaped_{suffix}",
            f"nested/escaped_{suffix}", f"nested\\escaped_{suffix}",
            str(self.root / f"absolute_{suffix}"), f"/absolute_{suffix}",
            f"C:\\absolute_{suffix}", f"C:relative_{suffix}",
            f"\\\\server\\share\\escaped_{suffix}", f"name:stream_{suffix}",
            f"..%2fescaped_{suffix}", f"space name_{suffix}",
            f"control\nname_{suffix}", f"null\x00name_{suffix}",
        ]
        output_root = self.root / "research"
        expected_entries = set(self.root.iterdir())
        for snapshot_id in bad_ids:
            with self.subTest(snapshot_id=snapshot_id):
                # Only the ID is changed: the existing valid content/domain hashes stay intact.
                self._set_snapshot_id(snapshot_id)
                with self.assertRaisesRegex(ValueError, "snapshot_id.*safe filename component"):
                    load_snapshot(self.snapshot_dir)
                with self.assertRaisesRegex(ValueError, "snapshot_id.*safe filename component"):
                    run_analysis(self.snapshot_dir, output_root)
                self.assertFalse(output_root.exists())
                self.assertEqual(set(self.root.iterdir()), expected_entries)

    def test_missing_non_string_and_dot_snapshot_ids_are_rejected(self):
        for value in (None, 123, {}, [], "", ".", ".."):
            with self.subTest(value=value):
                self._set_snapshot_id(value)
                with self.assertRaisesRegex(ValueError, "snapshot_id.*safe filename component"):
                    load_snapshot(self.snapshot_dir)

    def test_safe_id_with_wrong_digest_remains_rejected(self):
        self._set_snapshot_id("safe_name_" + "0" * 10)
        with self.assertRaisesRegex(ValueError, "snapshot content identity mismatch"):
            load_snapshot(self.snapshot_dir)

    def test_normal_snapshot_and_relative_output_root_complete_all_artifacts(self):
        manifest, _ = load_snapshot(self.snapshot_dir)
        self.assertEqual(manifest["snapshot_id"], self.original_manifest["snapshot_id"])
        output_root = self.root / "research"
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            destination = run_analysis(self.snapshot_dir, Path("research"))
        finally:
            os.chdir(original_cwd)
        self.assertTrue(destination.is_absolute())
        self.assertEqual(destination.resolve().parent, output_root)
        for name in ("quant_result.json", "quant_result.freeze.json", "news_result.json",
                     "news_result.freeze.json", "report.json", "report.md"):
            self.assertTrue((destination / name).is_file(), name)
        report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["snapshot"]["snapshot_id"], manifest["snapshot_id"])
        self.assertEqual(set(report["researchers"]), {"quant", "factor", "ml", "factor_backtest"})

    def test_freezer_keeps_normal_ticker_slug_formats_compatible(self):
        for ticker in ("BRK.B", "0700.HK", ".DJI", "^GSPC"):
            with self.subTest(ticker=ticker):
                collection = {**self.collection, "request": {**self.collection["request"], "ticker": ticker}}
                self.input_path.write_text(json.dumps(collection), encoding="utf-8")
                destination = freeze_snapshot(self.input_path, self.root / "snapshots")
                manifest, _ = load_snapshot(destination)
                self.assertEqual(manifest["ticker"], ticker)
                self.assertEqual(destination.resolve().parent, self.root / "snapshots")

    def test_analysis_rejects_output_link_escape_before_writing_or_running_paths(self):
        from quant_research import orchestrator

        output_root = self.root / "research"
        output_root.mkdir()
        outside = self.root / "outside-research"
        outside.mkdir()
        clock = datetime(2024, 3, 16, tzinfo=timezone.utc)
        analysis_name = self.original_manifest["snapshot_id"] + "_analysis_" + clock.strftime("%Y%m%dT%H%M%S%fZ")
        self._directory_link(output_root / analysis_name, outside)
        with patch.object(orchestrator, "datetime") as fake_clock, \
             patch.object(orchestrator, "RUNNERS") as runners:
            fake_clock.now.return_value = clock
            with self.assertRaisesRegex(ValueError, "outside output_root"):
                run_analysis(self.snapshot_dir, output_root)
            runners.__getitem__.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_analysis_output_guard_also_rejects_unsafe_id_if_loader_is_bypassed(self):
        from quant_research import orchestrator

        manifest, market = load_snapshot(self.snapshot_dir)
        manifest = {**manifest, "snapshot_id": "../escaped_" + self.suffix}
        output_root = self.root / "research"
        expected_entries = set(self.root.iterdir())
        with patch.object(orchestrator, "load_snapshot", return_value=(manifest, market)):
            with self.assertRaisesRegex(ValueError, "output name.*safe filename component"):
                run_analysis(self.snapshot_dir, output_root)
        self.assertFalse(output_root.exists())
        self.assertEqual(set(self.root.iterdir()), expected_entries)

    def test_freezer_rejects_output_link_escape_before_writing(self):
        from quant_research import snapshot

        output_root = self.root / "other-snapshots"
        output_root.mkdir()
        outside = self.root / "outside-snapshots"
        outside.mkdir()
        self._directory_link(output_root / self.original_manifest["snapshot_id"], outside)
        with patch.object(snapshot, "utc_now", return_value=self.original_manifest["snapshot_created_at"]):
            with self.assertRaisesRegex(ValueError, "outside output_root"):
                freeze_snapshot(self.input_path, output_root)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(len(list(output_root.iterdir())), 1)

    def test_existing_analysis_is_not_overwritten(self):
        from quant_research import orchestrator

        clock = datetime(2024, 3, 16, tzinfo=timezone.utc)
        with patch.object(orchestrator, "datetime") as fake_clock:
            fake_clock.now.return_value = clock
            destination = run_analysis(self.snapshot_dir, self.root / "research")
            report = destination / "report.json"
            before = hashlib.sha256(report.read_bytes()).hexdigest()
            with self.assertRaises(FileExistsError):
                run_analysis(self.snapshot_dir, self.root / "research")
            self.assertEqual(hashlib.sha256(report.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
