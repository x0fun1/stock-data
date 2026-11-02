"""Exercise the actual CLI and collection/import boundary with clearly synthetic evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from news_research.common.jsonio import read_json, write_json
from news_research.common.time import iso, now, parse_time

CLI = Path(__file__).resolve().parents[1] / "news_research.py"


class CliIntegrationTests(unittest.TestCase):
    def invoke(self, *args: str, expected: int = 0) -> dict:
        completed = subprocess.run(  # noqa: S603 - fixed local project CLI, explicit arguments, no shell or external command text
            [sys.executable, "-B", str(CLI), *args], check=False, capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr.decode("utf-8"))
        output = completed.stdout if expected == 0 else completed.stderr
        return json.loads(output.decode("utf-8"))

    def fixture(self, root: Path) -> tuple[Path, Path]:
        end = now()
        start = iso(parse_time(end) - timedelta(days=60))
        published = iso(parse_time(end) - timedelta(days=2))
        request = {"symbol": "DEMO", "market": "US", "as_of": end, "sources": [{"source_id": "company_ir"}],
                   "budget": {"max_requests": 0, "batch_size": 1, "max_analysis_events": 2}}
        article = {"source_id": "company_ir", "source_item_id": "synthetic-contract", "url": "https://example.invalid/synthetic-contract",
                   "title": "合成验收订单，非真实新闻", "text": "合成验收材料：公司收到重大订单，后续执行仍有条件。",
                   "source_kind": "company", "text_scope": "official_excerpt", "published_at": published, "fetched_at": end,
                   "entities": ["DEMO"], "event_key": "synthetic-contract", "permissions": {"store_text": True, "retention_days": 120},
                   "receipt_id": "synthetic-receipt"}
        receipt = {"receipt_id": "synthetic-receipt", "source_id": "company_ir", "entity": "DEMO", "domain": "company",
                   "requested_start": start, "requested_end": end, "successful_intervals": [{"start": start, "end": end}],
                   "failed_intervals": [], "status": "success", "cursor": None, "exhausted": True, "cap_reached": False,
                   "fetched_count": 1, "deduplicated_count": 1, "last_success_at": end, "reason": "synthetic test receipt, not a real query"}
        request_path, import_path = root / "request.json", root / "imports.json"
        write_json(request_path, request)
        write_json(import_path, {"records": [article], "coverage": [receipt]})
        return request_path, import_path

    def test_full_import_prepare_analyze_report_validate_and_failed_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request, imports = self.fixture(root)
            collected = self.invoke("collect", "--request", str(request), "--imports", str(imports), "--output-root", str(root / "runtime"))
            self.assertEqual(collected["events"], 1)
            self.assertIn("仅声明配置来源", collected["coverage"]["scope_note"])
            run = collected["run_dir"]
            prepared = self.invoke("prepare", "--run-dir", run)
            pack = read_json(Path(prepared["prepared_file"]))
            event = pack["batches"][0][0]
            evidence_id = event["evidence_ids"][0]
            assessment = {
                "schema_version": "news-1", "input_digest": prepared["input_digest"], "model_id": "host",
                "assessments": [{"event_id": event["event_id"], "target": "DEMO", "evidence_ids": [evidence_id],
                                 "fact_summary": pack["evidence"][0]["quote"], "mechanism": "分析判断：订单可能支持后续经营。",
                                 "direction": "bullish", "importance": "major", "importance_reason": "本合成用例假定订单影响主营业务。",
                                 "time_horizon": "后续执行期间", "evidence_status": "sufficient", "counter_evidence_ids": [],
                                 "counterargument": "尚无独立相反资料，执行条件仍待核实。", "exposure_ids": [],
                                 "impact_scale": "主营业务受到直接影响。", "persistence": "取决于订单执行。",
                                 "new_information": "原文披露收到订单。", "uncertainty": "实际执行条件尚不明确。", "conditions": ["核实后续执行情况"]}],
                "selection": {"selected_event_id": event["event_id"], "reason": "本合成用例唯一有正文的重大事件。",
                              "comparison": "无其他合格候选，不以数量评分。", "uncertain": False},
                "synthesis": {"direction": "bullish", "status": "limited", "primary_event_ids": [event["event_id"]],
                              "background_event_ids": [], "reason": "分析判断：订单直接影响经营，但资料仅来自合成用例。",
                              "counter_evidence_ids": [], "counterargument": "缺少独立执行资料。", "why_not_counter": "仅对已披露订单形成有限判断。",
                              "change_conditions": ["订单撤回或执行条件发生实质变化"]},
            }
            assessment_path = root / "host.json"
            write_json(assessment_path, assessment)
            outcome = self.invoke("analyze", "--run-dir", run, "--assessment", str(assessment_path))
            self.assertEqual(outcome["direction"], "bullish")
            reported = self.invoke("report", "--run-dir", run)
            reply = Path(reported["report_file"])
            self.assertTrue(self.invoke("validate-report", "--run-dir", run, "--response-file", str(reply))["valid"])
            assessment["assessments"][0]["mechanism"] = "收入新增99999亿美元"
            write_json(assessment_path, assessment)
            failure = self.invoke("analyze", "--run-dir", run, "--assessment", str(assessment_path), expected=1)
            self.assertEqual(failure["status"], "error")
            degraded = self.invoke("report", "--run-dir", run)
            self.assertIsNone(degraded["direction"])
            self.assertIn("综合结论：证据不足", reply.read_text(encoding="utf-8"))
            self.assertTrue(self.invoke("validate-report", "--run-dir", run, "--response-file", str(reply))["valid"])

    def test_repeated_incremental_import_is_idempotent_and_gc_dry_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request, imports = self.fixture(root)
            for args in ([], ["--incremental"]):
                self.invoke("collect", "--request", str(request), "--imports", str(imports), "--output-root", str(root / "runtime"), *args)
            connection = sqlite3.connect(root / "runtime" / "evidence.sqlite3")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
            finally:
                connection.close()
            self.assertTrue(self.invoke("gc", "--output-root", str(root / "runtime"))["dry_run"])
            self.assertTrue((root / "runtime" / "evidence.sqlite3").exists())

    def test_collected_financial_facts_reach_the_frozen_context(self):
        from news_research.orchestration import collect

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path, import_path = self.fixture(root)
            supplied = read_json(import_path)
            article = supplied["records"][0]
            article.update(data_type="financial", text="Synthetic revenue: 100 USD; period end 2025-12-31.")
            fact = {"metric": "revenue", "value": 100, "unit": "USD", "currency": "USD", "period_end": "2025-12-31",
                    "period_kind": "annual", "disclosed_at": article["published_at"], "accounting_basis": "US-GAAP", "basis": "reported",
                    "record_refs": [{"source_id": article["source_id"], "source_item_id": article["source_item_id"]}]}
            payload = {"records": [article], "coverage": supplied["coverage"], "source_capabilities": [], "limitations": [], "financial_facts": [fact]}
            with patch("news_research.collection.collect.collect_sources", return_value=payload):
                outcome = collect(read_json(request_path), root / "runtime")
            snapshot = read_json(Path(outcome["run_dir"]) / "snapshot.json")
            self.assertEqual(snapshot["context"]["financial_facts"][0]["value"], "100")
            self.assertEqual(snapshot["events"], [])

    def test_unknown_source_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.json"
            write_json(request, {"symbol": "DEMO", "market": "US", "as_of": now(), "sources": [{"source_id": "invented_source"}]})
            failure = self.invoke("collect", "--request", str(request), "--output-root", str(root / "runtime"), expected=1)
            self.assertIn("unregistered source_id", failure["error"])

    def test_all_public_schemas_and_sources_are_valid_json(self):
        for kind in ("request", "assessment", "import"):
            self.assertEqual(self.invoke("schema", "--kind", kind)["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("sec_submissions", [item["source_id"] for item in self.invoke("sources")])
