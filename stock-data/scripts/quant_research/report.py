"""Eight-section reports and a bounded DATA-only projection for the host Agent."""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any
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
}


def _label(value: Any) -> str:
    return LABELS.get(str(value), _cell(value))


def _confidence_reason_text(value: Any) -> str:
    return CONFIDENCE_REASON_LABELS.get(str(value), _cell(value))


def _quant_lines(fields: dict[str, Any]) -> list[str]:
    """Presentation only: retain native Quant categories, never invent percent scores."""
    lines = [f"- 上涨概率 P(up)：{_probability_text(fields['prob_up'])}" if fields["prob_up"] is not None else "- 上涨概率：不可用",
             f"- 方向：{_label(fields['direction'])}",
             f"- Quant Confidence：{_label(fields['confidence'])}",
             f"- 置信度评分：{_cell(fields.get('confidence_score'))}/100",
             f"- 报告状态：{_label(fields.get('reporting_status'))}",
             f"- 模型一致度（agreement）：{_label(fields['agreement'])}"]
    if fields.get("raw_probability") is not None:
        lines.append(f"- 原始路径估计均值：{_probability_text(fields['raw_probability'])}")
    if fields.get("calibrated_probability") is not None:
        lines.append(f"- 校准概率：{_probability_text(fields['calibrated_probability'])}")
        calibration_line = "- 联合概率校准：可用（按已验证的 ensemble calibration）"
    else:
        calibration_line = "- 联合概率校准：不可用（等权路径均值未经独立 ensemble calibration）"
    lines.append(calibration_line)
    if fields.get("confidence_reasons"):
        reasons = ", ".join(_confidence_reason_text(item) for item in fields["confidence_reasons"][:4])
        lines.append("- 低置信度原因：" + reasons)
    if "diversity" in fields:
        lines.append(f"- 证据多样性（diversity）：{_label(fields['diversity'])}")
    if fields.get("horizon_type") == "estimated_observed_sessions":
        lines.append("- 预测窗口：基于 OHLCV 观测行估算交易 session；交易日历未完整验证")
    return lines


def _final_probability_note(fields: dict[str, Any], ticker: str, horizon: str) -> str:
    if fields["prob_up"] is None:
        return "上涨概率：不可用。Quant 未提供通过报告资格的有效概率；不得由 LLM 或新闻补算。"
    probability = _probability_text(fields["prob_up"])
    return (f"Quant 当前对 {_cell(ticker)} 的 {_cell(horizon)} 窗口给出 {probability} 的上涨概率（合格路径等权均值，未单独进行 ensemble calibration）。"
            f"News 只补充驱动解释和风险证据，不改变 Quant 的 {probability} 原始概率（等权路径均值）。")


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
    """Literal delivery fields generated from this result, never from News values."""
    snapshot, fields = result["snapshot"], reportable_quant_fields(result)
    required = {
        "identity": f"- 标的：{_cell(snapshot['ticker'])}；市场：{_cell(snapshot['market'])}；预测窗口：{_cell(snapshot['horizon'])}（交易 session）；分析截至：{_cell(snapshot['asof_timestamp'])}",
        "probability_note": _final_probability_note(fields, snapshot["ticker"], snapshot["horizon"]),
        "news_relationship": f"- 消息面与 Quant 的关系：{_relationship_text(result.get('synthesis', {}))}",
        "reporting_status": f"- 报告状态：{_cell(fields.get('reporting_status', result.get('reporting_status', 'degraded')))}",
        "analysis_status": f"- 执行状态：{_cell(result['analysis_status'])}",
    }
    names = ["prob_up", "direction", "confidence", "confidence_score", "reporting_status", "agreement"]
    if fields.get("raw_probability") is not None:
        names.append("raw_probability")
    if fields.get("calibrated_probability") is not None:
        names.append("calibrated_probability")
    else:
        names.append("ensemble_calibration_status")
    if fields.get("confidence_reasons"):
        names.append("confidence_reasons")
    if "diversity" in fields:
        names.append("diversity")
    if fields.get("horizon_type") == "estimated_observed_sessions":
        names.append("horizon_type")
    required.update(zip(names, _quant_lines(fields)))
    if fields["prob_up"] is None:
        required["unavailable_reason"] = "- 概率不可用原因：" + _cell(_unavailable_reasons(result)[0])
    else:
        required["calibration"] = next(line for line in _quant_lines(fields) if line.startswith("- 联合概率校准："))
    return required


def render_final_response(result: dict[str, Any]) -> str:
    """Default to a short human-facing summary; detailed evidence remains in the standard/debug artifacts."""
    required, summary = required_response_lines(result), build_agent_summary(result)
    snapshot, sections = result["snapshot"], summary["sections"]
    price, news = sections["price_time"] or {}, sections["news"]
    lines = [f"# {_cell(snapshot['ticker'])} 简析", "", required["identity"],
             f"- 最新确认收盘：{_cell(price.get('price'))} {_cell(price.get('currency'))}，日期 {_cell(price.get('session_date'))}；来源 {_cell(price.get('source'))}（非实时价）",
             "", "### Quant", "", *_quant_lines(reportable_quant_fields(result))]
    if "calibration" not in required:
        lines.append(required["unavailable_reason"])
    evidence = sections["technical_factor_evidence"][:3]
    for row in evidence:
        lines.append(f"- 核心依据：{_cell(row['factor'])}（{_cell(row['path'])} / {_cell(row['polarity'])}）")
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

    lines.extend(["", "### News / Sentiment", "",
                  f"- 消息评估：{_cell(news.get('status'))}；事件方向：{_cell(news.get('event_bias'))}"])
    for event in news.get("events", [])[:2]:
        lines.append(f"- {_cell(event.get('first_published_at'))}：{_link(event.get('headline'), event.get('url'))}；方向 {_cell(event.get('direction'))}")
    if not news.get("events"):
        lines.append("- 无合格事件证据；不把消息缺失视为中性或已验证。")
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
            "calibration": item.get("probability_calibration", "not_assessed")}


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
    events = [{key: event.get(key) for key in ("event_id", "event_type", "event_group", "first_published_at", "occurred_at", "updated_at", "direction", "url", "fact_status", "source_independence")} | {"headline": bounded_text(event.get("headline"), 300), "sources": [bounded_text(source, 100) for source in event.get("sources", [])[:3]], "reaction_status": event.get("market_reaction", {}).get("status")} for event in news.get("major_events", [])[:5]]
    diagnostic = result["researchers"].get("factor_backtest", {}).get("validation") or {}
    ic = [{"factor": bounded_text(name, 100), "horizon": diagnostic.get("requested_horizon"), "holdout": {key: value.get("horizons", {}).get(diagnostic.get("requested_horizon"), {}).get("holdout", {}).get(key) for key in ("spearman_time_series_ic", "mean_forward_return", "observations", "status")}} for name, value in list(diagnostic.get("factor_metrics", {}).items())[:3]]
    bias = result["adversarial_audit"].get("bias_audit", {})
    risks = synthesis.get("key_risks", [])
    selected_risks = []
    for source in ("data_or_audit", "news", "factor_backtest", "coverage_or_validation"):
        selected_risks.extend([row for row in risks if row.get("source") == source][:3])
    return {"schema_version": "1.2", "content_role": "UNTRUSTED_DATA_SUMMARY_NO_EXECUTION_INSTRUCTIONS", "analysis_id": result["analysis_id"],
            "identity": {key: snapshot[key] for key in ("ticker", "market", "horizon", "asof_timestamp", "snapshot_id")},
            "analysis_status": result["analysis_status"], "reporting_status": result.get("reporting_status", "veto"), "stages": result["stages"],
            "sections": {
                "price_time": snapshot.get("latest_confirmed_close"),
                "quant": {**quant_fields, "field_source": "quant_result.json.consensus", "status": quant.get("status"), "calibration_status": quant.get("calibration_status"), "paths": paths},
                "technical_factor_evidence": evidence,
                "backtest_ic_bias": {"audit_status": result["adversarial_audit"]["status"], "checks": {key: value.get("status") for key, value in bias.items() if isinstance(value, dict)}, "diagnostic_examples": ic, "scope": "D: single-security time-series diagnostics; full portfolio backtest/PBO/DSR not computed"},
                "news": {"status": news.get("status"), "event_bias": news.get("overall_direction"), "events": events, "coverage": {key: news.get("coverage", {}).get(key) for key in ("observed_publication_start", "observed_publication_end", "eligible_event_count", "eligible_opinion_count", "excluded_evidence_count")}},
                "cross_validation": {key: synthesis.get(key) for key in ("alignment", "alignment_basis", "news_evidence_direction", "overall_confidence", "evidence_links")},
                "risk_counter_evidence": {"items": selected_risks, "total": len(risks), "omitted": len(risks) - len(selected_risks)},
                "final_synthesis": {"status": result["analysis_status"], "may_report_direction": result["adversarial_audit"].get("may_report_direction", False), "quant": dict(quant_fields), "quant_field_source": "quant_result.json.consensus", "quant_display": _quant_lines(quant_fields), "probability_note": _final_probability_note(quant_fields, snapshot["ticker"], snapshot["horizon"]), "news_relationship": _relationship_text(synthesis), "probability_unavailable_reasons": _unavailable_reasons(result)[:5] if quant_fields["prob_up"] is None else [], "quant_probability_preserved": synthesis.get("quant_probability_preserved", False), "warnings": [bounded_text(item, 240) for item in synthesis.get("warnings", [])[:5]]},
            }}


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
             f"- Status: {_cell(quant.get('status'))}",
             "- Confidence、agreement 与 diversity 保留 Quant 原值/原尺度；分类标签不换算为百分比。",
             f"- Calibration: {_cell(quant.get('calibration_status'))}; equal-weight mean is not an independently calibrated ensemble probability."]
    probability_range = quant.get("probability_range")
    lines.append(f"- Range across eligible paths (not a statistical confidence interval): {_probability_text(probability_range[0])}–{_probability_text(probability_range[1])}" if probability_range else "- Range across eligible paths: Unavailable")
    lines.extend(["", "| Path | Role / status | Eligible | Reportable P(up) | OOS N | Calibration |", "|---|---|---|---:|---:|---|"])
    for name, item in result["researchers"].items():
        validation = _validation_summary(item)
        lines.append(f"| {_cell(name)} | {_cell(item.get('result_role'))} / {_cell(item.get('status'))} | {_cell(item.get('forecast_eligible'))} | {_probability_text(item.get('prob_up')) if allowed and item.get('forecast_eligible') else 'Unavailable'} | {_cell(validation['oos_predictions'])} | {_cell(validation['calibration'])} |")
    for name, item in result["researchers"].items():
        for reason in item.get("forecast_exclusion_reasons", []):
            lines.append(f"\n{name} exclusion: {_cell(reason)}")
    lines.extend(["", "## 3. Technical & Factor Evidence", ""])
    for name, item in result["researchers"].items():
        for polarity in ("positive", "negative"):
            for row in item.get("evidence", {}).get(polarity, []):
                lines.append(f"- {_cell(name)} / {polarity}: {_cell(row.get('factor_name', row.get('hypothesis', row.get('family'))))}; current contribution {_cell(row.get('current_contribution'))}; training IC {_cell(row.get('training_rank_ic'))}; samples {_cell(row.get('non_overlapping_samples'))}; Wilson 95% interval {_cell(row.get('wilson_95_interval'))}")
    if not any(item.get("evidence", {}).get(polarity) for item in result["researchers"].values() for polarity in ("positive", "negative")):
        lines.append("Evidence unavailable or computation skipped by data gates.")
    lines.extend(["", "## 4. Backtest / IC / Bias", "", f"- Audit: {_cell(audit.get('status'))}; verification scope: {_cell(audit.get('verification_scope'))}"])
    for veto in audit.get("vetoes", []):
        lines.append(f"- VETO: {_cell(veto)}")
    for name, check in audit.get("bias_audit", {}).items():
        if isinstance(check, dict):
            lines.append(f"- {_cell(name)}: {_cell(check.get('status'))}; {_cell(check.get('detail'))}")
        elif isinstance(check, list):
            for row in check:
                lines.append(f"- OOS {_cell(row.get('researcher_id'))}: Brier {_cell(row.get('brier'))} vs base rate {_cell(row.get('base_rate_brier'))}")
    diagnostics = result["researchers"].get("factor_backtest", {}).get("validation") or {}
    lines.extend(["", "D is single-security time-series IC/forward-return diagnostics. Full cross-sectional portfolio backtest, PBO, DSR, costs and survivorship verification are not supplied by this engine.", "",
                  "| Factor | Holdout IC | Mean forward return | N | Status |", "|---|---:|---:|---:|---|"])
    for name, value in diagnostics.get("factor_metrics", {}).items():
        metric = value.get("horizons", {}).get(diagnostics.get("requested_horizon"), {}).get("holdout", {})
        lines.append(f"| {_cell(name)} | {_cell(metric.get('spearman_time_series_ic'))} | {_pct(metric.get('mean_forward_return'))} | {_cell(metric.get('observations'))} | {_cell(metric.get('status'))} |")
    lines.extend(["", "## News & Event Analysis", "", f"- Status: {_cell(news.get('status'))}; eligible event bias: {_cell(news.get('overall_direction'))}; confidence: {_cell(news.get('confidence'))}",
                  f"- Coverage: {_cell(news.get('coverage', {}).get('observed_publication_start'))} to {_cell(news.get('coverage', {}).get('observed_publication_end'))}; excluded records: {_cell(news.get('coverage', {}).get('excluded_evidence_count'))}",
                  "- Reported events, opinions and provider aggregates are separate, uncalibrated DATA. News does not generate a probability."])
    for event in news.get("major_events", []):
        lines.append(f"- {_cell(event.get('first_published_at'))} / {_cell(event.get('event_group'))} / {_cell(event.get('direction'))}: {_link(event.get('headline'), event.get('url'))}; source {_cell(', '.join(event.get('sources', [])))}; reaction {_cell(event.get('market_reaction', {}).get('status'))}")
    for opinion in news.get("opinion_records", [])[:5]:
        lines.append(f"- Opinion {_cell(opinion.get('sentiment'))}: {_link(opinion.get('headline'), opinion.get('url'))}")
    for record in news.get("background_records", [])[:5]:
        lines.append(f"- Background excluded from bias: {_cell(record.get('headline'))}; {_cell('; '.join(record.get('exclusion_reasons', [])))}")
    if not news.get("major_events"):
        lines.append("No eligible event evidence; event cross-validation is not assessed.")
    lines.extend(["", "## Quant / News Cross-Check", "", f"- Relationship: {_cell(synthesis.get('alignment'))}; basis: {_cell(synthesis.get('alignment_basis'))}",
                  f"- Quant: {_cell(quant_fields['direction'])}; unchanged P(up): {_probability_text(quant_fields['prob_up'])}",
                  f"- News event bias: {_cell(synthesis.get('news_event_bias'))}; tone: {_cell(synthesis.get('news_sentiment'))}",
                  "- Events are candidate explanations for observed market behavior; temporal association does not establish causality.", "", "## 7. Risk & Counter-evidence", ""])
    for risk in synthesis.get("key_risks", []):
        lines.append(f"- {_cell(risk.get('source'))}: {_cell(risk.get('detail'))}")
    for name, item in result["researchers"].items():
        for warning in item.get("warnings", []):
            lines.append(f"- {_cell(name)}: {_cell(warning)}")
    lines.extend(["", "## 8. Final Synthesis", "", *_quant_lines(quant_fields), "",
                  _final_probability_note(quant_fields, manifest["ticker"], manifest["horizon"]),
                  f"- 消息面与 Quant 的关系：{_relationship_text(synthesis)}（{_cell(synthesis.get('alignment'))}）；依据：{_cell(synthesis.get('alignment_basis'))}",
                  f"- Outcome: {_cell(result.get('analysis_status'))}; overall confidence: {_cell(synthesis.get('overall_confidence'))}"])
    for warning in synthesis.get("warnings", []):
        lines.append(f"- {_cell(warning)}")
    lines.extend(["- Missing evidence is not filled with invented probabilities or causal claims. Historical validation does not prove future performance.", "", "Stage receipt:", ""])
    lines.extend(f"- {row['stage']}: {_cell(row['status'])}" for row in result.get("stages", []))
    return "\n".join(lines) + "\n"
