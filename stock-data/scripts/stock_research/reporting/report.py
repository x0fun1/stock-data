"""Single-message report assembly.

Only verified values are printed as probabilities.  Everything else is shown as
its state and reason; no placeholder 0 or 50% is generated.
"""

from __future__ import annotations

from collections import defaultdict

from ..contracts import (
    CapabilityDecision,
    CapabilityState,
    ContextEvent,
    FeatureObservation,
    Grade,
    QuantitativeResult,
    ReportBundle,
    Target,
)

STATE_LABEL = {
    CapabilityState.VERIFIED: "已验证",
    CapabilityState.UNVERIFIED: "未证实优于基准",
    CapabilityState.FAILED: "验证失败",
    CapabilityState.NOT_EXECUTED: "未执行",
    CapabilityState.INSUFFICIENT_DATA: "数据不足",
    CapabilityState.DESCRIPTIVE: "仅描述",
    CapabilityState.NOT_PUBLISHED: "不发布",
}


def _pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "不发布"
    return f"{value * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "缺失"
    return f"{value:.{digits}f}"


def _prob_cell(result: QuantitativeResult | None) -> str:
    if result is None:
        return "未执行"
    if result.state is CapabilityState.VERIFIED and result.probability is not None:
        return _pct(result.probability)
    return f"不发布（{STATE_LABEL.get(result.state, result.state.value)}）"


def _validation_cell(decision: CapabilityDecision | None) -> str:
    if decision is None:
        return "未执行"
    label = STATE_LABEL.get(decision.state, decision.state.value)
    if decision.state is CapabilityState.VERIFIED:
        return f"{label}；样本外日期 {decision.details.get('independent_dates', '?')}"
    return f"{label}：{decision.reason}"


def _feature_line(features: dict[str, FeatureObservation], names: list[str]) -> str:
    parts = []
    for name in names:
        obs = features.get(name)
        if obs is None:
            parts.append(f"{name}=缺失")
        elif obs.status == "ok":
            parts.append(f"{name}={obs.value:.4f}")
        else:
            parts.append(f"{name}=缺失({obs.reason})")
    return "；".join(parts)


def _quantile_text(results: list[QuantitativeResult]) -> str:
    for result in results:
        if result.quantiles:
            q = result.quantiles
            return f"{_num(q.get('q50'))}（10% {_num(q.get('q10'))} / 90% {_num(q.get('q90'))}）"
    return "样本不足"


def _risk_text(result: QuantitativeResult | None, features: dict[str, FeatureObservation]) -> str:
    if result is not None and result.risk:
        risk = result.risk
        if "worst_floating_return" in risk:
            return f"历史路径最差 {_pct(risk['worst_floating_return'])}；最大回撤 {_pct(risk.get('max_drawdown_close_path'))}"
        if risk.get("reason"):
            return f"缺失（{risk['reason']}）"
    dd = features.get("dd_60")
    rv = features.get("rv_20")
    bits = []
    if dd and dd.status == "ok":
        bits.append(f"当前回撤 {_pct(dd.value)}")
    if rv and rv.status == "ok":
        bits.append(f"20日年化波动 {_pct(rv.value)}")
    return "；".join(bits) if bits else "缺失"


def _decision_for(decisions: list[CapabilityDecision], security_id: str | None, horizon: int, target: Target) -> CapabilityDecision | None:
    for decision in decisions:
        if decision.horizon == horizon and decision.target is target and decision.security_id in {"", security_id or ""}:
            return decision
    return None


def render_report(bundle: ReportBundle) -> str:
    req = bundle.request
    lines: list[str] = []
    lines.append(f"# 研究报告：{', '.join(s.ticker for s in bundle.securities) or ', '.join(req.tickers)}")
    lines.append("")
    lines.append(
        f"- 市场：{req.market.value}；币种：{', '.join(sorted({s.currency for s in bundle.securities})) or '未知'}；"
        f"截止：{req.cutoff.isoformat() if req.cutoff else bundle.run_manifest.snapshot.get('cutoff', '最新可用交易日')}"
    )
    snapshot = bundle.run_manifest.snapshot.get("datasets", {})
    versions = "；".join(f"{k}={v['version']}" for k, v in snapshot.items()) if snapshot else "无已登记数据版本"
    lines.append(f"- 数据版本：{versions}")
    lines.append(f"- 数据截止与模型版本：{bundle.run_manifest.policy_version}；运行：{bundle.run_manifest.run_id}")
    if bundle.headline:
        lines.append(f"- 结论：{bundle.headline}")
    lines.append("")

    lines.append("## 量化结果")
    if not bundle.results:
        lines.append("没有可发布的量化结果。以下数据缺口不自动等于中性。")
    for security in bundle.securities:
        own = [r for r in bundle.results if r.security_id == security.security_id]
        features = bundle.features.get(security.security_id, {})
        lines.append("")
        lines.append(f"### {security.ticker}（{security.security_id}）")
        lines.append("")
        lines.append("| 期限 | 绝对上涨概率 | 跑赢基准概率 | 条件收益中位数（10%/90%） | 下行/路径风险 | 验证状态 |")
        lines.append("|---|---|---|---|---|---|")
        for horizon in req.horizons:
            abs_result = next((r for r in own if r.horizon == horizon and r.target is Target.ABSOLUTE), None)
            ex_result = next((r for r in own if r.horizon == horizon and r.target is Target.EXCESS), None)
            decision = _decision_for(bundle.decisions, security.security_id, horizon, Target.ABSOLUTE)
            lines.append(
                f"| {horizon} 交易日 | {_prob_cell(abs_result)} | {_prob_cell(ex_result)} | "
                f"{_quantile_text([r for r in own if r.horizon == horizon])} | "
                f"{_risk_text(abs_result or ex_result, features)} | {_validation_cell(decision)} |"
            )
        baselines = sorted({r.baseline_probability for r in own if r.baseline_probability is not None})
        if baselines:
            lines.append("")
            lines.append(
                "基准率仅表示训练/历史样本中的上涨比例，不是预测："
                + "、".join(f"{b:.1%}" for b in baselines)
                + "。未获验证前不把候选模型概率写成个股预测。"
            )
        lines.append("")
        lines.append(
            "- 趋势/反转：" + _feature_line(features, ["mom_60", "mom_12_1", "trend_diff", "residual_reversal"])
        )
        lines.append(
            "- 波动/流动性：" + _feature_line(features, ["rv_20", "vol_change", "dd_60", "abnormal_volume", "amihud_20"])
        )
        lines.append(
            "- 价值/质量：" + _feature_line(features, ["ep_ttm", "fcf_yield_ttm", "roa_ttm", "accruals_ttm", "revenue_growth_yoy", "net_debt_ratio"])
        )
        lines.append(
            "- 说明性指标（不单独参与模型投票）：" + _feature_line(features, ["rsi_14", "macd_12_26_9", "bollinger_position_20", "atr_14_pct"])
        )
        baseline = bundle.baseline_forecasts.get(security.security_id)
        if baseline:
            from ..control.kronos_baseline import render_baseline
            lines.append('')
            lines.append(render_baseline(baseline).replace('### ', '#### ', 1).rstrip())

    lines.append("")
    if bundle.multifactor_snapshot:
        from ..control.multifactor import render_multifactor
        lines.append(render_multifactor(bundle.multifactor_snapshot).rstrip())
        lines.append("")
    lines.append("## 新闻与事件")
    if not bundle.context_events:
        lines.append("观察窗内没有合格事件证据。")
    else:
        lines.append("以下为当前上下文；未经历史验证的新闻不调整上述模型概率。")
        lines.append("")
        for event in bundle.context_events:
            published = event.published_at.isoformat() if event.published_at else "时间未知"
            scope = event.evidence_scope.value
            url = f"；{event.url}" if event.url else ""
            lines.append(
                f"- **{event.grade.value}**：{event.title}（{event.source_id}，{published}，证据范围：{scope}，"
                f"方法：{event.grade_method}{url}）"
            )
            if event.grade_reason:
                lines.append(f"  - 依据：{event.grade_reason}")
            if event.fact_summary:
                lines.append(f"  - 事实摘要：{event.fact_summary}")

    lines.append("")
    lines.append("## 综合摘要")
    verified = [d for d in bundle.decisions if d.state is CapabilityState.VERIFIED]
    unverified = [d for d in bundle.decisions if d.state is CapabilityState.UNVERIFIED]
    failed = [d for d in bundle.decisions if d.state in {CapabilityState.FAILED, CapabilityState.INSUFFICIENT_DATA}]
    if verified:
        lines.append(f"已有 {len(verified)} 个市场/期限/目标通过登记验证，表中只把这些值作为概率参考。")
    else:
        lines.append("尚未建立通过验证的概率参考；表中保留描述、基准和缺口，不用新闻或中性值填充预测。")
    executed = sum(value.get('status') == 'ok' for value in bundle.baseline_forecasts.values())
    if executed:
        lines.append(f'Kronos-small已完成 {executed} 个证券的下一根量价基准推理；生成方向与路径票数单列，未作为校准投资概率。')
    if unverified:
        lines.append(f"{len(unverified)} 个目标有候选模型但样本外优势/校准未通过，标为未证实。")
    if failed:
        lines.append(f"{len(failed)} 个目标因数据或验证失败不发布。")
    if bundle.context_events:
        lines.append("新闻与量化可能一致也可能冲突；事件后价格变化不证明因果关系，也不证明已充分定价。")
    lines.append("综合摘要不覆盖任何“不发布”决定，不提供买卖、仓位或执行指令。")

    lines.append("")
    lines.append("## 限制与复现")
    for limitation in bundle.limitations:
        lines.append(f"- {limitation}")
    lines.append(f"- 运行清单：{bundle.run_manifest.run_id}；代码版本：{bundle.run_manifest.code_version or '未记录'}")
    lines.append(f"- 策略哈希：{bundle.run_manifest.snapshot.get('policy_hash', '未记录')}")
    for stage in bundle.run_manifest.stages:
        if stage.get("status") != "ok":
            lines.append(f"- 阶段 {stage.get('name')}：{stage.get('status')}（{stage.get('detail', '')}）")
    return "\n".join(lines).rstrip() + "\n"
