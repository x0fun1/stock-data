"""Deterministic Quant/News cross-validation without blending probabilities.

Quant and News stay frozen and independent.  This module only *describes* their
relation with a fixed template: it never computes an alignment score, never
averages the two directions, and never emits a joint probability.  A claimable
Quant direction is compared with the host AI's direct, major News labels; every
other combination is disclosed as unavailable, partial or mixed.
"""

from __future__ import annotations

from typing import Any

from .consensus import (
    DIRECTION_CLAIM_UNAVAILABLE,
    derive_direction_claim_status,
    direction_claim_is_claimable,
)

RELATION_ALIGNED = "aligned"
RELATION_CONFLICTING = "conflicting"
RELATION_MIXED_NEWS = "mixed_news"
RELATION_NEWS_UNCLEAR = "news_unclear"
RELATION_NEWS_NOT_ASSESSED = "news_not_assessed"
RELATION_NEWS_PARTIAL = "news_partial"
RELATION_QUANT_UNAVAILABLE = "quant_unavailable"
RELATION_BOTH_UNAVAILABLE = "both_unavailable"

RELATIONS = (
    RELATION_ALIGNED,
    RELATION_CONFLICTING,
    RELATION_MIXED_NEWS,
    RELATION_NEWS_UNCLEAR,
    RELATION_NEWS_NOT_ASSESSED,
    RELATION_NEWS_PARTIAL,
    RELATION_QUANT_UNAVAILABLE,
    RELATION_BOTH_UNAVAILABLE,
)

_LEAN_LABEL = {"up": "偏上涨", "down": "偏下跌", "flat": "持平"}


def _quant_lean(consensus: dict[str, Any]) -> tuple[str, str]:
    """Return (lean, claim_status) and never turn a diagnostic into a direction."""

    probability = consensus.get("prob_up")
    evidence = consensus.get("evidence") or {}
    status = consensus.get("direction_claim_status")
    if not isinstance(status, str) or not status:
        status = derive_direction_claim_status(
            probability,
            edge_state=evidence.get("directional_edge_state"),
            independent_supported=bool(evidence.get("directional_edge_independent_supported")),
            replay_interval_excludes_zero=bool(
                evidence.get("directional_edge_replay_interval_excludes_zero")
            ),
        )
    if probability is None or status == DIRECTION_CLAIM_UNAVAILABLE:
        return "unavailable", status
    if not direction_claim_is_claimable(status):
        return "abstain", status
    lean = str(consensus.get("model_lean") or "unavailable")
    return lean if lean in _LEAN_LABEL else "unavailable", status


def _counts(news_result: dict[str, Any]) -> tuple[dict[str, int] | None, dict[str, int], str | None]:
    ai = news_result.get("ai_lean") or {}
    coverage = ai.get("coverage") or {}
    direct = coverage.get("direct_label_counts")
    direct_counts = (
        {label: int(direct.get(label) or 0) for label in ("bullish", "bearish", "neutral", "mixed", "unclear")}
        if isinstance(direct, dict) else None
    )
    major = {"bullish": 0, "bearish": 0}
    for row in ai.get("major_direct_items") or []:
        if isinstance(row, dict) and row.get("lean") in major:
            major[row["lean"]] += 1
    return direct_counts, major, ai.get("status")


def _relation(
    *, quant_lean: str, news_status: str | None, direct: dict[str, int] | None, major: dict[str, int],
) -> str:
    news_assessed = news_status in {"assessed", "partial"}
    quant_available = quant_lean not in {"unavailable"}
    if not quant_available and not news_assessed:
        return RELATION_BOTH_UNAVAILABLE
    if not quant_available:
        return RELATION_QUANT_UNAVAILABLE
    if not news_assessed:
        return RELATION_NEWS_NOT_ASSESSED
    bullish = (direct or {}).get("bullish", 0)
    bearish = (direct or {}).get("bearish", 0)
    # A conflict needs a one-sided *major direct* item against the Quant lean.
    if quant_lean == "up" and major["bearish"] >= 1 and major["bullish"] == 0:
        return RELATION_CONFLICTING
    if quant_lean == "down" and major["bullish"] >= 1 and major["bearish"] == 0:
        return RELATION_CONFLICTING
    if major["bullish"] and major["bearish"]:
        return RELATION_MIXED_NEWS
    if bullish and bearish:
        return RELATION_MIXED_NEWS
    if news_status == "partial":
        return RELATION_NEWS_PARTIAL
    if quant_lean == "up" and bullish >= 1 and bearish == 0:
        return RELATION_ALIGNED
    if quant_lean == "down" and bearish >= 1 and bullish == 0:
        return RELATION_ALIGNED
    return RELATION_NEWS_UNCLEAR


def _explanation_lines(
    *, relation: str, quant_lean: str, claim_status: str, news_status: str | None,
    direct: dict[str, int] | None, major: dict[str, int],
) -> list[str]:
    quant_text = (
        f"Quant={_LEAN_LABEL[quant_lean]}（direction_claim_status={claim_status}）"
        if quant_lean in _LEAN_LABEL
        else f"Quant=方向 abstain（direction_claim_status={claim_status}）"
    )
    counts = direct or {}
    news_text = (
        f"News AI 状态={news_status}；直接利多{counts.get('bullish', 0)}条、"
        f"利空{counts.get('bearish', 0)}条、中性{counts.get('neutral', 0)}条、"
        f"混合{counts.get('mixed', 0)}条、不明{counts.get('unclear', 0)}条；"
        f"重大直接利多{major['bullish']}条、重大直接利空{major['bearish']}条"
    )
    templates = {
        RELATION_CONFLICTING: "Quant 方向与重大直接新闻方向相反，属于冲突；两者并列保留，不互相抵消。",
        RELATION_ALIGNED: "Quant 方向与直接新闻方向一致，但这仍不是联合概率或因果证明。",
        RELATION_MIXED_NEWS: "新闻内部同时存在直接利多与利空（或重大条目相反），无法归为单一方向。",
        RELATION_NEWS_UNCLEAR: "新闻没有可用的直接方向条目，综合结论不将词库或模糊标题当作方向。",
        RELATION_NEWS_NOT_ASSESSED: "新闻未评估，综合只报告 Quant 与新闻缺失状态，不推断消息面方向。",
        RELATION_NEWS_PARTIAL: "新闻仅部分覆盖，方向关系只能在不完整证据上描述。",
        RELATION_QUANT_UNAVAILABLE: "Quant 方向结论 abstain，新闻方向不能替代或补全 Quant 概率。",
        RELATION_BOTH_UNAVAILABLE: "Quant 与 News 均不可用，综合结论不可计算。",
    }
    return [
        quant_text + "；" + news_text,
        templates.get(relation, "综合关系不可计算。"),
        "禁止将 Quant 与 News 方向或概率平均、校准或合成为单一上涨概率。",
    ]


def cross_validate(consensus: dict[str, Any], news_result: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the frozen Quant/News relation with deterministic templates."""

    consensus = consensus or {}
    news_result = news_result or {}
    quant_lean, claim_status = _quant_lean(consensus)
    direct, major, news_status = _counts(news_result)
    relation = _relation(
        quant_lean=quant_lean, news_status=news_status, direct=direct, major=major,
    )
    return {
        "relation": relation,
        "quant_lean": quant_lean,
        "quant_direction_claim_status": claim_status,
        "quant_prob_up": consensus.get("prob_up"),
        "news_status": news_status,
        "news_direct_counts": direct or {},
        "news_major_direct_counts": major,
        "explanation_lines": _explanation_lines(
            relation=relation, quant_lean=quant_lean, claim_status=claim_status,
            news_status=news_status, direct=direct, major=major,
        ),
        "score_fields": "none",
        "probability_blending": "prohibited_in_v1",
        "joint_probability": None,
        "probability_unchanged": True,
        "basis": "deterministic_template_no_llm_no_averaging",
    }
