"""Ownership, delivery-validation, retry, and retention checks for JSON cleanup."""

import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "stock-data" / "scripts"), str(ROOT / "tests")]

from quant_research.cleanup import (  # noqa: E402
    ANALYSIS_JSON,
    CLEANUP_RECEIPT,
    PIPELINE_LOCK,
    _exclusive_lock,
    cleanup_analysis,
    cleanup_owned_run,
    create_run_workspace,
    release_pipeline_lock,
    update_run_workspace,
)
from quant_research.contracts import RESEARCHERS  # noqa: E402
from quant_research.snapshot import DOMAINS  # noqa: E402
from research_fixture import collection  # noqa: E402


class JsonCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.response_file = self.root / "prepared-reply.md"
        self.response_file.write_text("validated prepared reply\n", encoding="utf-8")
        self.validation = {
            "status": "success",
            "analysis_id": "SYNTH_analysis_20261011T000000000000Z",
            "analysis_status": "abstain",
            "reporting_status": "degraded",
            "snapshot_id": "SYNTH_20261011T000000Z_5D_0123456789",
            "ticker": "SYNTH",
            "horizon": "5D",
            "asof_timestamp": "2026-10-10T20:00:00Z",
            "prob_up": None,
        }

    def _write_json(self, path: Path, marker: str = "process-json") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"marker": marker}), encoding="utf-8")

    def _owned_run(self):
        run_dir, _ = create_run_workspace(self.root / "runtime")
        release_pipeline_lock(run_dir)
        snapshot_id = self.validation["snapshot_id"]
        analysis_id = self.validation["analysis_id"]
        collection_name = f"collection_{run_dir.name}.json"
        update_run_workspace(run_dir, collection_name=collection_name,
                             snapshot_id=snapshot_id, analysis_id=analysis_id,
                             state="awaiting_cleanup")
        self._write_json(run_dir / "collections" / collection_name)
        snapshot = run_dir / "snapshots" / snapshot_id
        self._write_json(snapshot / "manifest.json")
        for domain in DOMAINS:
            self._write_json(snapshot / f"{domain}.json")
            self._write_json(snapshot / "raw" / f"{domain}.json")
        analysis = run_dir / "research" / analysis_id
        for researcher in RESEARCHERS:
            self._write_json(analysis / "researchers" / f"{researcher}.json")
        for name in ANALYSIS_JSON:
            self._write_json(analysis / name)
        (analysis / "report.md").write_text("human readable report\n", encoding="utf-8")
        (analysis / "final_response.md").write_text("final response summary\n", encoding="utf-8")
        return run_dir, analysis

    def test_validated_owned_run_removes_only_registered_json_and_is_idempotent(self):
        run_dir, analysis = self._owned_run()
        external = self.root / "gateway-responses.json"
        external.write_text('{"private":"must stay"}\n', encoding="utf-8")
        before = hashlib.sha256(external.read_bytes()).hexdigest()

        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation) as validate:
            result = cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)

        self.assertEqual(result["cleanup_status"], "complete")
        self.assertEqual(result["analysis_status"], "abstain")
        self.assertEqual(result["json_file_count"], 1 + 1 + (1 + 2 * len(DOMAINS)) + len(RESEARCHERS) + len(ANALYSIS_JSON))
        self.assertEqual(list(run_dir.rglob("*.json")), [])
        self.assertTrue((analysis / "report.md").is_file())
        self.assertTrue((analysis / "final_response.md").is_file())
        receipt = run_dir / CLEANUP_RECEIPT
        self.assertTrue(receipt.is_file())
        receipt_text = receipt.read_text(encoding="utf-8")
        self.assertIn("response_sha256=", receipt_text)
        self.assertIn("report_sha256=", receipt_text)
        self.assertIn("removed_file_count=", receipt_text)
        self.assertNotIn("private", receipt_text)
        self.assertEqual(hashlib.sha256(external.read_bytes()).hexdigest(), before)
        validate.assert_called_once()

        repeated = cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        self.assertEqual(repeated["cleanup_status"], "already_cleaned")
        self.assertEqual(repeated["analysis_status"], "abstain")
        self.assertEqual(len(repeated["retained_files"]), 2)

    def test_real_abstain_pipeline_reply_is_revalidated_before_owned_cleanup(self):
        spec = importlib.util.spec_from_file_location(
            "quant_research_cli_cleanup_test", ROOT / "stock-data" / "scripts" / "quant_research.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        fixture = collection(21, ticker="INTC")
        request_file = self.root / "request.json"
        responses_file = self.root / "gateway-responses.json"
        request_file.write_text(json.dumps(fixture["request"]), encoding="utf-8")
        responses_file.write_text(json.dumps(fixture["domains"]), encoding="utf-8")

        def invoke(arguments, *, expected_exit=0):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = cli.main(arguments)
            self.assertEqual(exit_code, expected_exit, stderr.getvalue())
            return json.loads(stdout.getvalue() or stderr.getvalue())

        pipeline = invoke([
            "pipeline", "--request", str(request_file), "--responses", str(responses_file),
            "--output-root", str(self.root / "runtime"), "--cleanup-json",
        ])
        self.assertEqual(pipeline["analysis_status"], "abstain")
        self.assertTrue(pipeline["cleanup_required"])
        run_dir = Path(pipeline["run_dir"])
        analysis = Path(pipeline["report_dir"])
        prepared = self.root / "prepared-from-final.md"
        prepared.write_text((analysis / "final_response.md").read_text(encoding="utf-8"), encoding="utf-8")
        delivery = invoke([
            "validate-response", "--analysis-dir", str(analysis), "--response-file", str(prepared),
        ])
        self.assertEqual(delivery["delivery_status"], "validated")
        cleanup_args = [
            "cleanup-run", "--run-dir", str(run_dir), "--analysis-dir", str(analysis),
            "--response-file", str(prepared),
        ]
        report_json = analysis / "report.json"
        report_bytes = report_json.read_bytes()
        report_value = json.loads(report_bytes.decode("utf-8"))
        report_value["analysis_id"] = "tampered-analysis"
        report_json.write_text(json.dumps(report_value), encoding="utf-8")
        report_error = invoke(cleanup_args, expected_exit=2)
        self.assertEqual(report_error["status"], "error")
        self.assertGreater(len(list(run_dir.rglob("*.json"))), 0)
        report_json.write_bytes(report_bytes)

        quant_json = analysis / "quant_result.json"
        quant_bytes = quant_json.read_bytes()
        quant_value = json.loads(quant_bytes.decode("utf-8"))
        quant_value["snapshot"]["ticker"] = "TAMPERED"
        quant_json.write_text(json.dumps(quant_value), encoding="utf-8")
        freeze_error = invoke(cleanup_args, expected_exit=2)
        self.assertEqual(freeze_error["status"], "error")
        self.assertGreater(len(list(run_dir.rglob("*.json"))), 0)
        quant_json.write_bytes(quant_bytes)

        result = invoke(cleanup_args)
        self.assertEqual(result["cleanup_status"], "complete")
        self.assertEqual(result["analysis_status"], "abstain")
        self.assertEqual(list(run_dir.rglob("*.json")), [])
        self.assertTrue((analysis / "final_response.md").is_file())
        self.assertTrue((analysis / "report.md").is_file())

    def test_live_pipeline_marker_blocks_cleanup_even_with_force_flag(self):
        run_dir, analysis = self._owned_run()
        (run_dir / PIPELINE_LOCK).write_text(f"pid={os.getpid()}\n", encoding="ascii")
        before = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        with self.assertRaisesRegex(ValueError, "pipeline is still active"):
            cleanup_owned_run(run_dir, analysis_dir=analysis, force_incomplete=True)
        after = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        self.assertEqual(after, before)

    def test_cleanup_lock_blocks_concurrent_run_and_reuses_stale_lock_file(self):
        run_dir, analysis = self._owned_run()
        lock = run_dir.parent / f".{run_dir.name}.cleanup.lock"
        lock.write_text("pid=999999999\n", encoding="ascii")
        with _exclusive_lock(lock):
            with self.assertRaisesRegex(ValueError, "another cleanup is active"):
                cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation):
            result = cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        self.assertEqual(result["cleanup_status"], "complete")
        self.assertTrue(lock.is_file())

    def test_stale_pipeline_marker_is_preserved_when_run_ownership_is_invalid(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        marker = unrelated / PIPELINE_LOCK
        marker.write_text("pid=999999999\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "manifest is missing"):
            cleanup_owned_run(unrelated, force_incomplete=True)
        self.assertEqual(marker.read_text(encoding="ascii"), "pid=999999999\n")

    def test_unrecognized_cleanup_lock_file_is_not_overwritten(self):
        run_dir, analysis = self._owned_run()
        lock = run_dir.parent / f".{run_dir.name}.cleanup.lock"
        lock.write_text("user data\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected contents"):
            cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        self.assertEqual(lock.read_text(encoding="utf-8"), "user data\n")

    def test_failed_run_requires_explicit_force_incomplete(self):
        run_dir, analysis = self._owned_run()
        update_run_workspace(run_dir, state="failed")
        before = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        with self.assertRaisesRegex(ValueError, "force-incomplete"):
            cleanup_owned_run(run_dir, analysis_dir=analysis)
        after = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        self.assertEqual(after, before)
        forced = cleanup_owned_run(run_dir, analysis_dir=analysis, force_incomplete=True)
        self.assertEqual(forced["cleanup_status"], "complete")
        self.assertIsNone(forced["analysis_status"])
        self.assertEqual(list(run_dir.rglob("*.json")), [])

    def test_delivery_validation_failure_leaves_every_json_in_place(self):
        run_dir, analysis = self._owned_run()
        before = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        with patch("quant_research.cleanup.validate_final_response", side_effect=ValueError("invalid reply")):
            with self.assertRaisesRegex(ValueError, "invalid reply"):
                cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        after = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        self.assertEqual(after, before)
        self.assertFalse((run_dir / CLEANUP_RECEIPT).exists())

    def test_unknown_file_blocks_cleanup_without_deleting_registered_targets(self):
        run_dir, analysis = self._owned_run()
        (run_dir / "user-notes.json").write_text('{"keep":true}', encoding="utf-8")
        before = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation):
            with self.assertRaisesRegex(ValueError, "unknown files"):
                cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        after = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        self.assertEqual(after, before)

    def test_link_inside_owned_tree_blocks_cleanup_without_touching_target(self):
        run_dir, analysis = self._owned_run()
        outside = self.root / "outside.json"
        outside.write_text('{"must":"stay"}', encoding="utf-8")
        link = run_dir / "collections" / "outside.json"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlinks are unavailable: {type(exc).__name__}")
        outside_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
        before = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation):
            with self.assertRaisesRegex(ValueError, "linked or non-directory"):
                cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
        after = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        self.assertEqual(after, before)
        self.assertEqual(hashlib.sha256(outside.read_bytes()).hexdigest(), outside_hash)

    def test_mid_cleanup_failure_is_reported_and_can_be_retried(self):
        run_dir, analysis = self._owned_run()
        manifest = run_dir / "run_manifest.json"
        original_unlink = Path.unlink
        failed_once = {"value": False}

        def fail_manifest_once(path, *args, **kwargs):
            if path == manifest and not failed_once["value"]:
                failed_once["value"] = True
                raise PermissionError("synthetic file lock")
            return original_unlink(path, *args, **kwargs)

        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation) as validate:
            with patch.object(Path, "unlink", fail_manifest_once):
                first = cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
            self.assertEqual(first["cleanup_status"], "partial_cleanup")
            self.assertEqual(first["remaining_json_files"], ["run_manifest.json"])
            second = cleanup_owned_run(run_dir, analysis_dir=analysis, response_file=self.response_file)
            self.assertEqual(second["cleanup_status"], "complete")
            validate.assert_called_once()
        self.assertFalse(manifest.exists())
        self.assertEqual(list(run_dir.rglob("*.json")), [])

    def test_dry_run_reports_targets_without_mutating_the_run(self):
        run_dir, analysis = self._owned_run()
        before = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation):
            result = cleanup_owned_run(run_dir, analysis_dir=analysis,
                                       response_file=self.response_file, dry_run=True)
        after = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*.json"))
        self.assertEqual(result["cleanup_status"], "dry_run")
        self.assertEqual(result["json_file_count"], len(before))
        self.assertEqual(after, before)
        self.assertFalse((run_dir / CLEANUP_RECEIPT).exists())

    def test_analysis_only_cleanup_keeps_the_shared_snapshot(self):
        analysis_id = "legacy_analysis_20261011"
        analysis = self.root / "legacy" / "research" / analysis_id
        for researcher in RESEARCHERS:
            self._write_json(analysis / "researchers" / f"{researcher}.json")
        for name in ANALYSIS_JSON:
            self._write_json(analysis / name)
        (analysis / "report.md").write_text("legacy report\n", encoding="utf-8")
        (analysis / "final_response.md").write_text("legacy final\n", encoding="utf-8")
        snapshot_file = self.root / "legacy" / "snapshots" / "shared" / "market.json"
        self._write_json(snapshot_file, "shared-snapshot")
        snapshot_hash = hashlib.sha256(snapshot_file.read_bytes()).hexdigest()
        self.validation["analysis_id"] = analysis_id

        with patch("quant_research.cleanup.validate_final_response", return_value=self.validation):
            result = cleanup_analysis(analysis, response_file=self.response_file)

        self.assertEqual(result["cleanup_status"], "complete")
        self.assertEqual(list(analysis.rglob("*.json")), [])
        self.assertEqual(hashlib.sha256(snapshot_file.read_bytes()).hexdigest(), snapshot_hash)
        self.assertTrue((analysis / CLEANUP_RECEIPT).is_file())


if __name__ == "__main__":
    unittest.main()
