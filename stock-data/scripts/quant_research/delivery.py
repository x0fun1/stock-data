"""Validate a prepared reply against the frozen analysis bundle and its stages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .forecast_policy import POLICY_VERSION, REQUIRED_STAGES
from .news import load_frozen_result
from .report import required_response_lines

_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_LEADING_MARKER = re.compile(r"^(?:(?:[-•*]|\(?\d{1,3}[.)、]|[①-⑳])\s*)+")
LABELS = ("标的", "新闻窗口", "新闻", "交易信号")
FORBIDDEN = (
    "上涨" + "概率", "下跌" + "概率", "模型" + "倾向", "方向" + "优势",
    "综合" + "评分", "综合" + "看涨", "综合" + "看跌",
    "建议" + "买入", "建议" + "卖出", "买入" + "建议", "卖出" + "建议", "仓位" + "建议",
    "Quant " + "Confidence", "置信度" + "评分", "校准" + "概率",
)


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
    """Require literal bound fields and reject forbidden prediction language."""

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
            "Final response failed delivery contract: "
            + ", ".join(missing + (["conflicting/forbidden field"] if conflicting else []))
        )


def require_ai_news_coverage(news: dict[str, Any]) -> None:
    """Require a consistent per-headline AI review, and disclose partial coverage."""

    ai = news.get("ai_lean") or {}
    coverage = ai.get("coverage") or {}
    candidate_count = len(news.get("ai_lean_candidates") or [])
    if not candidate_count:
        return
    status = ai.get("status")
    assessed_count = coverage.get("assessed_count")
    if status not in {"assessed", "partial"}:
        raise ValueError("Final audit rejected: every canonical news headline needs a host AI assessment")
    if coverage.get("candidate_count") != candidate_count:
        raise ValueError("Final audit rejected: AI coverage counts do not match the canonical candidates")
    if not isinstance(assessed_count, int) or isinstance(assessed_count, bool):
        raise ValueError("Final audit rejected: AI coverage has no assessed count")
    if len(ai.get("items") or []) != assessed_count:
        raise ValueError("Final audit rejected: AI items do not match the assessed count")
    if assessed_count > candidate_count:
        raise ValueError("Final audit rejected: AI assessed count exceeds the candidate count")
    if status == "assessed" and assessed_count != candidate_count:
        raise ValueError("Final audit rejected: an assessed AI review must cover every canonical headline")
    if status == "partial" and not (news.get("warnings") or []):
        raise ValueError("Final audit rejected: a partial AI review must disclose why it is partial")


def _validate_stages(result: dict[str, Any]) -> dict[str, Any]:
    stages = result.get("stages")
    if not isinstance(stages, list) or [row.get("stage") for row in stages] != list(REQUIRED_STAGES):
        raise ValueError("Final audit rejected: stage receipts must be unique and exactly the required sequence")
    run_id = result.get("run_id")
    snapshot_id = result.get("snapshot_id")
    allowed = {"complete", "partial", "skipped", "not_applicable", "not_assessed", "blocked", "insufficient_data"}
    for row in stages:
        if row.get("status") not in allowed:
            raise ValueError(f"Final audit rejected: invalid stage status {row.get('status')!r}")
        if row.get("status") != "complete" and not row.get("reason"):
            raise ValueError(f"Final audit rejected: stage {row.get('stage')} has no reason")
        if row.get("run_id") != run_id or row.get("snapshot_id") != snapshot_id:
            raise ValueError(f"Final audit rejected: stage {row.get('stage')} is bound to another run or snapshot")
        if row.get("policy_version") != POLICY_VERSION:
            raise ValueError(f"Final audit rejected: stage {row.get('stage')} uses another policy version")
    return {"stage_count": len(stages), "order": [row["stage"] for row in stages]}


def validate_final_response(analysis_dir: Path, response_file: Path) -> dict[str, Any]:
    result = json.loads((analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Final audit rejected: analysis.json is not an object")  # noqa: TRY004 - repo-wide validation contract
    stage_summary = _validate_stages(result)
    frozen_news, _news_receipt = load_frozen_result(analysis_dir / "news_result.json")
    if frozen_news != result.get("news_result"):
        raise ValueError("Final audit rejected: news_result.json does not match the frozen News stage")
    require_ai_news_coverage(frozen_news)
    validate_response_text(result, response_file.read_text(encoding="utf-8-sig"))
    snapshot = result.get("snapshot") or {}
    return {
        "status": "success",
        "delivery_status": "validated",
        "analysis_id": result.get("analysis_id") or result.get("run_id"),
        "analysis_status": result.get("analysis_status"),
        "reporting_status": result.get("reporting_status"),
        "run_id": result.get("run_id"),
        "snapshot_id": result.get("snapshot_id"),
        "policy_version": result.get("policy_version"),
        "ticker": snapshot.get("ticker"),
        "asof_timestamp": snapshot.get("asof_timestamp"),
        "response_file": str(response_file),
        "stages": stage_summary,
    }
