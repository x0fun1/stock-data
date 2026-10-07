"""Quant fields must survive synthesis and display; synthetic values are not defaults."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stock-data/scripts"))
from quant_research.report import build_agent_summary, render_markdown
from quant_research.synthesis import build_synthesis


def report(probability=.613824, ticker="SYNTH", *, news_direction="negative", allowed=True):
    snapshot = {"ticker": ticker, "market": "US", "horizon": "5D", "snapshot_id": "synthetic_output_fixture", "asof_timestamp": "2024-03-15T21:00:00Z", "warnings": []}
    quant = {"snapshot": snapshot, "consensus": {"prob_up": probability, "direction": "bullish" if probability is None or isinstance(probability, bool) or probability >= .5 else "bearish", "confidence": "medium", "agreement": "high", "diversity": "low", "status": "ensemble"},
             "adversarial_audit": {"may_report_direction": allowed, "status": "pass" if allowed else "veto", "vetoes": [], "warnings": []}, "researchers": {}}
    news = {**snapshot, "status": "not_assessed" if news_direction == "unknown" else "complete", "overall_direction": news_direction, "confidence": "low", "sentiment": {}, "risks": [], "prob_up": .999, "direction": "bearish", "agreement": "low", "diversity": "high"}
    return {**quant, "analysis_id": "synthetic_output_case", "analysis_status": "complete_with_limitations" if allowed else "abstain", "stages": [], "news_result": news, "synthesis": build_synthesis(quant, news)}


class QuantOutputTests(unittest.TestCase):
    def test_final_cli_schema_is_utf8_readable_and_declares_native_quant_fields(self):
        script = Path(__file__).resolve().parents[1] / "stock-data/scripts/quant_research.py"
        output = subprocess.run([sys.executable, str(script), "schema", "--kind", "final"], check=True, capture_output=True)
        contract = json.loads(output.stdout.decode("utf-8"))["synthesis_quant"]
        self.assertEqual(contract["missing_probability_display"], "上涨概率：不可用")
        self.assertEqual(contract["required"], ["prob_up", "direction", "confidence", "agreement"])
        self.assertFalse(contract["news_may_change_prob_up"])

    def assert_quant_fields(self, value):
        source = value["consensus"]
        expected = {key: source[key] for key in ("prob_up", "direction", "confidence", "agreement", "diversity")}
        self.assertEqual(value["synthesis"]["quant"], expected)
        summary = build_agent_summary(value)
        self.assertEqual({key: summary["sections"]["quant"][key] for key in expected}, expected)
        self.assertEqual(summary["sections"]["final_synthesis"]["quant"], expected)
        self.assertEqual(value["synthesis"]["quant_field_source"], "quant_result.json.consensus")
        return summary

    def test_each_news_outcome_preserves_probability_and_native_quant_metadata(self):
        for direction in ("positive", "negative", "mixed", "unknown"):
            with self.subTest(news=direction):
                value = report(news_direction=direction)
                original = copy.deepcopy(value["consensus"])
                summary = self.assert_quant_fields(value)
                self.assertEqual(value["consensus"], original)
                self.assertIn("61.4%", summary["sections"]["final_synthesis"]["probability_note"])
                if direction == "negative":
                    self.assertEqual(value["synthesis"]["overall_confidence"], "low")
                    self.assertEqual(value["synthesis"]["quant"]["confidence"], "medium")
                    self.assertEqual(summary["sections"]["final_synthesis"]["news_relationship"], "冲突")
                elif direction == "unknown":
                    self.assertEqual(summary["sections"]["final_synthesis"]["news_relationship"], "消息面交叉验证未评估")

    def test_final_markdown_displays_target_specific_probability_and_all_five_fields(self):
        for ticker, probability, text in (("SYNTH-A", .43127, "43.1%"), ("SYNTH-B", .75896, "75.9%"), ("SYNTH-C", .5, "50.0%")):
            value = report(probability, ticker)
            markdown = render_markdown(value)
            quant_section = markdown.split("## 2. Quant 数据面", 1)[1].split("## 3.", 1)[0]
            final_section = markdown.split("## 8. Final Synthesis", 1)[1]
            for section in (quant_section, final_section):
                for content in (f"上涨概率 P(up)：{text}", f"方向：{value['consensus']['direction']}", "Quant Confidence：medium", "模型一致度（agreement）：high", "证据多样性（diversity）：low"):
                    self.assertIn(content, section)
            self.assertIn(f"Quant 当前对 {ticker} 的 5D 窗口给出 {text}", final_section)
            self.assertIn(f"不改变 Quant 的 {text} 原始概率", final_section)

    def test_unavailable_invalid_and_vetoed_probabilities_are_explicit_not_filled(self):
        for probability, allowed in ((None, True), (True, True), (float("nan"), True), (float("inf"), True), (-.1, True), (1.2, True), (.9123, False)):
            with self.subTest(probability=probability, allowed=allowed):
                value = report(probability, allowed=allowed)
                summary = build_agent_summary(value)
                markdown = render_markdown(value)
                self.assertIsNone(value["synthesis"]["quant"]["prob_up"])
                self.assertIsNone(summary["sections"]["quant"]["prob_up"])
                self.assertIsNone(summary["sections"]["final_synthesis"]["quant"]["prob_up"])
                self.assertIn("上涨概率：不可用", markdown.split("## 8. Final Synthesis", 1)[1])
                self.assertNotIn("上涨概率 P(up)：", markdown)
                self.assertNotIn("91.2%", markdown)

    def test_zero_one_and_near_boundary_values_remain_available_and_not_false_certainty(self):
        for probability, text in ((0, "0.0%"), (1, "100.0%"), (.00001, "0.001%"), (.99999, "99.999%")):
            value = report(probability)
            self.assert_quant_fields(value)
            markdown = render_markdown(value)
            self.assertIn(f"上涨概率 P(up)：{text}", markdown)
            self.assertIn(f"unchanged P(up): {text}", markdown)
            if 0 < probability < 1:
                self.assertNotIn("100.0%", markdown)
                self.assertNotIn("0.0%", markdown)

    def test_optional_diversity_is_not_filled_when_quant_does_not_supply_it(self):
        value = report()
        value["consensus"].pop("diversity")
        value["synthesis"] = build_synthesis(value, value["news_result"])
        self.assertNotIn("diversity", value["synthesis"]["quant"])
        self.assertNotIn("diversity", build_agent_summary(value)["sections"]["final_synthesis"]["quant"])
        self.assertNotIn("证据多样性（diversity）", render_markdown(value))

    def test_numeric_metadata_retains_source_scale_without_inventing_percentages(self):
        value = report()
        value["consensus"].update(confidence=.73, agreement=.81, diversity=.54)
        value["synthesis"] = build_synthesis(value, value["news_result"])
        self.assert_quant_fields(value)
        self.assertEqual(value["synthesis"]["overall_confidence"], "unavailable")
        markdown = render_markdown(value)
        self.assertIn("Quant Confidence：0.73", markdown)
        self.assertIn("模型一致度（agreement）：0.81", markdown)
        self.assertIn("证据多样性（diversity）：0.54", markdown)


if __name__ == "__main__":
    unittest.main()
