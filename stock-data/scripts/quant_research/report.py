"""Eight-section reports and a bounded DATA-only projection for the host Agent."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

from .contracts import MISSING_PROBABILITY_LABEL
from .security import bounded_text, markdown_text, safe_url
from .synthesis import reportable_quant_fields


def _pct(value: Any) -> str:
    return f"{float(value):.1%}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "Unavailable"


def _cell(value: Any) -> str:
    return markdown_text(value if value not in (None, "") else "Unavailable")


def _probability_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        return "Unavailable"
    text = _pct(value)
    if 0 < value < 1 and text in {"0.0%", "100.0%"}:
        # Display rounding must not turn a nonzero/noncertain estimate into 0/100%.
        return format(Decimal(str(value)) * 100, "f").rstrip("0").rstrip(".") + "%"
    return text


LABELS = {
    "bullish": "bullish（偏多）", "bearish": "bearish（偏空）", "neutral": "neutral（中性）", "unavailable": "unavailable（不可用）",
    "high": "high（高）", "medium": "medium（中）", "low": "low（低）", "very_low": "very_low（很低）",
    "high_disagreement": "high_disagreement（高度分歧）", "extreme_disagreement": "extreme_disagreement（极度分歧）",
    "pass": "pass（PASS/通过）", "degraded": "degraded（DEGRADED/降级）", "veto": "veto（VETO/否决）",
}
PREDICTIVE_EVIDENCE_LABELS = {
    "insufficient_oos_evidence": "样本外证据不足（insufficient_oos_evidence）",
    "no_observed_skill_vs_expanding_base_rate": "未观察到优于因果扩展基准的技能（no_observed_skill_vs_expanding_base_rate）",
    "exploratory_positive_skill_unconfirmed": "正向技能仅为探索性点估计，尚未确认（exploratory_positive_skill_unconfirmed）",
    "positive_skill_point_estimate_uncertainty_unestimated": "正向技能点估计；不确定性尚未估计（positive_skill_point_estimate_uncertainty_unestimated）",
    "positive_skill_with_estimated_interval_excluding_zero": "正向技能；已估计不确定性且区间不含零（positive_skill_with_estimated_interval_excluding_zero）",
    "positive_skill_with_estimated_interval_including_zero": "正向技能；已估计不确定性但区间含零（positive_skill_with_estimated_interval_including_zero）",
    "observed_skill_below_expanding_base_rate": "观测技能低于基准（区间整体低于零）（observed_skill_below_expanding_base_rate）",
}
CONFIDENCE_REASON_LABELS = {
    "only_one_quant_path": "仅一个 Quant 路径提供概率",
    "uncalibrated_raw_probability": "概率未经充分校准",
    "oos_validation_insufficient": "样本外验证不足",
    "some_oos_validation_weak": "部分路径的样本外验证偏弱",
    "leakage_audit_incomplete": "泄漏审计证据不完整",
    "moderate_path_disagreement": "路径间存在一定分歧",
    "high_path_disagreement": "路径间分歧较大",
    "extreme_path_disagreement": "路径间极度分歧",
    "low_evidence_family_diversity": "证据来源多样性较低",
    "limited_history_depth": "可用日线历史偏短",
    "ensemble_calibration_unavailable": "等权共识没有通过时间序后段改进检查的 ensemble 校准器",
    "oos_evidence_invalid": "部分 OOS 记录未通过冻结行情核验",
    "policy_oos_skill_evidence_insufficient": "整套历史预测策略的 OOS 样本不足",
    "policy_oos_skill_below_expanding_base_rate_interval": "整套历史预测策略的观测技能区间整体低于因果扩展基准",
    "policy_oos_not_better_than_expanding_base_rate": "整套历史预测策略未优于因果扩展基准",
    "policy_oos_skill_is_exploratory": "整套策略的正向 OOS 技能非重叠样本低于完整支持（探索性）",
    "policy_oos_skill_uncertainty_unestimated": "整套策略的正向 OOS 技能尚未估计不确定性",
    "policy_oos_skill_uncertainty_includes_zero": "整套策略的正向 OOS 技能区间含零，尚未与基准区分",
}


def _label(value: Any) -> str:
    return LABELS.get(str(value), _cell(value))


def _confidence_reason_text(value: Any) -> str:
    return CONFIDENCE_REASON_LABELS.get(str(value), _cell(value))


def _predictive_evidence_text(value: Any) -> str:
    return PREDICTIVE_EVIDENCE_LABELS.get(str(value), _cell(value))


def _quant_lines(fields: dict[str, Any]) -> list[str]:
    """Presentation only: retain native Quant categories, never invent percent scores."""
    lines = [f"- 上涨概率 P(up)：{_probability_text(fields['prob_up'])}" if fields["prob_up"] is not None else f"- {MISSING_PROBABILITY_LABEL}",
             f"- 方向：{_label(fields['direction'])}",
             f"- Quant Confidence：{_label(fields['confidence'])}",
             f"- 置信度支持度评分：{_cell(fields.get('confidence_score'))}/100",
             f"- 置信度评分含义（confidence_score_kind）：{_cell(fields.get('confidence_score_kind'))}；启发式研究支持度，不代表预测准确率。",
             f"- OOS 预测证据等级（predictive_evidence_grade）：{_predictive_evidence_text(fields.get('predictive_evidence_grade'))}",
             f"- 报告状态：{_label(fields.get('reporting_status'))}",
             f"- 模型一致度（agreement）：{_label(fields['agreement'])}"]
    if fields.get("raw_probability") is not None:
        lines.append(f"- 原始路径估计均值：{_probability_text(fields['raw_probability'])}")
    if fields.get("calibrated_probability") is not None:
        lines.append(f"- 校准概率：{_probability_text(fields['calibrated_probability'])}")
        calibration_line = "- 联合概率校准：可用（按通过时间序后段检查的 ensemble calibration；回顾性检查，非独立确认）"
    else:
        calibration_line = "- 联合概率校准：不可用（等权路径均值未通过 ensemble calibration 的时间序后段改进检查）"
    lines.append(calibration_line)
    if fields.get("confidence_reasons"):
        reasons = ", ".join(_confidence_reason_text(item) for item in fields["confidence_reasons"][:4])
        lines.append("- 低置信度原因：" + reasons)
    if "diversity" in fields:
        lines.append(f"- 证据多样性（diversity）：{_label(fields['diversity'])}")
    if fields.get("horizon_type") == "estimated_observed_sessions":
        lines.append("- 预测窗口：基于 OHLCV 观测行估算交易 session；交易日历未完整验证")
    return lines


_MOMENTUM_SIGN_CN = {"positive": "正", "negative": "负"}
_MOMENTUM_UNAVAILABLE = "- 20日综合动量特征：Unavailable（本次运行未计算）；不是上涨概率"


def _momentum_bucket(result: dict[str, Any], prob_up: float) -> tuple[float, int, list[float]] | None:
    """Return the momentum row that produced the reported probability, when present."""
    evidence = ((result.get("researchers") or {}).get("quant") or {}).get("evidence")
    side = "positive" if prob_up >= 0.5 else "negative"
    rows = (evidence or {}).get(side) if isinstance(evidence, dict) else None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("family") != "momentum":
            continue
        hit = row.get("empirical_hit_rate")
        samples = row.get("non_overlapping_samples")
        interval = row.get("wilson_95_interval")
        if isinstance(hit, (int, float)) and not isinstance(hit, bool) and isinstance(samples, int):
            bounds = [float(value) for value in interval or []
                      if isinstance(value, (int, float)) and not isinstance(value, bool)]
            return float(hit), samples, bounds
    return None


def _momentum_relationship_line(result: dict[str, Any]) -> str:
    """Bind the momentum-vs-probability verdict to one literal, contract-bound line.

    The momentum score describes the current price path; P(up) is the historical hit
    rate of same-sign samples. A negative score beside a bullish probability is not a
    contradiction, so the reply has to name the case instead of leaving the reader to
    infer one. The line is part of ``required_response_lines`` so a host cannot reword
    or omit it, and nothing here feeds back into the probability.
    """
    quant = (result.get("researchers") or {}).get("quant") or {}
    block = (quant.get("validation") or {}).get("momentum_20d")
    prob = reportable_quant_fields(result).get("prob_up")
    if not isinstance(block, dict):
        return ("- 动量与上涨概率的关系：不可比。本次运行未计算 20 日动量特征，因此不对二者作判断，"
                "也不把它当作中性证据。")
    status = str(block.get("score_status"))
    direction = str(block.get("momentum_direction"))
    score = block.get("momentum_score_20d")
    score_text = f"{float(score):+.2f}" if isinstance(score, (int, float)) and not isinstance(score, bool) else "Unavailable"
    return_text = _pct(block.get("return_20d"))
    if status != "valid" or prob is None or direction not in {"positive", "negative", "flat"}:
        reasons = block.get("status_reasons") or []
        reason = _cell(reasons[0]) if reasons else f"状态 {_cell(status)}"
        return (f"- 动量与上涨概率的关系：不可比。动量特征不可用（{reason}），因此不对二者作判断，"
                "也不做默认替代。")
    if direction == "flat":
        return (f"- 动量与上涨概率的关系：动量持平。20 日累计收益 {return_text}、综合动量 {score_text} 接近零，"
                f"不构成方向信号；上涨概率 {_pct(prob)} 仍只由自身条件历史给出，不因动量持平而调整，二者不构成矛盾。")
    bucket = _momentum_bucket(result, float(prob))
    sign_cn = _MOMENTUM_SIGN_CN.get(direction, direction)
    stats = ""
    if bucket:
        hit, samples, bounds = bucket
        stats = f" = {_pct(hit)}（非重叠 {samples} 个样本"
        if len(bounds) == 2:
            stats += f"；Wilson 95% 区间 {_pct(bounds[0])}–{_pct(bounds[1])}"
            if bounds[0] < 0.5 < bounds[1]:
                stats += "，跨越 50%，该条件证据本身不稳健"
        stats += "）"
    aligned = (direction == "positive") == (float(prob) >= 0.5)
    if aligned:
        return (f"- 动量与上涨概率的关系：一致（同向）。动量方向（{sign_cn}）与概率方向相同，但两者仍不是同一件事："
                f"{_pct(prob)} 是历史中与当前同号（{sign_cn}）动量样本的上涨占比{stats}，"
                f"综合动量 {score_text} 只是当期价格路径的描述。动量特征不参与该概率的计算，也不改变它。")
    return (f"- 动量与上涨概率的关系：背离（方向相反），但不是矛盾。{_pct(prob)} 不是动量数值的映射，"
            f"而是历史中与当前同号（{sign_cn}）动量的样本里上涨的占比{stats}。"
            f"因此这里同时成立的两件事是：当期 20 日累计收益 {return_text}（综合动量 {score_text}，方向{sign_cn}），"
            f"而该条件在样本内{'上涨' if float(prob) >= 0.5 else '下跌'}更常见。"
            "动量特征不参与该概率的计算，也不改变它。")


def _momentum_section(required: dict[str, str]) -> list[str]:
    """Render momentum as its own section, never as a line inside the Quant block."""
    lines = [required["momentum_feature"]]
    if "momentum_components" in required:
        lines.append(required["momentum_components"])
    return ["", "### 动量特征（独立诊断，不是上涨概率）", "", *lines, required["momentum_relationship"]]


def _momentum_lines(result: dict[str, Any]) -> list[str]:
    """Render the composite momentum feature without turning it into a probability."""
    researchers = result.get("researchers") or {}
    block = ((researchers.get("quant") or {}).get("validation") or {}).get("momentum_20d")
    if not isinstance(block, dict):
        return []
    score = block.get("momentum_score_20d")
    score_text = "Unavailable" if score is None else f"{float(score):+.2f}"
    status = _cell(block.get("score_status"))
    direction = {"positive": "positive（正）", "negative": "negative（负）", "flat": "flat（持平）"}.get(
        str(block.get("momentum_direction")), _cell(block.get("momentum_direction"))
    )
    return_20d = block.get("return_20d")
    # Underscores in the internal id would be backslash-escaped for Markdown and shown
    # literally as "momentum\_20d\_v1" in plain-text channels; the display form uses hyphens.
    model = str(block.get("model_version") or "momentum_20d_v1").replace("_", "-")
    lines = [
        f"- 20日综合动量特征（{_cell(model)}）：{score_text}"
        f"（状态 {status}；绝对方向 {direction}，20日累计收益 {_pct(return_20d)}）；特征分 −100~+100，不是上涨概率",
    ]
    components = block.get("components") or {}
    parts = []
    for key, label in (("risk_adjusted", "风险调整"), ("regression_trend", "回归趋势"), ("relative_strength", "相对强度")):
        contribution = (components.get(key) or {}).get("contribution")
        parts.append(f"{label} {'Unavailable' if contribution is None else f'{float(contribution):+.2f}'}")
    benchmark = block.get("benchmark") or {}
    lines.append(
        "- 动量分项：" + " / ".join(parts)
        + f"；权重 50/30/20，校准状态 {_cell(block.get('calibration_status'))}；"
        + f"基准 {_cell(benchmark.get('symbol'))}（{_cell(benchmark.get('selection'))}，对齐 {_cell(benchmark.get('aligned_sessions'))} 个 session）"
    )
    return lines


def _economic_lines(value: dict[str, Any] | None) -> list[str]:
    value = value if isinstance(value, dict) else {}
    if value.get("status") != "scenario_sensitivity_only":
        return ["- 交易经济性：未评估（预期收益或成本情景不足）"]
    scenarios = value.get("scenarios", [])
    costs = ", ".join(
        f"{_cell(row.get('round_trip_cost_bps'))}bp {_pct(row.get('net_expected_return'))}"
        for row in scenarios if isinstance(row, dict)
    ) or "Unavailable"
    return [
        "- 交易经济性：仅为假设成本敏感性分析；不代表实际费率、滑点、市场冲击或策略净收益回测。",
        f"- 毛预期收益：{_pct(value.get('gross_expected_return'))}；成本后的情景净预期收益：{costs}。",
        "- 交易经济性不改变上涨概率、原始预测或 Quant 置信度。",
    ]


def _probability_validation_lines(consensus: dict[str, Any]) -> list[str]:
    quality = consensus.get("oos_probability_quality") or {}
    common = quality.get("common_oos") or {}
    path_metrics = quality.get("paths") or {}
    common_metrics = common.get("path_metrics_on_common_oos_dates") or {}
    complete_metrics = common.get("path_metrics_on_same_complete_samples") or {}
    candidate_common = quality.get("candidate_path_common_oos") or {}
    row_validation = quality.get("row_validation") or {}
    replay = quality.get("algorithm_policy_replay") or {}
    weighting = consensus.get("skill_weight_validation") or {}
    lines = ["", "### OOS Probability Quality"]
    lines.append(f"- Method: {_cell(quality.get('method'))}; status: {_cell(quality.get('status'))}.")
    active_names = quality.get("current_forecast_paths") or []
    lines.append("- Current eligible forecast paths: " + (", ".join(_cell(name) for name in active_names) or "none") + "; the common-path metrics below use this set.")
    for name, audit in row_validation.items():
        reasons = audit.get("rejection_reasons") or {}
        reason_text = ", ".join(f"{_cell(code)}={_cell(count)}" for code, count in sorted(reasons.items())) or "none"
        lines.append(f"- OOS evidence audit {_cell(name)}: {_cell(audit.get('status'))}; supplied {_cell(audit.get('supplied_samples'))}, accepted {_cell(audit.get('valid_samples'))}, rejected {_cell(audit.get('rejected_samples'))}; reasons: {reason_text}.")
    lines.extend(["| Path | Own OOS N | Valid predictions | Coverage | Abstain | Brier | Log Loss | ECE |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for name, metrics in path_metrics.items():
        lines.append(f"| {_cell(name)} | {_cell(metrics.get('all_evaluable_oos_samples'))} | {_cell(metrics.get('valid_probability_predictions'))} | {_pct(metrics.get('oos_coverage'))} | {_pct(metrics.get('abstain_rate'))} | {_cell(metrics.get('brier_score'))} | {_cell(metrics.get('log_loss'))} | {_cell(metrics.get('expected_calibration_error'))} |")
    lines.append(f"- Current eligible-path OOS date intersection: N={_cell(common.get('common_oos_samples'))}; {_cell(common.get('common_oos_start'))} to {_cell(common.get('common_oos_end'))}; every available target label is included, while model-specific abstentions remain visible.")
    lines.extend(["| Path | Valid common predictions | Coverage | Brier | Log Loss | ECE |",
                  "|---|---:|---:|---:|---:|---:|"])
    for name, metrics in common_metrics.items():
        lines.append(f"| {_cell(name)} | {_cell(metrics.get('valid_probability_predictions'))}/{_cell(metrics.get('all_evaluable_oos_samples'))} | {_pct(metrics.get('oos_coverage'))} | {_cell(metrics.get('brier_score'))} | {_cell(metrics.get('log_loss'))} | {_cell(metrics.get('expected_calibration_error'))} |")
    lines.append(f"- Same-sample comparison across paths: N={_cell(common.get('all_paths_valid_prediction_samples'))}; metrics below use only dates where every path predicted.")
    for name, metrics in complete_metrics.items():
        lines.append(f"- {_cell(name)} same-sample Brier {_cell(metrics.get('brier_score'))}; Log Loss {_cell(metrics.get('log_loss'))}; ECE {_cell(metrics.get('expected_calibration_error'))}.")
    lines.append(f"- All-path candidate intersection (including currently ineligible paths): N={_cell(candidate_common.get('common_oos_samples'))}; complete predictions={_cell(candidate_common.get('all_paths_valid_prediction_samples'))}. This is diagnostic and does not change current path eligibility.")
    lines.extend(["- Reliability bins use 10 equal-width probability intervals. Counts and observed rates are reported below; ECE is auxiliary and no calibration confidence interval is claimed.",
                  "| Path | Probability interval | N | Mean predicted | Observed positive rate |",
                  "|---|---|---:|---:|---:|"])
    for name, metrics in common_metrics.items():
        lines.extend(f"| {_cell(name)} | {_cell(bucket.get('lower_bound'))}–{_cell(bucket.get('upper_bound'))} | {_cell(bucket.get('count'))} | {_cell(bucket.get('mean_probability'))} | {_cell(bucket.get('observed_positive_rate'))} |" for bucket in metrics.get("reliability_diagram", []))
    replay_forecast = replay.get("forecast_metrics") or {}
    replay_baseline = replay.get("expanding_base_rate_on_same_dates") or {}
    path_counts = replay.get("path_count_by_prediction_date") or {}
    lines.extend(["", "### Historical Algorithm Policy Replay"])
    lines.append(f"- Status: {_cell(replay.get('status'))}; method: {_cell(replay.get('method'))}.")
    if replay.get("evidence_scope"):
        lines.append(f"- Evidence scope: {_cell(replay.get('evidence_scope'))}; coverage denominator: {_cell(replay.get('coverage_denominator_scope'))}.")
    lines.append(f"- Predicted dates: {_cell(replay.get('predicted_dates'))}/{_cell(replay.get('eligible_oos_label_dates'))}; coverage: {_pct(replay.get('oos_coverage'))}; abstain rate: {_pct(replay.get('abstain_rate'))}.")
    lines.append("- Dates with 1/2/3 active forecast paths: " + "/".join(_cell(path_counts.get(str(count), 0)) for count in (1, 2, 3)) + "; the per-date active-path mask is retained in Quant JSON.")
    lines.append(f"- Same-date Brier: policy {_cell(replay_forecast.get('brier_score'))}; causal expanding base rate {_cell(replay_baseline.get('brier_score'))}; Brier Skill {_cell(replay.get('brier_skill_vs_expanding_base_rate'))}; policy Log Loss {_cell(replay_forecast.get('log_loss'))}; baseline Log Loss {_cell(replay_baseline.get('log_loss'))}.")
    lines.append(f"- Uncertainty: {_cell(replay.get('skill_uncertainty', 'not_estimated'))}. Positive point skill is not evidence of a statistically confirmed improvement.")
    if replay.get("reason"):
        lines.append(f"- Replay limitation: {_cell(replay.get('reason'))}.")
    lines.extend(["", "### Brier Skill Weight Validation"])
    lines.append(f"- Status: {_cell(weighting.get('status'))}; current weighting: {_cell(weighting.get('weighting'))}; skill weighting enabled: {_cell(weighting.get('current_weighting_enabled'))}; deployment: {_cell(weighting.get('deployment_status'))}.")
    lines.append(f"- Common OOS samples: {_cell(weighting.get('common_oos_samples'))}; weight estimation: {_cell(weighting.get('weight_estimation_start'))} to {_cell(weighting.get('weight_estimation_end'))}; evaluation: {_cell(weighting.get('evaluation_start'))} to {_cell(weighting.get('evaluation_end'))}; fallback: {_cell(weighting.get('fallback_reason'))}.")
    if weighting.get("candidate_weights"):
        lines.append("- Experimental candidate weights estimated only on the earlier window: " + ", ".join(f"{_cell(name)} {_cell(value)}" for name, value in weighting["candidate_weights"].items()) + ".")
    evaluation = weighting.get("evaluation") or {}
    lines.append(f"- Later-window Brier: experimental weighted {_cell(evaluation.get('weighted_brier'))}; default equal-weight {_cell(evaluation.get('equal_weight_brier'))}; estimation-window base-rate {_cell(evaluation.get('baseline_brier'))}. Experimental weights do not change the current equal-weight forecast.")
    return lines


def _final_probability_note(fields: dict[str, Any], ticker: str, horizon: str) -> str:
    if fields["prob_up"] is None:
        return f"{MISSING_PROBABILITY_LABEL}。Quant 未提供通过报告资格的有效概率；不得由 LLM 或新闻补算。"
    probability = _probability_text(fields["prob_up"])
    if fields.get("calibrated_probability") is not None:
        return (f"Quant 当前对 {_cell(ticker)} 的 {_cell(horizon)} 窗口给出 {probability} 的报告概率（等权均值经时间序后段检查的 ensemble calibration；回顾性检查，非独立确认）。"
                f"News 只补充驱动解释和风险证据，不改变这份 Quant 报告概率。若需 raw/calibrated 原值，见调试 JSON。")
    return (f"Quant 当前对 {_cell(ticker)} 的 {_cell(horizon)} 窗口给出 {probability} 的报告概率（合格路径等权均值，未单独进行 ensemble calibration）。"
            f"News 只补充驱动解释和风险证据，不改变这份 Quant 报告概率（等权路径均值）。若需 raw/calibrated 原值，见调试 JSON。")


def _ai_lean_line_text(news: dict[str, Any]) -> str:
    """One-line, source-traceable summary of the host AI headline lean.

    The lean is display evidence: this text never feeds a probability, a
    direction aggregate or a cross-validation label.
    """
    lean = news.get("ai_lean") or {}
    status = lean.get("status") or "not_assessed"
    if status == "not_assessed":
        return "未评估（本次未提供宿主 AI 逐条判断）"
    if status == "invalid":
        return f"不可用（宿主 AI 判断未通过校验并被排除：{_cell(lean.get('reason'))}）"
    parts: list[str] = []
    for label, key in (("利多", "bullish"), ("利空", "bearish"), ("混合", "mixed")):
        items = lean.get(key) or []
        if items:
            first = items[0]
            parts.append(f"{label}：{_cell(first.get('headline'))}（{_cell(first.get('reason'))}）")
    if not parts:
        parts.append("已逐条判断，未得出有依据的利多或利空线索")
    coverage = lean.get("coverage") or {}
    parts.append(
        f"共判断 {_cell(coverage.get('assessed_count'))}/{_cell(coverage.get('candidate_count'))} 条；"
        "标题级 AI 判断，仅作展示证据，不参与方向聚合、置信度或 Quant 概率"
    )
    return "；".join(parts)


def _relationship_text(synthesis: dict[str, Any]) -> str:
    return {"STRONG_ALIGNMENT": "一致（事件方向及日线关联同向）", "ALIGNMENT": "一致",
            "MIXED": "部分一致或存在冲突，保留混合证据", "DIVERGENCE": "冲突", "STRONG_DIVERGENCE": "冲突（存在反向日线关联）",
            "CATALYST_WITH_NEUTRAL_QUANT": "Quant 中性，消息存在催化剂",
            "OPINION_ONLY_CROSS_CHECK": "仅有观点交叉检查，尚无事件验证",
            "NOT_ASSESSED": "消息面交叉验证未评估", "ABSTAIN": "Quant 概率不可报告，方向交叉判断不可用"}.get(synthesis.get("alignment"), "未评估")


def _unavailable_reasons(result: dict[str, Any]) -> list[str]:
    gate = result["snapshot"].get("data_validation", {})
    reasons = list(gate.get("errors", [])) + list(gate.get("blockers", []))
    reasons += result["adversarial_audit"].get("vetoes", [])
    for item in result["researchers"].values():
        if item.get("result_role") != "diagnostic" and not item.get("forecast_eligible"):
            reasons += item.get("forecast_exclusion_reasons", []) + item.get("warnings", [])
    return list(dict.fromkeys(bounded_text(reason, 240) for reason in reasons if reason)) or ["Quant 未生成通过报告资格的有效概率。"]


def required_response_lines(result: dict[str, Any]) -> dict[str, str]:
    """Literal delivery fields generated from this result.

    Quant and identity fields are never taken from News values. The single
    News-derived literal is the headline-lean line, generated from the frozen
    News result so a host cannot summarize the message side freely or omit the
    per-item bullish/bearish basis.
    """
    snapshot, fields = result["snapshot"], reportable_quant_fields(result)
    required = {
        "identity": f"- 标的：{_cell(snapshot['ticker'])}；市场：{_cell(snapshot['market'])}；预测窗口：{_cell(snapshot['horizon'])}（交易 session）；分析截至：{_cell(snapshot['asof_timestamp'])}",
        "probability_note": _final_probability_note(fields, snapshot["ticker"], snapshot["horizon"]),
        "news_relationship": f"- 消息面与 Quant 的关系：{_relationship_text(result.get('synthesis', {}))}",
        "news_lean": f"- 消息面逐条依据：{bounded_text(_ai_lean_line_text(result.get('news_result', {})), 600)}",
        "reporting_status": f"- 报告状态：{_cell(fields.get('reporting_status', result.get('reporting_status', 'degraded')))}",
        "analysis_status": f"- 执行状态：{_cell(result['analysis_status'])}",
    }
    names = ["prob_up", "direction", "confidence", "confidence_score", "confidence_score_kind", "predictive_evidence_grade", "reporting_status", "agreement"]
    if fields.get("raw_probability") is not None:
        names.append("raw_probability")
    if fields.get("calibrated_probability") is not None:
        names.append("calibrated_probability")
    names.append("ensemble_calibration_status")
    if fields.get("confidence_reasons"):
        names.append("confidence_reasons")
    if "diversity" in fields:
        names.append("diversity")
    if fields.get("horizon_type") == "estimated_observed_sessions":
        names.append("horizon_type")
    required.update(zip(names, _quant_lines(fields), strict=True))
    if fields["prob_up"] is None:
        required["unavailable_reason"] = "- 概率不可用原因：" + _cell(_unavailable_reasons(result)[0])
    else:
        required["calibration"] = next(line for line in _quant_lines(fields) if line.startswith("- 联合概率校准："))
    momentum_lines = _momentum_lines(result) or [_MOMENTUM_UNAVAILABLE]
    required["momentum_feature"] = momentum_lines[0]
    if len(momentum_lines) > 1:
        required["momentum_components"] = momentum_lines[1]
    required["momentum_relationship"] = _momentum_relationship_line(result)
    return required


def render_final_response(result: dict[str, Any], *, summary: dict[str, Any] | None = None) -> str:
    """Default to a short human-facing summary; detailed evidence remains in the standard/debug artifacts."""
    required = required_response_lines(result)
    summary = summary if summary is not None else build_agent_summary(result)
    snapshot, sections = result["snapshot"], summary["sections"]
    price, news = sections["price_time"] or {}, sections["news"]
    lines = [f"# {_cell(snapshot['ticker'])} 简析", "", required["identity"],
             f"- 最新确认收盘：{_cell(price.get('price'))} {_cell(price.get('currency'))}，日期 {_cell(price.get('session_date'))}；来源 {_cell(price.get('source'))}（非实时价）",
             "", "### Quant", "", *_quant_lines(reportable_quant_fields(result)),
             *_economic_lines(sections.get("quant", {}).get("economic_viability"))]
    if "calibration" not in required:
        lines.append(required["unavailable_reason"])
    evidence = sections["technical_factor_evidence"][:3]
    lines.extend(f"- 核心依据：{_cell(row['factor'])}（{_cell(row['path'])} / {_cell(row['polarity'])}）" for row in evidence)
    if not evidence:
        lines.append("- 核心依据：没有可报告的技术/因子证据。")

    reason_codes = set((snapshot.get("data_validation") or {}).get("reason_codes", []))
    limit_labels = {
        "CALENDAR_FALLBACK_USED": "交易日历采用 OHLCV 观测日期回退，节假日/缺失交易日未独立验证",
        "ADJUSTMENT_UNKNOWN": "OHLCV 复权口径未知；短期方向可计算，但置信度下调",
        "ADJUSTMENT_EVIDENCE_UNAVAILABLE": "公司行动证据不足，不能确认近期无污染性公司行动",
        "ADJUSTMENT_EVIDENCE_CONFLICT": "复权口径与公司行动证据不完全一致",
        "CORPORATE_ACTION_UNKNOWN": "近期公司行动无法独立确认",
        "LATEST_CLOSE_SINGLE_PROVIDER": "最新收盘仅单一来源，未完成跨源核对",
        "EXTREME_PRICE_MOVE_UNRECONCILED": "存在尚未解释的极端价格变动",
    }
    limitations = [label for code, label in limit_labels.items() if code in reason_codes]
    limitations.extend(_confidence_reason_text(item) for item in result.get("consensus", {}).get("confidence_reasons", [])[:2])
    for penalty in snapshot.get("data_quality", {}).get("confidence_penalties", []):
        raw_reason = penalty.get("reason_code", penalty.get("reason"))
        if raw_reason == "fatal_prediction_integrity":
            continue
        if raw_reason in limit_labels:
            limitations.append(limit_labels[raw_reason])
        elif raw_reason:
            limitations.append(_confidence_reason_text(raw_reason))
    if limitations:
        lines.extend(["", "**限制**"])
        lines.extend("- " + _cell(item) for item in list(dict.fromkeys(limitations))[:4])

    lines.extend(_momentum_section(required))
    lines.extend(["", "### News / Sentiment", "",
                  f"- 消息评估：{_cell(news.get('status'))}；事件方向：{_cell(news.get('event_bias'))}"])
    lines.extend(f"- {_cell(event.get('first_published_at'))}：{_link(event.get('headline'), event.get('url'))}；方向 {_cell(event.get('direction'))}" for event in news.get("events", [])[:2])
    if not news.get("events"):
        lines.append("- 无合格事件证据；不把消息缺失视为中性或已验证。")
    lines.append(required["news_lean"])
    lines.extend(["", "### 综合", "", required["probability_note"], required["news_relationship"], required["analysis_status"]])
    risks = sections["risk_counter_evidence"]["items"]
    lines.extend(f"- 风险：{_cell(row['detail'])}" for row in risks[:2])
    lines.append("- 历史验证不保证未来表现；这不是已验证的可盈利交易策略。")
    return "\n".join(lines)


def _link(title: Any, url: Any) -> str:
    url = safe_url(url)
    return f"[{_cell(title)}](<{url.replace('>', '%3E').replace('<', '%3C')}>)" if url else _cell(title)


def _validation_summary(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("validation") or {}
    holdout = value.get("final_holdout") or {}
    return {"oos_predictions": value.get("oos_predictions", value.get("holdout_predictions", holdout.get("observations"))),
            "brier": value.get("brier", value.get("holdout_brier", holdout.get("brier"))),
            "base_rate_brier": value.get("base_rate_brier", value.get("holdout_base_rate_brier", holdout.get("base_rate_brier"))),
            "calibration": item.get("probability_calibration", "not_assessed"),
            "sample_feasibility": value.get("sample_feasibility")}


def impact_summary(event: dict[str, Any]) -> dict[str, Any]:
    """Compact projection of the News impact grading for the bounded summary.

    The impact block is graded inside the frozen News stage, so it must also be
    projected into report.md: a JSON-cleanup run keeps only report.md and
    final_response.md, and would otherwise discard the grading. agent_summary.json
    is deleted by that same cleanup, so it keeps only short per-event fields plus
    the constant rubric marker, keeping the bounded-size budget intact.
    """
    impact = event.get("impact")
    if not isinstance(impact, dict):
        # A pre-2.9.0 frozen news result has no impact block; nulls would be
        # indistinguishable from errors, so project the documented state.
        return {"magnitude_band": "not_assessed", "pattern": "not_assessed",
                "source_tier": "not_assessed", "declared_breadth_scope": "not_assessed",
                "observed_breadth": "not_assessed", "composite_impact_score": "not_assessed"}
    magnitude = impact.get("magnitude") or {}
    pattern = impact.get("pattern_comparison") or {}
    breadth = impact.get("breadth_scope") or {}
    tier = impact.get("source_tier") or {}
    return {
        "magnitude_band": magnitude.get("band"),
        "pattern": pattern.get("pattern"),
        "source_tier": tier.get("tier"),
        "declared_breadth_scope": breadth.get("declared_scope"),
        "observed_breadth": breadth.get("observed_breadth"),
        "composite_impact_score": impact.get("composite_impact_score"),
    }


# Full caveats are rendered in report.md. This marker keeps the bounded summary
# honest about scope without repeating a paragraph per event card.
IMPACT_RUBRIC_MARKER = ("heuristic_uncalibrated; breadth is declared not observed; "
                        "source tier is exact-alias weakest-of-cluster; no composite impact score; "
                        "see references/news-impact-rubric.md")

IMPACT_RUBRIC_LIMITS: tuple[str, ...] = (
    "Magnitude bands are single-instrument close-to-close thresholds, not index, sector or intraday moves.",
    "Magnitude and pattern bands are withheld when the reaction window includes pre-publication trading or the reaction direction is not classifiable; the observed raw return may still be shown.",
    "Pattern compares the observed move to the symbol's own trailing return sigma over up to the last 20 returns (minimum 5, none of them after the event session); the actual return count is reported as trailing_sigma_observations and it is not a cross-asset historical pattern test.",
    "Breadth scope is DECLARED from the event taxonomy, never observed; the layer reads one frozen ticker series only.",
    "Source tier uses exact publisher aliases and takes the weakest publisher in a cluster; an unrecognized publisher is never promoted.",
    "No composite impact score is produced. Daily-bar association is temporal and does not establish causation.",
)


def impact_rubric_limits() -> list[str]:
    """Constant impact-grading caveats, emitted once rather than per event card.

    Includes the full attribution-caveat block so the retained report.md keeps
    the interpretation caveats after cleanup deletes news_result.json.
    """
    from .news.impact import ATTRIBUTION_CAVEATS
    return list(IMPACT_RUBRIC_LIMITS) + list(ATTRIBUTION_CAVEATS)


AGENT_SUMMARY_BYTE_BUDGET = 30_000


def _summary_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def enforce_agent_summary_budget(summary: dict[str, Any]) -> dict[str, Any]:
    """Keep agent_summary.json inside its documented byte budget.

    The summary is bounded by construction; this guard only fires if future
    fields grow, and it drops or truncates optional detail — never the identity,
    probability, status or warning fields the delivery contract requires. The
    trim order is fixed and each removal is recorded under ``size_budget``.
    """
    if _summary_bytes(summary) <= AGENT_SUMMARY_BYTE_BUDGET:
        return summary
    sections = summary.get("sections", {})
    trimmed: list[str] = []

    def over() -> bool:
        return _summary_bytes(summary) > AGENT_SUMMARY_BYTE_BUDGET

    if over() and sections.get("technical_factor_evidence"):
        sections["technical_factor_evidence"] = []
        trimmed.append("technical_factor_evidence")
    backtest = sections.get("backtest_ic_bias") or {}
    if over() and backtest.get("diagnostic_examples"):
        backtest["diagnostic_examples"] = []
        trimmed.append("backtest_ic_bias.diagnostic_examples")
    news = sections.get("news") or {}
    if over() and len(news.get("events") or []) > 3:
        news["events"] = news["events"][:3]
        trimmed.append("news.events_reduced_to_3")
    risks = sections.get("risk_counter_evidence") or {}
    if over() and len(risks.get("items") or []) > 6:
        risks["items"] = risks["items"][:6]
        if isinstance(risks.get("total"), int):
            risks["omitted"] = max(0, risks["total"] - len(risks["items"]))
        trimmed.append("risk_counter_evidence.items_reduced_to_6")
    quality = ((sections.get("quant") or {}).get("oos_probability_quality")) or {}
    if over() and quality.get("path_metrics"):
        quality["path_metrics"] = {name: {"brier_score": row.get("brier_score")}
                                   for name, row in quality.get("path_metrics", {}).items()
                                   if isinstance(row, dict)}
        trimmed.append("quant.oos_probability_quality.path_metrics_reduced")
    if trimmed:
        summary["size_budget"] = {"byte_budget": AGENT_SUMMARY_BYTE_BUDGET,
                                  "bytes": _summary_bytes(summary), "trimmed": trimmed}
    return summary


def build_agent_summary(result: dict[str, Any]) -> dict[str, Any]:
    snapshot, quant = result["snapshot"], result["consensus"]
    quant_fields = reportable_quant_fields(result)
    news, synthesis = result.get("news_result", {}), result.get("synthesis", {})
    paths = [{"id": key, "status": item.get("status"), "role": item.get("result_role"),
              "eligible": item.get("forecast_eligible", False),
              "prob_up": item.get("prob_up") if item.get("forecast_eligible") and result["adversarial_audit"].get("may_report_direction") else None,
              "validation": _validation_summary(item), "exclusions": [bounded_text(text, 200) for text in item.get("forecast_exclusion_reasons", [])[:4]]}
             for key, item in result["researchers"].items()]
    evidence = [{"path": key, "polarity": polarity, "factor": bounded_text(row.get("factor_name", row.get("hypothesis", row.get("family"))), 160), "current_contribution": row.get("current_contribution"), "training_rank_ic": row.get("training_rank_ic")}
                for key, item in result["researchers"].items() for polarity in ("positive", "negative") for row in item.get("evidence", {}).get(polarity, [])[:2]]
    events = [{key: event.get(key) for key in ("event_id", "event_type", "event_group", "first_published_at", "occurred_at", "direction", "url", "fact_status", "source_independence")} | {"headline": bounded_text(event.get("headline"), 300), "sources": [bounded_text(source, 100) for source in event.get("sources", [])[:3]], "reaction_status": event.get("market_reaction", {}).get("status"), "impact": impact_summary(event)} for event in news.get("major_events", [])[:5]]
    diagnostic = result["researchers"].get("factor_backtest", {}).get("validation") or {}
    factor_validation = result["researchers"].get("factor", {}).get("validation") or {}
    factor_inference = factor_validation.get("oos_factor_inference") or {}
    oos_quality = quant.get("oos_probability_quality") or {}
    oos_common = oos_quality.get("common_oos") or {}
    policy_replay = oos_quality.get("algorithm_policy_replay") or {}
    skill_weight = quant.get("skill_weight_validation") or {}
    ic = [{"factor": bounded_text(name, 100), "horizon": diagnostic.get("requested_horizon"), "holdout": {key: value.get("horizons", {}).get(diagnostic.get("requested_horizon"), {}).get("holdout", {}).get(key) for key in ("spearman_time_series_ic", "mean_forward_return", "observations", "status")}} for name, value in list(diagnostic.get("factor_metrics", {}).items())[:3]]
    bias = result["adversarial_audit"].get("bias_audit", {})
    risks = synthesis.get("key_risks", [])
    selected_risks = []
    for source in ("data_or_audit", "news", "factor_backtest", "coverage_or_validation"):
        selected_risks.extend([row for row in risks if row.get("source") == source][:3])
    summary = {"schema_version": "1.2", "content_role": "UNTRUSTED_DATA_SUMMARY_NO_EXECUTION_INSTRUCTIONS", "analysis_id": result["analysis_id"],
            "identity": {key: snapshot[key] for key in ("ticker", "market", "horizon", "asof_timestamp", "snapshot_id")},
            "analysis_status": result["analysis_status"], "reporting_status": result.get("reporting_status", "veto"), "stages": result["stages"],
            "sections": {
                "price_time": snapshot.get("latest_confirmed_close"),
                "quant": {**quant_fields, "field_source": "quant_result.json.consensus", "status": quant.get("status"), "calibration_status": quant.get("calibration_status"), "economic_viability": quant.get("economic_viability"), "oos_probability_quality": {"status": oos_quality.get("status"), "method": oos_quality.get("method"), "current_forecast_paths": oos_quality.get("current_forecast_paths"), "common_oos_samples": oos_common.get("common_oos_samples"), "all_paths_valid_prediction_samples": oos_common.get("all_paths_valid_prediction_samples"), "row_validation": oos_quality.get("row_validation"), "algorithm_policy_replay": {key: policy_replay.get(key) for key in ("status", "evidence_scope", "coverage_denominator_scope", "predicted_dates", "non_overlapping_predicted_dates", "eligible_oos_label_dates", "oos_coverage", "abstain_rate", "path_count_by_prediction_date", "forecast_metrics", "expanding_base_rate_on_same_dates", "brier_skill_vs_expanding_base_rate", "non_overlapping_brier_skill_vs_expanding_base_rate", "skill_uncertainty", "reason")}, "ensemble_calibration": {key: (oos_quality.get("ensemble_calibration") or {}).get(key) for key in ("status", "evidence_scope", "fit_dates", "validation_dates", "validation_raw_brier", "validation_calibrated_brier", "validation_improvement")}, "path_metrics": {name: {key: row.get(key) for key in ("all_evaluable_oos_samples", "valid_probability_predictions", "prediction_days", "non_overlapping_prediction_count", "oos_coverage", "abstain_rate", "brier_score", "log_loss", "expected_calibration_error")} for name, row in oos_quality.get("paths", {}).items()}}, "skill_weight_validation": {key: skill_weight.get(key) for key in ("status", "weighting", "current_weighting_enabled", "deployment_status", "common_oos_samples", "common_complete_prediction_samples", "weight_estimation_start", "weight_estimation_end", "evaluation_start", "evaluation_end", "candidate_weights", "fallback_reason", "evaluation")}, "paths": paths},
                "technical_factor_evidence": evidence,
                "backtest_ic_bias": {"audit_status": result["adversarial_audit"]["status"], "checks": {key: value.get("status") for key, value in bias.items() if isinstance(value, dict)}, "diagnostic_examples": ic, "factor_oos_inference": {key: factor_inference.get(key) for key in ("status", "method", "data_start", "data_end", "sample_count", "trials", "tests_with_p_value", "p_adjustment_method", "factor_selection_used_this_window")}, "scope": "D: single-security time-series diagnostics; B: OOS time-series IC inference; full portfolio backtest/PBO/DSR not computed"},
                "news": {"status": news.get("status"), "event_bias": news.get("overall_direction"), "events": events, "impact_rubric": IMPACT_RUBRIC_MARKER,
                         "ai_lean": {"status": (news.get("ai_lean") or {}).get("status"),
                                     "basis": (news.get("ai_lean") or {}).get("basis"),
                                     "probability_effect": (news.get("ai_lean") or {}).get("probability_effect"),
                                     "coverage": (news.get("ai_lean") or {}).get("coverage"),
                                     "bullish": [{"headline": bounded_text(item.get("headline"), 300), "url": item.get("url"), "reason": bounded_text(item.get("reason"), 300), "published_at": item.get("published_at"), "ticker_relevance": item.get("ticker_relevance")} for item in (news.get("ai_lean") or {}).get("bullish", [])[:3]],
                                     "bearish": [{"headline": bounded_text(item.get("headline"), 300), "url": item.get("url"), "reason": bounded_text(item.get("reason"), 300), "published_at": item.get("published_at"), "ticker_relevance": item.get("ticker_relevance")} for item in (news.get("ai_lean") or {}).get("bearish", [])[:3]],
                                     "mixed": [{"headline": bounded_text(item.get("headline"), 300), "url": item.get("url"), "reason": bounded_text(item.get("reason"), 300)} for item in (news.get("ai_lean") or {}).get("mixed", [])[:2]],
                                     "limitations": [bounded_text(item, 240) for item in (news.get("ai_lean") or {}).get("limitations", [])[:3]]},
                         "coverage": {key: news.get("coverage", {}).get(key) for key in ("observed_publication_start", "observed_publication_end", "eligible_event_count", "eligible_opinion_count", "excluded_evidence_count")}},
                "cross_validation": {key: synthesis.get(key) for key in ("alignment", "alignment_basis", "news_evidence_direction", "overall_confidence", "evidence_links")},
                "risk_counter_evidence": {"items": selected_risks, "total": len(risks), "omitted": len(risks) - len(selected_risks)},
                "final_synthesis": {"status": result["analysis_status"], "may_report_direction": result["adversarial_audit"].get("may_report_direction", False), "quant": dict(quant_fields), "quant_field_source": "quant_result.json.consensus", "economic_viability": quant.get("economic_viability"), "quant_display": _quant_lines(quant_fields), "probability_note": _final_probability_note(quant_fields, snapshot["ticker"], snapshot["horizon"]), "news_relationship": _relationship_text(synthesis), "probability_unavailable_reasons": _unavailable_reasons(result)[:5] if quant_fields["prob_up"] is None else [], "quant_probability_preserved": synthesis.get("quant_probability_preserved", False), "warnings": [bounded_text(item, 240) for item in synthesis.get("warnings", [])[:5]]},
            }}
    return enforce_agent_summary_budget(summary)


def render_markdown(result: dict[str, Any]) -> str:
    manifest, quant, audit = result["snapshot"], result["consensus"], result["adversarial_audit"]
    quant_fields = reportable_quant_fields(result)
    news, synthesis = result.get("news_result", {}), result.get("synthesis", {})
    price = manifest.get("latest_confirmed_close") or {}
    allowed = audit.get("may_report_direction", False)
    lines = [f"# {_cell(manifest['ticker'])} — {_cell(manifest['horizon'])} Research", "",
             f"Analysis status: **{_cell(result.get('analysis_status'))}**. " + ("ABSTAIN: no reporting-eligible direction/probability." if not allowed else "Research estimates with the validation limits below."), "",
             "## 1. Price & Data Time", "",
             f"- Price: {_cell(price.get('price'))} {_cell(price.get('currency'))}; kind: {_cell(price.get('kind'))}",
             f"- Market session: {_cell(price.get('session_date'))}; confirmed close time: {_cell(price.get('close_at'))}",
             f"- As of: {_cell(manifest['asof_timestamp'])}; snapshot: `{manifest['snapshot_id']}`",
             f"- Source: {_cell(price.get('source'))}; bars: {manifest.get('market_bar_count', 0)}; window: {_cell(manifest.get('market_history_start'))} to {_cell(manifest.get('market_history_end'))}",
             f"- Prediction eligibility: {_cell(manifest.get('data_validation', {}).get('prediction_eligibility', {}).get('status'))}; reporting: {_label(manifest.get('data_validation', {}).get('reporting_status'))}; data confidence score: {_cell(manifest.get('data_quality', {}).get('confidence_score'))}/100",
             f"- Calendar/horizon: {_cell(manifest.get('data_validation', {}).get('calendar_verification'))} / {_cell(manifest.get('data_validation', {}).get('horizon_type'))}; adjustment: {_cell(manifest.get('data_validation', {}).get('adjustment_status'))}; corporate actions: {_cell(manifest.get('data_validation', {}).get('corporate_action_status'))}",
             "- Bar close is not a live quote. Source authenticity is not independently verified.", "",
             "## 2. Quant 数据面", "",
             *_quant_lines(quant_fields),
             "", "**动量特征（独立诊断，不是上涨概率）**", "",
             *_momentum_lines(result), _momentum_relationship_line(result),
             *_economic_lines(quant.get("economic_viability")),
             f"- Status: {_cell(quant.get('status'))}",
             "- Confidence、agreement 与 diversity 保留 Quant 原值/原尺度；分类标签不换算为百分比。",
             f"- Calibration: {_cell(quant.get('calibration_status'))}; " + ("equal-weight mean carries an ensemble calibrator that improved the subsequent ledger block (retrospective check, non-independent)." if quant.get("calibrated_probability") is not None else "equal-weight mean does not carry an ensemble calibrator that improved the subsequent ledger check.")]
    probability_range = quant.get("probability_range")
    lines.append(f"- Range across eligible paths (not a statistical confidence interval): {_probability_text(probability_range[0])}–{_probability_text(probability_range[1])}" if probability_range else "- Range across eligible paths: Unavailable")
    lines.extend(["", "| Path | Role / status | Eligible | Reportable P(up) | OOS N | Calibration |", "|---|---|---|---:|---:|---|"])
    for name, item in result["researchers"].items():
        validation = _validation_summary(item)
        lines.append(f"| {_cell(name)} | {_cell(item.get('result_role'))} / {_cell(item.get('status'))} | {_cell(item.get('forecast_eligible'))} | {_probability_text(item.get('prob_up')) if allowed and item.get('forecast_eligible') else 'Unavailable'} | {_cell(validation['oos_predictions'])} | {_cell(validation['calibration'])} |")
    factor_budget = (result["researchers"].get("factor", {}).get("validation") or {}).get("sample_feasibility") or {}
    if factor_budget:
        buckets = factor_budget.get("calibration_bucket_counts") or {}
        bucket_counts = "/".join(str(buckets.get(key, 0)) for key in ("low", "mid", "high"))
        lines.append(f"- Factor bucket sample budget (52-week standard): status {_cell(factor_budget.get('status'))}; the selection segment supports {_cell(factor_budget.get('selection_label_capacity'))} labels (minimum {_cell(factor_budget.get('minimum_selection_labels'))}); matured non-overlapping labels reach at most {_cell(factor_budget.get('max_matured_non_overlapping_label_capacity'))}, with {_cell(factor_budget.get('calibration_observations_with_complete_factor_scores'))} complete factor-score observations and low/mid/high counts {bucket_counts}; balanced three-bucket minimum {_cell(factor_budget.get('required_calibration_observations_for_three_balanced_buckets'))}; estimated minimum history {_cell(factor_budget.get('estimated_minimum_daily_bars'))} daily bars (~{_cell(factor_budget.get('estimated_minimum_calendar_weeks'))} weeks).")
    for name, item in result["researchers"].items():
        lines.extend(f"\n{name} exclusion: {_cell(reason)}" for reason in item.get("forecast_exclusion_reasons", []))
    lines.extend(["", "## 3. Technical & Factor Evidence", ""])
    for name, item in result["researchers"].items():
        for polarity in ("positive", "negative"):
            lines.extend(f"- {_cell(name)} / {polarity}: {_cell(row.get('factor_name', row.get('hypothesis', row.get('family'))))}; current contribution {_cell(row.get('current_contribution'))}; training IC {_cell(row.get('training_rank_ic'))}; samples {_cell(row.get('non_overlapping_samples'))}; Wilson IID-approximate interval {_cell(row.get('wilson_95_interval'))}" for row in item.get("evidence", {}).get(polarity, []))
    if not any(item.get("evidence", {}).get(polarity) for item in result["researchers"].values() for polarity in ("positive", "negative")):
        lines.append("Evidence unavailable or computation skipped by data gates.")
    lines.extend(["", "## 4. Backtest / IC / Bias", "", f"- Audit: {_cell(audit.get('status'))}; verification scope: {_cell(audit.get('verification_scope'))}"])
    lines.extend(f"- VETO: {_cell(veto)}" for veto in audit.get("vetoes", []))
    for name, check in audit.get("bias_audit", {}).items():
        if isinstance(check, dict):
            lines.append(f"- {_cell(name)}: {_cell(check.get('status'))}; {_cell(check.get('detail'))}")
        elif isinstance(check, list):
            lines.extend(f"- OOS {_cell(row.get('researcher_id'))}: Brier {_cell(row.get('brier'))} vs base rate {_cell(row.get('base_rate_brier'))}" for row in check)
    lines.extend(_probability_validation_lines(quant))
    performance = result.get("performance") or {}
    if performance:
        lines.extend(["", "Runtime measurements (this analysis run; wall clock):",
                      f"- Quant through frozen result: {_cell(performance.get('quant_through_frozen_result_wall_seconds'))} s; News through frozen result: {_cell(performance.get('news_through_frozen_result_wall_seconds'))} s; through synthesis: {_cell(performance.get('analysis_through_synthesis_wall_seconds'))} s.",
                      f"- Snapshot loads: {_cell(performance.get('snapshot_loads'))}; researcher snapshot reads: {_cell(performance.get('researcher_snapshot_reads'))}; peak memory: {_cell(performance.get('peak_memory_status'))}.",
                      f"- ML training: {_cell(performance.get('ml_training', {}).get('successful_fits'))}/{_cell(performance.get('ml_training', {}).get('attempts'))} successful/attempted fits; XGBoost: {_cell(performance.get('ml_model_selection', {}).get('xgboost_status'))}."])
        lines.append("- Per-path wall seconds: " + ", ".join(f"{_cell(name)} {_cell(seconds)}" for name, seconds in performance.get("researcher_wall_seconds", {}).items()) + ".")
    diagnostics = result["researchers"].get("factor_backtest", {}).get("validation") or {}
    factor_validation = result["researchers"].get("factor", {}).get("validation") or {}
    factor_inference = factor_validation.get("oos_factor_inference") or {}
    if factor_inference:
        lines.extend(["", ("Factor OOS IC inference (evaluation only; no factor selection): "
                           f"{_cell(factor_inference.get('method'))}; window {_cell(factor_inference.get('data_start'))} to "
                           f"{_cell(factor_inference.get('data_end'))}; N={_cell(factor_inference.get('sample_count'))}; "
                           f"trials={_cell(factor_inference.get('trials'))}; adjustment={_cell(factor_inference.get('p_adjustment_method'))}.")])
        lines.extend(["| Candidate factor | OOS time-series IC | Approx. block-bootstrap interval | Raw p | Adjusted p | N |",
                      "|---|---:|---:|---:|---:|---:|"])
        for name, item in factor_inference.get("factors", {}).items():
            interval = (item.get("block_bootstrap") or {}).get("interval")
            interval_text = f"{interval[0]:.4f} to {interval[1]:.4f}" if isinstance(interval, list) and len(interval) == 2 and all(isinstance(v, (int, float)) for v in interval) else "Not assessed"
            lines.append(f"| {_cell(name)} | {_cell(item.get('oos_time_series_ic'))} | {interval_text} | {_cell(item.get('p_value'))} | {_cell(item.get('p_value_adjusted'))} | {_cell(item.get('observations'))} |")
    lines.extend(["", "D is single-security time-series IC/forward-return diagnostics. Full cross-sectional portfolio backtest, PBO, DSR, actual execution costs/market impact, and survivorship verification are not supplied by this engine; the economic table is assumed-cost sensitivity only.", "",
                  "| Factor | Holdout IC | Mean forward return | N | Status |", "|---|---:|---:|---:|---|"])
    for name, value in diagnostics.get("factor_metrics", {}).items():
        metric = value.get("horizons", {}).get(diagnostics.get("requested_horizon"), {}).get("holdout", {})
        lines.append(f"| {_cell(name)} | {_cell(metric.get('spearman_time_series_ic'))} | {_pct(metric.get('mean_forward_return'))} | {_cell(metric.get('observations'))} | {_cell(metric.get('status'))} |")
    lines.extend(["", "## 5. News & Event Analysis", "", f"- Status: {_cell(news.get('status'))}; eligible event bias: {_cell(news.get('overall_direction'))}; confidence: {_cell(news.get('confidence'))}",
                  f"- Coverage: {_cell(news.get('coverage', {}).get('observed_publication_start'))} to {_cell(news.get('coverage', {}).get('observed_publication_end'))}; excluded records: {_cell(news.get('coverage', {}).get('excluded_evidence_count'))}",
                  "- Reported events, opinions and provider aggregates are separate, uncalibrated DATA. News does not generate a probability."])
    lean = news.get("ai_lean") or {}
    lines.append(f"- Host AI headline lean: {_cell(lean.get('status'))}; basis {_cell(lean.get('basis'))}; judged {_cell((lean.get('coverage') or {}).get('assessed_count'))}/{_cell((lean.get('coverage') or {}).get('candidate_count'))}; effect on probability: {_cell(lean.get('probability_effect'))}")
    for label, key in (("Bullish", "bullish"), ("Bearish", "bearish"), ("Mixed", "mixed"), ("Neutral/unclear", "neutral_or_unclear")):
        for item in lean.get(key) or []:
            lines.append(f"  - {label} lean [{_cell(item.get('evidence_grade'))}; relevance {_cell(item.get('ticker_relevance'))}]: {_link(item.get('headline'), item.get('url'))} — {_cell(item.get('reason'))}")  # noqa: PERF401 - nested label/record loops keep the rendered order explicit
    if lean.get("status") == "not_assessed":
        lines.append("  - No host AI lean artifact was supplied; the message side has no per-item lean evidence (this is not the same as neutral).")
    for event in news.get("major_events", []):
        # report.md is a retained artifact and is not size-bounded, so it renders
        # the full impact block rather than the compact summary projection.
        full = event.get("impact")
        lines.append(f"- {_cell(event.get('first_published_at'))} / {_cell(event.get('event_group'))} / {_cell(event.get('direction'))}: {_link(event.get('headline'), event.get('url'))}; source {_cell(', '.join(event.get('sources', [])))}; reaction {_cell(event.get('market_reaction', {}).get('status'))}")
        if not isinstance(full, dict):
            lines.append("  - Impact grading: not_assessed (this frozen news result carries no impact block)")
            continue
        magnitude = full.get("magnitude") or {}
        pattern = full.get("pattern_comparison") or {}
        breadth = full.get("breadth_scope") or {}
        tier = full.get("source_tier") or {}
        observed = magnitude.get("observed_abs_return")
        magnitude_text = _cell(magnitude.get("band"))
        if magnitude.get("band") == "not_assessed" and magnitude.get("warning"):
            magnitude_text += f" ({_cell(magnitude.get('warning'))})"
        sigma_note = (f", {pattern.get('trailing_sigma_observations')} trailing returns"
                      if pattern.get("trailing_sigma_observations") is not None else "")
        lines.append(f"  - Impact grading: magnitude {magnitude_text} (abs return {_pct(observed) if observed is not None else 'n/a'}); pattern {_cell(pattern.get('pattern'))} (ratio to own trailing sigma {_cell(pattern.get('ratio_to_trailing_sigma'))}{sigma_note}); source tier {_cell(tier.get('tier'))}; declared breadth {_cell(breadth.get('declared_scope'))} with observed breadth {_cell(breadth.get('observed_breadth'))}; composite impact score {_cell(full.get('composite_impact_score'))}")
    lines.extend(f"- Opinion {_cell(opinion.get('sentiment'))}: {_link(opinion.get('headline'), opinion.get('url'))}" for opinion in news.get("opinion_records", [])[:5])
    lines.extend(f"- Background excluded from bias: {_cell(record.get('headline'))}; {_cell('; '.join(record.get('exclusion_reasons', [])))}" for record in news.get("background_records", [])[:5])
    if not news.get("major_events"):
        lines.append("No eligible event evidence; event cross-validation is not assessed.")
    lines.extend(["",
                  "Impact grading is a deterministic News-side heuristic, not a calibrated expectation and not a probability:"])
    lines.extend(f"- {limit}" for limit in impact_rubric_limits())
    lines.extend(["", "## 6. Quant / News Cross-Check", "", f"- Relationship: {_cell(synthesis.get('alignment'))}; basis: {_cell(synthesis.get('alignment_basis'))}",
                  f"- Quant: {_cell(quant_fields['direction'])}; unchanged P(up): {_probability_text(quant_fields['prob_up'])}",
                  f"- News event bias: {_cell(synthesis.get('news_event_bias'))}; tone: {_cell(synthesis.get('news_sentiment'))}",
                  "- Events are candidate explanations for observed market behavior; temporal association does not establish causality.", "", "## 7. Risk & Counter-evidence", ""])
    lines.extend(f"- {_cell(risk.get('source'))}: {_cell(risk.get('detail'))}" for risk in synthesis.get("key_risks", []))
    for name, item in result["researchers"].items():
        lines.extend(f"- {_cell(name)}: {_cell(warning)}" for warning in item.get("warnings", []))
    lines.extend(["", "## 8. Final Synthesis", "", *_quant_lines(quant_fields), "",
                  *_economic_lines(quant.get("economic_viability")), "",
                  _final_probability_note(quant_fields, manifest["ticker"], manifest["horizon"]),
                  _momentum_relationship_line(result),
                  f"- 消息面与 Quant 的关系：{_relationship_text(synthesis)}（{_cell(synthesis.get('alignment'))}）；依据：{_cell(synthesis.get('alignment_basis'))}",
                  f"- Outcome: {_cell(result.get('analysis_status'))}; overall confidence: {_cell(synthesis.get('overall_confidence'))}"])
    lines.extend(f"- {_cell(warning)}" for warning in synthesis.get("warnings", []))
    lines.extend(["- Missing evidence is not filled with invented probabilities or causal claims. Historical validation does not prove future performance.", "", "Stage receipt:", ""])
    lines.extend(f"- {row['stage']}: {_cell(row['status'])}" for row in result.get("stages", []))
    return "\n".join(lines) + "\n"
