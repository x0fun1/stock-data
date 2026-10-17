"""Validate a prepared user reply against completed, independently frozen stages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .news import load_frozen_result
from .gate_policy import analysis_status
from .report import required_response_lines
from .synthesis import build_synthesis

_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_LEADING_MARKER = re.compile(r"^(?:(?:[-•*]|\(?\d{1,3}[.)、]|[①-⑳])\s*)+")


def _normalized_line(line: str) -> str:
    """Fold presentation-level differences so a reformatted value is still checked."""
    folded = _INVISIBLE.sub("", line.replace("\\_", "_").replace("\\*", "*"))
    for _ in range(3):
        folded = folded.strip().strip("*_`#~|\"'“”‘’>").strip()
        folded = folded.replace("**", "").replace("`", "").strip()
        folded = _LEADING_MARKER.sub("", folded).strip()
    folded = (folded.replace("：", ":").replace("（", "(").replace("）", ")")
              .replace("，", ",").replace("；", ";"))
    return re.sub(r"\s+", " ", folded).rstrip(":;。.").strip()


def validate_response_text(result: dict[str, Any], text: str) -> None:
    """Require literal bound fields; reject omitted or conflicting labeled values.

    Validation runs on the visible text: HTML comments and zero-width characters cannot
    satisfy a required field or hide a conflicting one, and numbered/quoted list renderings
    are folded before the labeled-value comparison.
    """
    visible = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    expected = required_response_lines(result)
    lines = {line.strip() for line in visible.splitlines()}
    normalized_set = {_normalized_line(line) for line in visible.splitlines()}
    missing = [name for name, value in expected.items() if value not in lines and _normalized_line(value) not in normalized_set]
    prefixes = ("- 上涨概率", "- 方向：", "- Quant Confidence：", "- 置信度支持度评分：", "- 置信度评分含义", "- OOS 预测证据等级", "- 报告状态：", "- 模型一致度", "- 原始路径估计均值：", "- 校准概率：", "- 联合概率校准：", "- 低置信度原因：", "- 证据多样性", "- 预测窗口：", "- 标的：", "- 执行状态：", "- 消息面与 Quant 的关系", "- 消息面逐条依据：")
    # The allow-list is every required literal line: a reformatted or changed
    # labeled value (any prefix) is a conflict, while identity/status lines are
    # covered because they are part of the required response contract.
    required_lines = {_normalized_line(value) for value in expected.values()}
    # Compare fold-normalized labeled lines so emphasis, bullet style or
    # punctuation cannot smuggle a conflicting quant/identity value.
    conflicting = any(
        any(_normalized_line(line).startswith(_normalized_line(prefix)) for prefix in prefixes)
        and _normalized_line(line) not in required_lines
        for line in lines
    )
    if missing or conflicting:
        raise ValueError("Final response failed Quant delivery contract: " + ", ".join(missing + (["conflicting Quant field"] if conflicting else [])))


def validate_final_response(analysis_dir: Path, response_file: Path) -> dict[str, Any]:
    quant, _ = load_frozen_result(analysis_dir / "quant_result.json")
    news, _ = load_frozen_result(analysis_dir / "news_result.json")
    result = json.loads((analysis_dir / "report.json").read_text(encoding="utf-8"))
    # A modified summary/report cannot substitute another ticker, probability,
    # audit, or News result for the completed frozen stages.
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in quant.items()):
        raise ValueError("Report does not match frozen Quant result")
    synthesis = build_synthesis(quant, news)
    if result.get("news_result") != news or result.get("synthesis") != synthesis:
        raise ValueError("Report does not match frozen News/synthesis result")
    adversarial = quant.get("adversarial_audit") or {}
    derived_analysis_status = analysis_status(
        may_report_direction=bool(adversarial.get("may_report_direction")),
        reporting_state=str(adversarial.get("reporting_status", "veto")),
        researcher_statuses=[row.get("status") for row in (quant.get("researchers") or {}).values()],
        news_status=str(news.get("status", "not_assessed")),
    )
    derived_reporting_status = str(adversarial.get("reporting_status", "veto"))
    # Status fields are re-derived from the frozen stage results instead of being trusted
    # from the editable report, and every stage receipt must match its authoritative outcome.
    if (result.get("analysis_status") != derived_analysis_status
            or result.get("reporting_status") != derived_reporting_status):
        raise ValueError("Report status fields do not match the derived status of the frozen stages")
    stage_rows = {row.get("stage"): row for row in result.get("stages", []) if isinstance(row, dict)}
    expected_stage_status = {
        "data_validation": str((quant.get("snapshot") or {}).get("data_validation", {}).get("reporting_status", "veto")),
        "quant_freeze": "verified",
        "news_freeze": "verified",
        "audit": adversarial.get("status"),
        "news": str(news.get("status", "not_assessed")),
        "cross_validation": (synthesis or {}).get("alignment"),
        "final": derived_analysis_status,
    }
    for stage, expected in expected_stage_status.items():
        if (stage_rows.get(stage) or {}).get("status") != expected:
            raise ValueError(f"Stage receipt for {stage!r} does not match the authoritative stage result")
    if (stage_rows.get("quant") or {}).get("paths") != {key: row.get("status") for key, row in (quant.get("researchers") or {}).items()}:
        raise ValueError("Stage receipt paths do not match the frozen researcher results")
    expected_prediction_eligible = bool((quant.get("snapshot") or {}).get("data_validation", {}).get("forecast_eligible", False))
    if (stage_rows.get("data_validation") or {}).get("prediction_eligible") != expected_prediction_eligible:
        raise ValueError("Stage receipt for 'data_validation' does not match the frozen snapshot gate")
    validate_response_text(result, response_file.read_text(encoding="utf-8-sig"))
    return {"status": "success", "delivery_status": "validated", "analysis_id": result["analysis_id"],
            "analysis_status": derived_analysis_status, "reporting_status": derived_reporting_status,
            "snapshot_id": quant["snapshot"]["snapshot_id"],
            "ticker": quant["snapshot"]["ticker"], "horizon": quant["snapshot"]["horizon"],
            "asof_timestamp": quant["snapshot"]["asof_timestamp"], "response_file": str(response_file),
            "prob_up": result["synthesis"]["quant"]["prob_up"]}
