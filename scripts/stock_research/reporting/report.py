"""Single-message report assembly for news, macro and company-message research.

Graded conclusions come only from reviewed evidence.  A gap is shown with its
reason; it is never rendered as neutral.
"""

from __future__ import annotations

from ..contracts import Grade, ReportBundle

SCOPE_LABEL = {
    "official_excerpt": "官方公告/摘录已核对",
    "source_excerpt": "原文相关段落已核对",
    "full_text": "媒体正文已核对",
    "numeric": "数值证据已核对",
}


def _macro_lines(bundle: ReportBundle) -> list[str]:
    lines = ["## 宏观背景"]
    if not bundle.macro_background:
        return lines + ["没有已登记的宏观序列。"]
    lines.append("以下为知识截止时已公开的宏观读数，只作背景描述，不是预测；缺少公开时点的观测不计入。")
    lines.append("")
    for item in bundle.macro_background:
        series = "/".join(item["series"])
        if item["status"] == "ok":
            lines.append(f"- {item['label']}：{item['value']:.4f}（{series}；最新观测 {item['inputs'].get('latest', '未知')}）")
        else:
            lines.append(f"- {item['label']}：缺失（{series}；{item['reason']}）")
    return lines


def _news_lines(bundle: ReportBundle) -> list[str]:
    lines = ["## 新闻与事件"]
    impact = bundle.news_impact_summary
    if impact.get("status") == "computed":
        lines += ["", f"新闻综合判断：**{impact['classification']}**；利多贡献 {impact['positive_contribution']:.3f}，利空贡献 {impact['negative_contribution']:.3f}，净贡献 {impact['net_contribution']:+.3f}。",
                  "贡献=方向符号×影响强度×证据可信度×关联度×时间衰减；按独立事件去重。这是新闻影响研究分数，不是上涨概率。", "",
                  "| 事件 | 符号 | 影响 | 原始信息时间 | 时间权重 | 净贡献 |", "|---|---|---|---|---|---|"]
        for item in impact.get("events", [])[:20]:
            sign = {-1: "−", 0: "0", 1: "+"}[item["direction_sign"]]
            level = {"major": "重大", "medium": "中等", "light": "轻微"}[item["impact_level"]]
            lines.append(f"| {item['title']} | {sign} | {level} | {item['event_time']} | {item['time_decay']:.3f} | {item['signed_contribution']:+.3f} |")
    for ticker, status in impact.get("coverage", {}).items():
        if status:
            counts = status["counts"]
            lines.append(f"{ticker}逐项工作流：{status['completed']}/{status['total']}项已处理；已核实{counts['verified']}、无关联{counts['unrelated']}、核实未完成{counts['unresolved']}、待处理{counts['pending']}。未核实项不当作中性。")
            for item in status.get("unresolved_items", [])[:5]:
                lines.append(f"- 核实缺口：{item['title']}；原因：{item['reason']}")
    if not bundle.context_events:
        return lines + ["观察窗内没有合格事件证据。"]
    lines.append("")
    reviewed = [event for event in bundle.context_events if event.grade is not Grade.INSUFFICIENT]
    pending = [event for event in bundle.context_events if event.grade is Grade.INSUFFICIENT]
    for event in reviewed:
        published = event.published_at.isoformat() if event.published_at else "时间未知"
        scope = SCOPE_LABEL.get(event.evidence_scope.value, event.evidence_scope.value)
        sources = event.permissions.get("review_sources", [])
        source_label = sources[0].get("title", event.source_id) if sources else event.source_id
        method = "正文/公告核实" if event.grade_method == "source_review" else event.grade_method
        url = f"；{event.url}" if event.url else ""
        lines.append(
            f"- **{event.grade.value}**：{event.title}（{source_label}，{published}，证据范围：{scope}，"
            f"方法：{method}{url}）"
        )
        if event.grade_reason:
            lines.append(f"  - 依据：{event.grade_reason}")
        if event.fact_summary:
            lines.append(f"  - 事实摘要：{event.fact_summary}")
        for source in sources:
            lines.append(f"  - 核实来源：[{source.get('title', '原文')}]({source['url']})；发布日期：{source.get('published_at', '未知')}")
        if event.permissions.get("conditions"):
            lines.append("  - 待确认条件：" + event.permissions["conditions"])
        if event.permissions.get("impact_reason"):
            lines.append("  - 影响强度依据：" + event.permissions["impact_reason"])
    if pending:
        lines += ["", f"展示窗中另有 {len(pending)} 条发现记录未形成已核实方向评估；逐项检索与具体缺口保留在工作流/JSON中，不计作独立利多/利空。"]
        for event in pending[:5]:
            lines.append(f"- 待核实：[{event.title}]({event.url})；{event.permissions.get('workflow_reason', event.grade_reason)}")
    return lines


def render_report(bundle: ReportBundle) -> str:
    req = bundle.request
    manifest = bundle.run_manifest
    datasets = manifest.snapshot.get("datasets", {})
    versions = "；".join(f"{k}={v['version']}" for k, v in datasets.items()) if datasets else "无已登记数据版本"
    lines = [
        f"# 消息研究报告：{', '.join(s.ticker for s in bundle.securities) or ', '.join(req.tickers)}",
        "",
        f"- 市场：{req.market.value}；知识截止：{manifest.snapshot.get('knowledge_cutoff', '未记录')}；新闻观察窗：{req.news_lookback_days} 天",
        f"- 数据版本：{versions}",
        f"- 策略版本：{manifest.policy_version}；运行：{manifest.run_id}",
        "",
    ]
    lines += _macro_lines(bundle)
    lines.append("")
    lines += _news_lines(bundle)

    impact = bundle.news_impact_summary
    lines += ["", "## 综合摘要"]
    if impact.get("status") == "computed":
        lines.append(f"在{impact['asof']}知识截止下，已核实新闻按时间衰减和独立事件去重后的综合判断为{impact['classification']}，净贡献{impact['net_contribution']:+.3f}，共{impact['event_count']}个事件。")
    elif bundle.context_events:
        lines.append(f"观察窗内有 {len(bundle.context_events)} 条事件记录，但尚无已核实的影响评估；未核实标题不计作利多或利空。")
    else:
        lines.append("观察窗内没有合格事件证据；缺少证据不等于中性。")
    lines.append("新闻贡献描述当前证据方向，不是上涨概率；事件后的价格变化不证明因果关系或已充分定价。")
    lines.append("本报告只解读消息，不提供价格预测、买卖、仓位或执行指令。")

    lines += ["", "## 限制与复现"]
    for limitation in bundle.limitations:
        lines.append(f"- {limitation}")
    lines.append(f"- 运行清单：{manifest.run_id}；代码版本：{manifest.code_version or '未记录'}")
    lines.append(f"- 策略哈希：{manifest.snapshot.get('policy_hash', '未记录')}")
    for stage in manifest.stages:
        if stage.get("status") != "ok":
            lines.append(f"- 阶段 {stage.get('name')}：{stage.get('status')}（{stage.get('detail', '')}）")
    return "\n".join(lines).rstrip() + "\n"
