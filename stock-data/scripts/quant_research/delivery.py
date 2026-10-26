"""Validate a prepared reply against the frozen analysis bundle and its stages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .forecast_policy import POLICY_VERSION, REQUIRED_STAGES
from .news import load_frozen_result
from .report import reporting_status_for, required_response_lines

_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_LEADING_MARKER = re.compile(r"^(?:(?:[-•*]|\(?\d{1,3}[.)、]|[①-⑳])\s*)+")
LABELS = ("研究窗口", "预测目标", "上涨概率", "模型倾向", "方向优势", "生产路径", "验证", "相对因果基率技能", "新闻")
FORBIDDEN = ("Quant Confidence", "置信度评分", "置信度支持度评分", "校准概率", "联合概率校准", "报告状态", "模型一致度", "概率区间", "OOS 预测证据等级")


def _normalized_line(line: str) -> str:
    folded = _INVISIBLE.sub("", line.replace("\\_", "_").replace("\\*", "*"))
    for _ in range(3):
        folded = folded.strip().strip("*_`#~|\"'“”‘’>").strip()
        folded = folded.replace("**", "").replace("`", "").strip()
        folded = _LEADING_MARKER.sub("", folded).strip()
    folded = (folded.replace("：", ":").replace("（", "(").replace("）", ")")
              .replace("，", ",").replace("；", ";"))
    return re.sub(r"\s+", " ", folded).rstrip(":;。.").strip()


def validate_response_text(result: dict[str, Any], text: str) -> None:
    """Require literal bound fields and reject conflicting labeled values."""

    visible = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    expected = required_response_lines(result)
    raw_lines = [line.strip() for line in visible.splitlines() if line.strip()]
    normalized = [_normalized_line(line) for line in raw_lines]
    expected_normalized = {_normalized_line(value) for value in expected.values()}
    missing = [name for name, value in expected.items() if _normalized_line(value) not in normalized]
    conflicting = []
    for line in raw_lines:
        folded = _normalized_line(line)
        for label in LABELS:
            if folded.startswith(_normalized_line(label)) and folded not in expected_normalized:
                conflicting.append(folded)
                break
        if any(token in line for token in FORBIDDEN):
            conflicting.append(folded)
    if missing or conflicting:
        raise ValueError(
            "Final response failed Quant delivery contract: "
            + ", ".join(missing + (["conflicting/forbidden field"] if conflicting else []))
        )


def _validate_stages(result: dict[str, Any]) -> dict[str, Any]:
    stages = result.get("stages")
    if not isinstance(stages, list) or [row.get("stage") for row in stages] != list(REQUIRED_STAGES):
        raise ValueError("Final audit rejected: stage receipts must be unique and exactly the required sequence")
    run_id = result.get("run_id")
    snapshot_id = result.get("snapshot_id")
    for row in stages:
        if row.get("status") not in {"complete", "partial", "skipped", "not_applicable", "not_assessed", "blocked", "insufficient_data"}:
            raise ValueError(f"Final audit rejected: invalid stage status {row.get('status')!r}")
        if row.get("status") != "complete" and not row.get("reason"):
            raise ValueError(f"Final audit rejected: stage {row.get('stage')} has no reason")
        if row.get("run_id") != run_id or row.get("snapshot_id") != snapshot_id:
            raise ValueError(f"Final audit rejected: stage {row.get('stage')} is bound to another run or snapshot")
        if row.get("policy_version") != POLICY_VERSION:
            raise ValueError(f"Final audit rejected: stage {row.get('stage')} uses another policy version")
    return {"stage_count": len(stages), "order": [row["stage"] for row in stages]}


def require_ai_news_coverage(news: dict[str, Any]) -> None:
    """Refuse delivery when collected canonical headlines lack per-item AI review."""
    ai = news.get("ai_lean") or {}
    coverage = ai.get("coverage") or {}
    candidate_count = len(news.get("ai_lean_candidates") or [])
    if candidate_count and (ai.get("status") != "assessed"
                            or coverage.get("candidate_count") != candidate_count
                            or coverage.get("assessed_count") != candidate_count
                            or len(ai.get("items") or []) != candidate_count):
        raise ValueError("Final audit rejected: every canonical news headline needs a host AI assessment")


def validate_final_response(analysis_dir: Path, response_file: Path) -> dict[str, Any]:
    result = json.loads((analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Final audit rejected: analysis.json is not an object")  # noqa: TRY004 - repo-wide validation contract
    stage_summary = _validate_stages(result)
    frozen_news, _news_receipt = load_frozen_result(analysis_dir / "news_result.json")
    if frozen_news != result.get("news_result"):
        raise ValueError("Final audit rejected: news_result.json does not match the frozen News stage")
    require_ai_news_coverage(frozen_news)
    consensus = result.get("consensus") or {}
    production = consensus.get("production") or {}
    if consensus.get("prob_up") != production.get("prob_up"):
        raise ValueError("Final audit rejected: consensus probability differs from the production path")
    if consensus.get("trading_signal_eligible") is not False:
        raise ValueError("Final audit rejected: trading_signal_eligible must remain false")
    if consensus.get("aggregation_method") != "single_path" or int(consensus.get("production_path_count") or 0) != 1:
        raise ValueError("Final audit rejected: production must use exactly one path")
    if consensus.get("prob_up") is not None and not consensus.get("production_probability_eligible"):
        raise ValueError("Final audit rejected: ineligible production probability is present")
    validate_response_text(result, response_file.read_text(encoding="utf-8-sig"))
    snapshot = result.get("snapshot") or {}
    return {
        "status": "success",
        "delivery_status": "validated",
        "analysis_id": result.get("analysis_id") or result.get("run_id"),
        "analysis_status": result.get("analysis_status") or consensus.get("status"),
        "reporting_status": reporting_status_for(result),
        "run_id": result.get("run_id"),
        "snapshot_id": result.get("snapshot_id"),
        "policy_version": result.get("policy_version"),
        "ticker": snapshot.get("ticker"),
        "horizon": snapshot.get("horizon"),
        "asof_timestamp": snapshot.get("asof_timestamp"),
        "response_file": str(response_file),
        "prob_up": consensus.get("prob_up"),
        "production_probability_eligible": bool(consensus.get("production_probability_eligible")),
        "directional_edge_supported": bool(consensus.get("directional_edge_supported")),
        "stages": stage_summary,
    }
