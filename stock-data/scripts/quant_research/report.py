"""Render the fixed-policy report and the mandatory response lines."""

from __future__ import annotations

import json
from typing import Any

from .forecast_policy import (
    DRIFT_SHRINKAGE_K,
    FORECAST_HORIZON_SESSIONS,
    HISTORY_CALENDAR_DAYS,
    MIN_PROSPECTIVE_NONOVERLAP,
    MIN_REPLAY_EFFECTIVE_SAMPLES,
    dividend_label_disclosure,
)
from .news import news_assessed, news_status_label
from .security import markdown_text

AGENT_SUMMARY_BUDGET = 9000
LEAN_LABELS = {"up": "点估计偏上涨", "down": "点估计偏下跌", "flat": "点估计持平", "unavailable": "不可用"}


def _pct(value: Any, digits: int = 1) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "不可估计"
    return f"{float(value) * 100:.{digits}f}%"


def _num(value: Any, digits: int = 4) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "不可估计"
    return f"{float(value):.{digits}f}"


def _window(result: dict[str, Any]) -> dict[str, Any]:
    return (result.get("snapshot") or {}).get("policy") or (result.get("snapshot") or {}).get("manifest_policy") or {}


def _production(result: dict[str, Any]) -> dict[str, Any]:
    return (result.get("consensus") or {}).get("production") or {}


def _evidence(result: dict[str, Any]) -> dict[str, Any]:
    return (result.get("consensus") or {}).get("evidence") or {}


def _target_session(result: dict[str, Any]) -> str | None:
    return (result.get("snapshot") or {}).get("target_session")


def _unavailable_reason(result: dict[str, Any]) -> str:
    codes = (result.get("consensus") or {}).get("reason_codes") or []
    evidence = _evidence(result)
    gaps = []
    if "OOS_SUPPORT_INSUFFICIENT" in codes:
        gaps.append(
            f"有效回放样本 {evidence.get('oos_effective_samples') or 0}/{MIN_REPLAY_EFFECTIVE_SAMPLES}"
        )
    if "NO_VALID_OOS_PROBABILITY" in codes:
        gaps.append("有效OOS概率 0")
    if "TARGET_SESSION_UNRESOLVED" in codes:
        gaps.append("预测目标交易日未解析")
    if "INSUFFICIENT_HISTORY_COVERAGE" in codes:
        gaps.append("52周覆盖不完整")
    if "CALENDAR_UNVERIFIED" in codes:
        gaps.append("交易日历不可验证")
    if "PRICE_BASIS_UNVERIFIED" in codes:
        gaps.append("价格基础未验证")
    if "PREDICTION_INTEGRITY_FAILED" in codes:
        gaps.append("当前概率无法重算")
    if not gaps:
        gaps.append("生产路径A没有可用概率")
    return f"原因码{'/'.join(codes) if codes else 'UNKNOWN'}；实际缺口：{'，'.join(gaps)}"


def reporting_status_for(result: dict[str, Any]) -> str:
    """One definition of "may this run's probability be delivered".

    The previous implementation read ``consensus.production_probability_eligible``
    only.  After the adversarial audit vetoes a run it sets ``consensus.prob_up``
    to ``None`` but leaves that eligibility flag untouched, so the report could
    still say ``reportable`` while carrying no probability at all — a stale flag
    contradicting the frozen payload.
    """

    consensus = result.get("consensus") or {}
    audit = result.get("adversarial_audit") or {}
    probability = consensus.get("prob_up")
    reportable = (
        isinstance(probability, (int, float)) and not isinstance(probability, bool)
        and audit.get("may_report_probability") is True
    )
    return "reportable" if reportable else "unavailable"


def _news_line(result: dict[str, Any]) -> str:
    """Disclose the three News states separately, with the warning count.

    ``partial`` is assessed evidence with disclosed gaps.  Writing it as
    "未评估" is not a stylistic choice: it contradicts the frozen news_result and
    the delivery validator used to accept that contradiction.
    """

    news = result.get("news_result") or {}
    status = news.get("status")
    if not news_assessed(status):
        return "新闻：未提供（不影响Quant资格，也不当作中性证据）"
    state = news_status_label(status)
    warnings = len(news.get("warnings") or [])
    direction = str(news.get("overall_direction") or "unknown")
    regime = str((news.get("expected_volatility_regime") or {}).get("regime") or "unknown")
    return (
        f"新闻：已评估（{state}；{warnings} 条警告；事件方向 {direction}；"
        f"波动状态 {regime}；不改变Quant概率）"
    )


def _edge_line(result: dict[str, Any], *, prospective: int) -> str:
    consensus = result.get("consensus") or {}
    evidence = _evidence(result)
    state = str(evidence.get("directional_edge_state") or "")
    replay = int(evidence.get("replay_valid") or 0)
    if consensus.get("prob_up") is None:
        return "方向优势：不可用（没有可发布的生产概率）"
    if state == "positive_skill_with_estimated_interval_excluding_zero":
        return (
            f"方向优势：已支持（全量前缀回放 {replay} 条，成对损失改善区间下界大于0；"
            f"真实前向 {prospective}/{MIN_PROSPECTIVE_NONOVERLAP}条）"
        )
    if state == "negative_skill_with_estimated_interval_excluding_zero":
        return (
            f"方向优势：无（全量前缀回放 {replay} 条，改善区间上界小于0，劣于纯基率；"
            f"真实前向 {prospective}/{MIN_PROSPECTIVE_NONOVERLAP}条）"
        )
    return (
        f"方向优势：证据不足（全量前缀回放 {replay} 条，改善区间含0；"
        f"真实前向 {prospective}/{MIN_PROSPECTIVE_NONOVERLAP}条）"
    )


def required_response_lines(result: dict[str, Any]) -> dict[str, str]:
    """The exact fixed text block; every line is a delivery contract."""

    consensus = result.get("consensus") or {}
    snapshot = result.get("snapshot") or {}
    window = _window(result)
    decision = window.get("decision_session") or snapshot.get("last_bar_date") or "未知"
    end = window.get("window_end_inclusive") or decision
    target = _target_session(result) or "待定"
    probability = consensus.get("prob_up")
    evidence = _evidence(result)
    transform = ((_production(result).get("probability_transform")) or {})
    k = transform.get("k") if isinstance(transform.get("k"), int) else DRIFT_SHRINKAGE_K
    if isinstance(probability, (int, float)) and not isinstance(probability, bool):
        probability_line = (
            f"上涨概率：{_pct(probability)}（漂移收缩估计 k={k}；未校准，不可作为真实频率）"
        )
    else:
        probability_line = f"上涨概率：不可用（{_unavailable_reason(result)}）"
    prospective = int(evidence.get("prospective_nonoverlap") or 0)
    intervals = evidence.get("relative_baseline_skill") or []
    skill = next((row for row in intervals if isinstance(row, dict) and row.get("primary")), None)
    if skill is None:
        skill = next((row for row in intervals if isinstance(row, dict)), None)
    comparison = evidence.get("baseline_comparison") or {}
    if skill and skill.get("status") == "estimated":
        skill_line = (
            f"相对因果基率技能：{_num(skill.get('mean'))}；区间：[{_num(skill.get('lower'))}, {_num(skill.get('upper'))}]；"
            f"Brier 模型{_num(comparison.get('brier_model'))} vs 基率{_num(comparison.get('brier_causal_base_rate'))} "
            f"vs 常数0.5 {_num(comparison.get('brier_constant_half'))}；AUC {_num(comparison.get('auc'))}；"
            f"有效样本≈{comparison.get('effective_sample_size') if comparison.get('effective_sample_size') is not None else '不可估计'}"
        )
    else:
        skill_line = "相对因果基率技能：不可估计；区间：不可估计"
    replay = evidence.get("replay_nonoverlap")
    coverage = evidence.get("replay_coverage_ratio")
    if isinstance(coverage, (int, float)):
        coverage_text = f"{_pct(coverage)}"
    elif evidence.get("oos_sampling_status") == "unanchored":
        coverage_text = "不适用（相位未锚定，未节流）"
    else:
        coverage_text = "不可估计"
    effective = evidence.get("oos_effective_samples")
    lines = {
        "research_window": f"研究窗口：截至{end}的52周（{HISTORY_CALENDAR_DAYS}个自然日）",
        "forecast_target": f"预测目标：{target}收盘相对{decision}收盘的拆股调整价格涨跌（{FORECAST_HORIZON_SESSIONS}个交易日）",
        "probability": probability_line,
        "model_lean": f"模型倾向：{LEAN_LABELS.get(str(consensus.get('model_lean')), '不可用')}（仅描述点估计）",
        "directional_edge": _edge_line(result, prospective=prospective),
        "production_path": "生产路径：A；B/C为影子研究，D为描述性诊断",
        "validation": (
            f"验证：前缀回放{evidence.get('replay_valid') if evidence.get('replay_valid') is not None else 0}条"
            f"（其中非重叠{replay if replay is not None else 0}条；有效样本≈{effective if effective is not None else '不可估计'}）；"
            f"真实前向{prospective}条；覆盖率{coverage_text}"
        ),
        "relative_skill": skill_line,
        "news": _news_line(result),
    }
    return lines  # noqa: RET504 - the named mapping documents the delivery contract


def build_agent_summary(result: dict[str, Any]) -> dict[str, Any]:
    consensus = result.get("consensus") or {}
    snapshot = result.get("snapshot") or {}
    production = _production(result)
    evidence = _evidence(result)
    lines = required_response_lines(result)
    return {
        "schema": "agent-summary-3.0",
        "ticker": snapshot.get("ticker"),
        "market": snapshot.get("market"),
        "horizon": snapshot.get("horizon"),
        "asof": snapshot.get("asof_timestamp"),
        "decision_session": _window(result).get("decision_session"),
        "policy_version": _window(result).get("policy_version"),
        "headline": {key: lines[key] for key in ("research_window", "forecast_target", "probability", "model_lean")},
        "sections": {
            "quant": {
                "production_probability_eligible": consensus.get("production_probability_eligible"),
                "current_prediction_integrity_pass": consensus.get("current_prediction_integrity_pass"),
                "oos_integrity_pass": consensus.get("oos_integrity_pass"),
                "directional_edge_supported": consensus.get("directional_edge_supported"),
                "trading_signal_eligible": False,
                "probability_label": "uncalibrated_model_estimate",
                "reason_codes": consensus.get("reason_codes") or [],
                "matching_observations": production.get("matching_observations"),
                "causal_base_rate": production.get("causal_base_rate"),
            },
            "replay_and_prospective": {
                "replay_type": evidence.get("replay_type"),
                "replay_nonoverlap": evidence.get("replay_nonoverlap"),
                "replay_total": evidence.get("replay_total"),
                "replay_abstained": evidence.get("replay_abstained"),
                "replay_coverage_ratio": evidence.get("replay_coverage_ratio"),
                "oos_effective_samples": evidence.get("oos_effective_samples"),
                "prospective_nonoverlap": evidence.get("prospective_nonoverlap"),
                "directional_edge_state": evidence.get("directional_edge_state"),
                "primary_evidence": evidence.get("directional_edge_primary_evidence"),
                "baseline_comparison": {
                    key: (evidence.get("baseline_comparison") or {}).get(key)
                    for key in ("observations", "effective_sample_size", "brier_model",
                                "brier_causal_base_rate", "brier_constant_half", "skill", "auc", "verdict")
                },
                "calibration_status": "disabled_by_policy",
            },
            "shadow_predictions": [
                {key: row.get(key) for key in ("path", "probability", "status", "model_version")}
                for row in consensus.get("shadow_predictions") or []
            ],
            "diagnostics": consensus.get("diagnostics") or {},
            "news": {
                "status": (result.get("news_result") or {}).get("status"),
                "state": news_status_label((result.get("news_result") or {}).get("status")),
                "assessed": news_assessed((result.get("news_result") or {}).get("status")),
                "warning_count": (result.get("news_result") or {}).get("warning_count"),
                "direction": (result.get("news_result") or {}).get("overall_direction"),
                "volatility_regime": ((result.get("news_result") or {}).get("expected_volatility_regime") or {}).get("regime"),
                "already_priced_in": ((result.get("news_result") or {}).get("already_priced_in") or {}).get("state"),
                "independent_of_quant": True,
            },
        },
        "contract": {key: lines[key] for key in ("production_path", "validation", "relative_skill", "news", "directional_edge")},
    }


def enforce_agent_summary_budget(summary: dict[str, Any], budget: int = AGENT_SUMMARY_BUDGET) -> dict[str, Any]:
    encoded = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    if len(encoded.encode("utf-8")) <= budget:
        return summary
    trimmed = dict(summary)
    trimmed["truncated"] = True
    trimmed["sections"] = dict(summary.get("sections") or {})
    trimmed["sections"]["shadow_predictions"] = (summary.get("sections", {}).get("shadow_predictions") or [])[:3]
    trimmed["sections"]["diagnostics"] = {}
    encoded = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    while len(encoded.encode("utf-8")) > budget and trimmed["contract"]:
        trimmed["contract"].pop(next(iter(trimmed["contract"])))
        encoded = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    return trimmed


def render_final_response(result: dict[str, Any], summary: dict[str, Any] | None = None) -> str:
    del summary  # kept for API compatibility; the fixed block is rendered from the frozen result
    lines = required_response_lines(result)
    consensus = result.get("consensus") or {}
    evidence = _evidence(result)
    snapshot = result.get("snapshot") or {}
    blocks = [
        lines["research_window"],
        lines["forecast_target"],
        lines["probability"],
        lines["model_lean"],
        lines["directional_edge"],
        lines["production_path"],
        lines["validation"],
        lines["relative_skill"],
        lines["news"],
        "",
        "证据边界：",
        "- 概率语义：漂移收缩的因果基率估计；未校准，不可解释为真实涨跌频率",
        f"- 资格状态：computable={consensus.get('computable')}；current_integrity={consensus.get('current_prediction_integrity_pass')}；"
        f"oos_integrity={consensus.get('oos_integrity_pass')}；production_eligible={consensus.get('production_probability_eligible')}；"
        f"directional_edge={consensus.get('directional_edge_supported')}；trading_signal=false",
        f"- 优势证据状态：{evidence.get('directional_edge_state') or 'insufficient_evidence'}；"
        f"主要证据=全量前缀回放（{evidence.get('replay_valid') or 0}条，有效样本≈{evidence.get('oos_effective_samples') or 0}）；"
        f"前向账本为佐证（{evidence.get('prospective_nonoverlap') or 0}条）",
        f"- 回放类型：{evidence.get('replay_type')}；前缀回放成熟{evidence.get('replay_valid') or 0}条，其中非重叠{evidence.get('replay_nonoverlap') or 0}条；"
        f"真实前向{evidence.get('prospective_nonoverlap') or 0}条",
        f"- 校准状态：disabled_by_policy；影子路径：{', '.join(row.get('path', '') for row in consensus.get('shadow_predictions') or []) or '无'}",
        f"- 标签口径：拆股调整=True；分红调整={dividend_label_disclosure()['dividend_adjusted']}；"
        f"股息翻转率未量化（快照无分红历史）",
        f"- 标的：{snapshot.get('ticker')} / {snapshot.get('market')} / {snapshot.get('horizon')}；快照 {snapshot.get('snapshot_id')}",
    ]
    if consensus.get("reason_codes"):
        blocks.append(f"- 原因码：{', '.join(consensus['reason_codes'])}")
    if result.get("adversarial_audit", {}).get("vetoes"):
        blocks.append("- 审计否决：" + "；".join(result["adversarial_audit"]["vetoes"]))
    return "\n".join(blocks)


def _news_layers(news: dict[str, Any]) -> list[str]:
    events = [row for row in news.get("major_events") or [] if isinstance(row, dict) and row.get("eligible_evidence")]
    recent = [row for row in events if isinstance(row.get("age_sessions"), int) and row["age_sessions"] <= 5]
    background = [row for row in events if row not in recent]
    upcoming = [row for row in events if row.get("scheduled_at") and str(row.get("scheduled_at")) > str(news.get("asof") or "")]
    lines = []
    # Headlines are provider text: they are escaped before they reach Markdown so a
    # headline can never inject a heading, list item or link into report.md.
    lines.append("过去5个交易日新增信息：" + ("；".join(
        f"{markdown_text(row.get('headline'), 200)}（{row.get('direction')}）" for row in recent[:5]
    ) or "未发现符合证据门槛的新增事件"))
    lines.append("较早背景：" + ("；".join(
        f"{markdown_text(row.get('headline'), 200)}（age={row.get('age_sessions')} sessions）" for row in background[:5]
    ) or "无"))
    lines.append("未来5个交易日已公布日程：" + ("；".join(
        f"{row.get('scheduled_at')} {markdown_text(row.get('headline'), 200)}" for row in upcoming[:5]
    ) or "not_assessed（未取得可核实的未来事件日程）"))
    return lines


def render_markdown(result: dict[str, Any]) -> str:
    lines = required_response_lines(result)
    consensus = result.get("consensus") or {}
    evidence = _evidence(result)
    snapshot = result.get("snapshot") or {}
    production = _production(result)
    out = ["# Stock Data 研究报告", ""]
    out.extend(f"- {value}" for value in lines.values())
    out.extend(["", "## 数据与目标", ""])
    out.append(f"- 决策日：{_window(result).get('decision_session')}")
    out.append(f"- 窗口：({_window(result).get('window_start_exclusive')}, {_window(result).get('window_end_inclusive')}]")
    out.append(f"- 日历验证：{_window(result).get('calendar_verified')}；来源 {_window(result).get('calendar_source') or 'unverified'}；"
               f"预期交易日 {_window(result).get('expected_sessions')}；观测 {_window(result).get('observed_sessions')}；"
               f"缺失 {_window(result).get('missing_sessions')}")
    out.append(f"- 价格基础：{_window(result).get('price_basis')}（拆股调整，现金分红不再投资）")
    out.extend(["", "## 概率与证据", ""])
    out.append(f"- 模型倾向：{LEAN_LABELS.get(str(consensus.get('model_lean')), '不可用')}")
    out.append(f"- 当前匹配样本：{production.get('matching_observations')}；因果基率：{_num(production.get('causal_base_rate'))}")
    out.append(f"- 前缀回放：类型 {evidence.get('replay_type')}；有效 {evidence.get('replay_valid')}；非重叠 {evidence.get('replay_nonoverlap')}；弃权 {evidence.get('replay_abstained')}")
    momentum = (((result.get("researchers") or {}).get("quant") or {}).get("validation") or {}).get("momentum_20d") or {}
    if momentum:
        out.append(
            f"- 动量诊断：Score20={_num(momentum.get('momentum_score_20d'), 2)}；"
            f"方向={momentum.get('momentum_direction') or 'unavailable'}；"
            f"return_20d={_pct(momentum.get('return_20d'), 2)}；"
            "仅描述当期价格路径，不进入概率，也不与概率作一致性判定"
        )
    out.append(f"- 有效回放样本：{evidence.get('oos_effective_samples')}（=有效条数/5）；对照基线：因果基率与常数0.5")
    out.append(f"- 抽样相位：{evidence.get('oos_sampling_status')}（角色：{evidence.get('oos_sampling_role')}）")
    out.append(f"- 方向优势状态：{evidence.get('directional_edge_state')}；主要证据：{evidence.get('directional_edge_primary_evidence')}")
    out.append(f"- 真实前向：非重叠 {evidence.get('prospective_nonoverlap')}；校准状态 disabled_by_policy")
    edge_state = str(evidence.get("directional_edge_state") or "insufficient_evidence")
    edge_text = {
        "positive_skill_with_estimated_interval_excluding_zero": "已支持（区间下界>0）",
        "negative_skill_with_estimated_interval_excluding_zero": "无（区间上界<0，劣于纯基率）",
    }.get(edge_state, "证据不足（区间含0）")
    out.append(f"- 方向优势：{edge_text}")
    if consensus.get("shadow_predictions"):
        out.extend(["", "## 影子与诊断（不进入生产概率）", ""])
        out.extend([
            f"- {row.get('path')}：{_pct(row.get('probability'))}；状态 {row.get('status')}"
            for row in consensus["shadow_predictions"]
        ])
        diagnostic = consensus.get("diagnostics") or {}
        if diagnostic:
            out.append(f"- D 描述性诊断（5D）：IC={_num(diagnostic.get('information_coefficient'))}；留出样本={diagnostic.get('holdout_sample_count')}；descriptive_only=true")
    news = result.get("news_result") or {}
    out.extend(["", "## 新闻（独立于 Quant）", ""])
    out.append(f"- 状态：{news.get('status') or 'not_assessed'}（{news_status_label(news.get('status'))}）；"
               f"已评估：{news_assessed(news.get('status'))}；警告：{news.get('warning_count') if news.get('warning_count') is not None else len(news.get('warnings') or [])} 条")
    out.append(f"- 事件方向：{news.get('overall_direction') or 'unknown'}；"
               f"预期波动状态：{(news.get('expected_volatility_regime') or {}).get('regime') or 'unknown'}（不含方向）")
    out.append(f"- 是否已被价格反映：{(news.get('already_priced_in') or {}).get('state') or 'not_assessable'}（仅展示，不进入概率）")
    out.append(f"- 发布时段分布：{news.get('event_time_bucket_counts') or {}}")
    out.append(f"- 来源一致度：观点 {(news.get('opinion_source_agreement') or {}).get('state')}；"
               f"事件 {(news.get('event_source_agreement') or {}).get('state')}")
    out.extend([f"- {line}" for line in _news_layers(news)])
    out.append("- 新闻证据不接收 Quant 概率，也不影响 Quant 资格或分数。")
    audit = result.get("adversarial_audit") or {}
    out.extend(["", "## 审计与限制", ""])
    out.append(f"- 审计状态：{audit.get('status')}；may_report_probability={audit.get('may_report_probability')}")
    out.append(f"- 否决：{'；'.join(audit.get('vetoes') or []) or '无'}")
    out.append(f"- 警告：{'；'.join(audit.get('warnings') or []) or '无'}")
    out.append("- 收益为拆股调整收盘价的 5 交易日方向，不含分红再投资；未建模执行、价差、滑点与费用。")
    label_basis = dividend_label_disclosure()
    out.append(f"- 标签口径：拆股调整={label_basis['price_basis']}；分红调整={label_basis['dividend_adjusted']}；"
               f"股息翻转率={label_basis['flip_rate']}（{label_basis['status']}）")
    out.append(f"- 快照：{snapshot.get('snapshot_id')}；政策：{_window(result).get('policy_version')}")
    return "\n".join(out)


def _momentum_relationship_line(result: dict[str, Any]) -> str:
    """Retired helper kept only so old callers fail loudly instead of silently.

    v1 promised a fixed "momentum vs probability" verdict line but never rendered
    one, and v2 removed ROC20 from the probability entirely.  Comparing the two
    now would present a non-input as evidence, so the line is gone.
    """
    raise NotImplementedError(
        "the momentum-vs-probability verdict was retired in v2; report the diagnostic only"
    )
