"""Validate a prepared user reply against completed, independently frozen stages."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .news import load_frozen_result
from .report import required_response_lines
from .synthesis import build_synthesis


def _normalized_line(line: str) -> str:
    """Fold presentation-level differences so a reformatted value is still checked."""
    folded = line.replace("\\_", "_").replace("\\*", "*")
    folded = folded.strip().strip("*_`#>").strip()
    folded = folded.replace("**", "").replace("`", "").strip()
    folded = re.sub(r"^\s*[-•*]\s+", "", folded)
    folded = (folded.replace("：", ":").replace("（", "(").replace("）", ")")
              .replace("，", ",").replace("；", ";"))
    return re.sub(r"\s+", " ", folded).rstrip(":;。.").strip()


def validate_response_text(result: dict[str, Any], text: str) -> None:
    """Require literal bound fields; reject omitted or conflicting labeled values."""
    expected = required_response_lines(result)
    lines = {line.strip() for line in text.splitlines()}
    normalized_set = {_normalized_line(line) for line in text.splitlines()}
    missing = [name for name, value in expected.items() if value not in lines and _normalized_line(value) not in normalized_set]
    prefixes = ("- 上涨概率", "- 方向：", "- Quant Confidence：", "- 置信度支持度评分：", "- 置信度评分含义", "- OOS 预测证据等级", "- 报告状态：", "- 模型一致度", "- 原始路径估计均值：", "- 校准概率：", "- 联合概率校准：", "- 低置信度原因：", "- 证据多样性", "- 预测窗口：", "- 标的：", "- 执行状态：", "- 消息面与 Quant 的关系")
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
    if result.get("news_result") != news or result.get("synthesis") != build_synthesis(quant, news):
        raise ValueError("Report does not match frozen News/synthesis result")
    stages = {row["stage"]: row["status"] for row in result.get("stages", [])}
    if stages.get("quant_freeze") != "verified" or stages.get("news_freeze") != "verified" or stages.get("final") != result.get("analysis_status"):
        raise ValueError("Completed Quant/News/final stage receipts are required")
    validate_response_text(result, response_file.read_text(encoding="utf-8-sig"))
    return {"status": "success", "delivery_status": "validated", "analysis_id": result["analysis_id"],
            "analysis_status": result.get("analysis_status"), "reporting_status": result.get("reporting_status"),
            "snapshot_id": quant["snapshot"]["snapshot_id"],
            "ticker": quant["snapshot"]["ticker"], "horizon": quant["snapshot"]["horizon"],
            "asof_timestamp": quant["snapshot"]["asof_timestamp"], "response_file": str(response_file),
            "prob_up": result["synthesis"]["quant"]["prob_up"]}
