"""Validate a prepared user reply against completed, independently frozen stages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .news import load_frozen_result
from .report import required_response_lines
from .synthesis import build_synthesis


def validate_response_text(result: dict[str, Any], text: str) -> None:
    """Require literal bound fields; reject omitted or conflicting labeled values."""
    expected = required_response_lines(result)
    lines = {line.strip() for line in text.splitlines()}
    missing = [name for name, value in expected.items() if value not in lines]
    prefixes = ("- 上涨概率", "- 方向：", "- Quant Confidence：", "- 模型一致度", "- 证据多样性")
    quant_lines = {expected[name] for name in ("prob_up", "direction", "confidence", "agreement", "diversity") if name in expected}
    conflicting = any(line.startswith(prefixes) and line not in quant_lines for line in lines)
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
            "ticker": quant["snapshot"]["ticker"], "horizon": quant["snapshot"]["horizon"],
            "asof_timestamp": quant["snapshot"]["asof_timestamp"], "response_file": str(response_file),
            "prob_up": result["synthesis"]["quant"]["prob_up"]}
