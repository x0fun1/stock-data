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
    if result.research_estimate.get('execution') == 'computed':
        return "待验证（候选已计算）"
    return f"不发布（{STATE_LABEL.get(result.state, result.state.value)}）"


def _validation_cell(decision: CapabilityDecision | None) -> str:
    if decision is None:
        return "未执行"
    label = STATE_LABEL.get(decision.state, decision.state.value)
    if decision.state is CapabilityState.VERIFIED:
        return f"{label}；样本外日期 {decision.details.get('independent_dates', '?')}"
    return f"{label}：{_reason_text(decision.reason)}"


def _reason_text(reason: str) -> str:
    if reason.startswith('direction_degenerate:'):
        return '方向输出退化：单一预测类别、零类别识别率或分数近常数；详见两类识别诊断'
    if reason == 'brier_skill_ci_crosses_zero_or_missing' or reason.startswith('brier_skill_ci_crosses_zero'):
        return '尚未证实误差优于历史基准'
    if reason.startswith('coverage_outside_range:'):
        return f"收益区间样本外覆盖率未达要求（{float(reason.split(':', 1)[1]):.1%}）"
    if reason.startswith('insufficient_out_of_sample_evidence'):
        return '独立样本外样本/日期不足'
    return {'no_active_verified_model':'该期限/目标尚未登记合格模型',
            'validated_candidate_not_activated':'统计验证完成，数据准入/激活尚未完成',
            'no_price_at_cutoff':'截止时点没有可用的完整行情'}.get(reason, reason)


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
    if any(result.research_estimate.get('quantiles') for result in results):
        return "候选区间已计算，验证未通过"
    risk = next((result.risk for result in results if 'return_median' in result.risk), None)
    if risk:
        return f"历史 {_pct(risk['return_median'])}（{_pct(risk['return_q10'])}/{_pct(risk['return_q90'])}）；非预测"
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
    if bundle.run_manifest.snapshot.get('knowledge_cutoff'):
        lines.append(f"- 完整日线截止：{bundle.run_manifest.snapshot.get('cutoff')}；新闻/知识截止：{bundle.run_manifest.snapshot['knowledge_cutoff']}")
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
        computed = [result for result in own if result.research_estimate.get('execution') == 'computed']
        if computed:
            lines += ['', '候选模型已实际计算；下列分数/区间为研究输出，尚未取得已验证概率或预测区间资格。', '',
                '| 期限/目标 | 研究输出 | 样本外验证 |', '|---|---|---|']
            for result in computed:
                estimate = result.research_estimate
                if 'score' in estimate:
                    value = f"分数 {estimate['score']:.4f}（不作真实概率）"
                else:
                    q = estimate['quantiles']
                    value = f"收益估算 {_pct(q.get('q50'))}（10% {_pct(q.get('q10'))} / 90% {_pct(q.get('q90'))}）；未验证"
                lines.append(f"| {result.horizon}日/{result.target.value} | {value} | {_reason_text(result.reason)}；{result.validation.get('independent_dates', '?')}个测试日期 |")
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
    impact = bundle.news_impact_summary
    if impact.get('status')=='computed':
        lines += ['', f"新闻综合判断：**{impact['classification']}**；利多贡献 {impact['positive_contribution']:.3f}，利空贡献 {impact['negative_contribution']:.3f}，净贡献 {impact['net_contribution']:+.3f}。",
            '贡献=方向符号×影响强度×证据可信度×关联度×时间衰减；按独立事件去重。这是新闻影响研究分数，不是上涨概率。', '',
            '| 事件 | 符号 | 影响 | 原始信息时间 | 时间权重 | 净贡献 |', '|---|---|---|---|---|---|']
        for item in impact.get('events',[])[:20]:
            sign={-1:'−',0:'0',1:'+'}[item['direction_sign']]
            level={'major':'重大','medium':'中等','light':'轻微'}[item['impact_level']]
            lines.append(f"| {item['title']} | {sign} | {level} | {item['event_time']} | {item['time_decay']:.3f} | {item['signed_contribution']:+.3f} |")
    for ticker,status in impact.get('coverage',{}).items():
        if status:
            counts=status['counts']
            lines.append(f"{ticker}逐项工作流：{status['completed']}/{status['total']}项已处理；已核实{counts['verified']}、无关联{counts['unrelated']}、核实未完成{counts['unresolved']}、待处理{counts['pending']}。未核实项不当作中性。")
            for item in status.get('unresolved_items',[])[:5]:
                lines.append(f"- 核实缺口：{item['title']}；原因：{item['reason']}")
    if not bundle.context_events:
        lines.append("观察窗内没有合格事件证据。")
    else:
        lines.append("以下为当前上下文；未经历史验证的新闻不调整上述模型概率。")
        lines.append("")
        reviewed = [event for event in bundle.context_events if event.grade is not Grade.INSUFFICIENT]
        pending = [event for event in bundle.context_events if event.grade is Grade.INSUFFICIENT]
        for event in reviewed:
            published = event.published_at.isoformat() if event.published_at else "时间未知"
            scope = {'official_excerpt':'官方公告/摘录已核对', 'source_excerpt':'原文相关段落已核对', 'full_text':'媒体正文已核对', 'numeric':'数值证据已核对'}.get(event.evidence_scope.value, event.evidence_scope.value)
            sources = event.permissions.get('review_sources', [])
            source_label = sources[0].get('title', event.source_id) if sources else event.source_id
            method = '正文/公告核实' if event.grade_method == 'source_review' else event.grade_method
            url = f"；{event.url}" if event.url else ""
            lines.append(
                f"- **{event.grade.value}**：{event.title}（{source_label}，{published}，证据范围：{scope}，"
                f"方法：{method}{url}）"
            )
            if event.grade_reason:
                lines.append(f"  - 依据：{event.grade_reason}")
            if event.fact_summary:
                lines.append(f"  - 事实摘要：{event.fact_summary}")
            for source in event.permissions.get('review_sources', []):
                lines.append(f"  - 核实来源：[{source.get('title', '原文')}]({source['url']})；发布日期：{source.get('published_at', '未知')}")
            if event.permissions.get('conditions'):
                lines.append('  - 待确认条件：' + event.permissions['conditions'])
            if event.permissions.get('impact_reason'):
                lines.append('  - 影响强度依据：' + event.permissions['impact_reason'])
        if pending:
            lines += ['', f'展示窗中另有 {len(pending)} 条发现记录未形成已核实方向评估；逐项检索与具体缺口保留在工作流/JSON中，不计作独立利多/利空。']
            for event in pending[:5]:
                lines.append(f"- 待核实：[{event.title}]({event.url})；{event.permissions.get('workflow_reason',event.grade_reason)}")

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
        if impact.get('status')=='computed':
            lines.append(f"在{impact['asof']}知识截止下，已核实新闻按时间衰减和独立事件去重后的综合判断为{impact['classification']}，净贡献{impact['net_contribution']:+.3f}，共{impact['event_count']}个事件。")
        lines.append("新闻贡献描述当前证据方向，未进入Kronos量价模型；事件后价格变化不证明因果关系或已充分定价。")
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
