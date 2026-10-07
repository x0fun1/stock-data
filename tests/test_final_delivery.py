"""Brief analysis must expose actual Quant fields or explicit unavailability."""

import copy
import importlib.util
import io
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "stock-data/scripts"), str(ROOT / "tests")]
from quant_research.delivery import validate_final_response, validate_response_text
from quant_research.orchestrator import run_analysis
from quant_research.report import render_final_response, required_response_lines
from quant_research.snapshot import freeze_snapshot
from research_fixture import article, collection
from test_quant_output_contract import report


class BriefDeliveryTests(unittest.TestCase):
    def test_each_target_keeps_native_metrics_after_news_and_brief_rendering(self):
        for ticker, probability in (("SYNTH-A", .43127), ("SYNTH-B", .75896)):
            for direction in ("positive", "negative", "mixed", "unknown"):
                with self.subTest(ticker=ticker, news=direction):
                    result = report(probability, ticker, news_direction=direction)
                    original = copy.deepcopy(result["consensus"])
                    text = render_final_response(result)
                    validate_response_text(result, text)
                    self.assertEqual(result["consensus"], original)
                    for field in ("prob_up", "direction", "confidence", "agreement", "diversity"):
                        self.assertIn(required_response_lines(result)[field], text)
                    self.assertIn(required_response_lines(result)["probability_note"], text)
                    self.assertEqual(text.count("### "), 3)

    def test_rtx_style_quote_valuation_news_digest_without_quant_is_rejected(self):
        # User-reported shape only. Prices/news are not verified market fixtures.
        text = "RTX 简析\n行情与走势\n公司与估值\n新闻观察\n综合观察\n数据缺口：不足以判断短期走势。"
        for probability, allowed in ((.613824, True), (None, False)):
            with self.subTest(probability=probability):
                with self.assertRaisesRegex(ValueError, "prob_up"):
                    validate_response_text(report(probability, "RTX", allowed=allowed), text)

    def test_every_required_field_is_checked_including_optional_diversity(self):
        result = report()
        original = render_final_response(result)
        for name, line in required_response_lines(result).items():
            with self.subTest(field=name):
                with self.assertRaisesRegex(ValueError, name):
                    validate_response_text(result, original.replace(line, ""))
        result["consensus"].pop("diversity")
        validate_response_text(result, render_final_response(result))

    def test_replaced_or_extra_probability_and_native_metadata_are_rejected(self):
        result = report()
        text = render_final_response(result)
        for original, replacement in (("P(up)：61.4%", "P(up)：67%"), ("Confidence：medium", "Confidence：72%"), ("（agreement）：high", "（agreement）：81%")):
            with self.subTest(value=replacement):
                with self.assertRaisesRegex(ValueError, "delivery contract"):
                    validate_response_text(result, text.replace(original, replacement))
        with self.assertRaisesRegex(ValueError, "conflicting Quant field"):
            validate_response_text(result, text + "\n- 上涨概率 P(up)：99%\n")

    def test_invalid_or_vetoed_probability_has_explicit_reason_and_no_number(self):
        for probability, allowed in ((None, True), (True, True), (float("nan"), True), (.91, False)):
            result = report(probability, "RTX", allowed=allowed)
            result["adversarial_audit"]["vetoes"] = ["Synthetic missing source calendar"]
            text = render_final_response(result)
            validate_response_text(result, text)
            self.assertIn("上涨概率：不可用", text)
            self.assertIn("概率不可用原因：Synthetic missing source calendar", text)
            self.assertNotIn("上涨概率 P(up)：", text)


class FrozenDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # 21 synthetic sessions reproduce the short-history failure, not RTX data.
        value = collection(21, ticker="RTX")
        value["domains"]["news"]["data"]["articles"] = [article(title="RTX synthetic unconfirmed event", tickers=["RTX"])]
        path = self.root / "collection.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.analysis = run_analysis(freeze_snapshot(path, self.root / "snapshots"), self.root / "research")
        self.response = self.analysis / "final_response.md"

    def test_real_short_history_pipeline_explicitly_abstains_and_brief_is_deliverable(self):
        result = json.loads((self.analysis / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(result["snapshot"]["market_bar_count"], 21)
        self.assertEqual(result["snapshot"]["request"]["intent"], "forecast")
        self.assertEqual(result["analysis_status"], "abstain")
        self.assertTrue(all(item["status"] == "insufficient_data" for item in result["researchers"].values() if item["result_role"] == "forecast"))
        self.assertIsNone(result["researchers"]["factor_backtest"]["prob_up"])
        validation = validate_final_response(self.analysis, self.response)
        self.assertIsNone(validation["prob_up"])
        self.assertIn("上涨概率：不可用", self.response.read_text(encoding="utf-8"))
        self.assertIn("概率不可用原因：", self.response.read_text(encoding="utf-8"))

    def test_modified_report_cannot_substitute_another_ticker_or_probability(self):
        path = self.analysis / "report.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        result["consensus"]["prob_up"] = .67
        path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "frozen Quant"):
            validate_final_response(self.analysis, self.response)

    def test_missing_or_tampered_freeze_receipt_is_rejected(self):
        path = self.analysis / "news_result.freeze.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["sha256"] = "fake"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            validate_final_response(self.analysis, self.response)

    def test_incomplete_stage_receipts_do_not_authorize_delivery(self):
        path = self.analysis / "report.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["stages"] = [row for row in value["stages"] if row["stage"] != "news_freeze"]
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stage receipts"):
            validate_final_response(self.analysis, self.response)

    def test_cli_checks_actual_prepared_text_and_returns_error_on_probability_omission(self):
        spec = importlib.util.spec_from_file_location("delivery_cli_test", ROOT / "stock-data/scripts/quant_research.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        arguments = ["validate-response", "--analysis-dir", str(self.analysis), "--response-file", str(self.response)]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(arguments)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["delivery_status"], "validated")
        self.response.write_text("RTX 简析：行情、估值和新闻观察。", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(arguments)
        self.assertEqual(code, 2)
        self.assertIn("prob_up", json.loads(stderr.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
