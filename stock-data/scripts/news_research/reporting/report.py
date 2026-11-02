"""Render only frozen, validated evidence and bind the actual reply to it."""

from __future__ import annotations

from urllib.parse import quote

from ..common.security import markdown_text, safe_url

DIRECTION_LABELS = {"bullish": "利多", "bearish": "利空", "neutral": "中性", None: "证据不足"}


def render(snapshot: dict, result: dict) -> str:
    request = snapshot["request"]
    events = {item["event_id"]: item for item in snapshot["events"]}
    evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
    records = {item["record_id"]: item for item in snapshot["records"]}
    assessments = {item["event_id"]: item for item in result["assessments"]}
    exposures = {item["exposure_id"]: item for item in snapshot["context"]["exposures"]}
    used = set()

    def citations(ids: list[str]) -> str:
        used.update(ids)
        return " ".join(f"[{identity}]" for identity in ids)

    def event_refs(ids: list[str]) -> str:
        return citations(sorted({ref for identity in ids for ref in assessments[identity]["evidence_ids"]}))

    def clean(value) -> str:
        return markdown_text(value)

    synthesis = result["synthesis"]
    coverage = snapshot["coverage_summary"]
    selected = result["selection"]["selected_event_id"]
    conclusion = ("证据不足（分析未完成）" if synthesis["status"] == "not_assessed"
                  else DIRECTION_LABELS[synthesis["direction"]])
    lines = [f"# {clean(request['symbol'])} 新闻消息面研究", "",
             f"市场：{clean(request['market'])}；资产类型：{clean(request['asset_type'])}；币种：{clean(request['currency'])}",
             f"分析截止时间：{clean(request['as_of'])}",
             f"请求窗口：({clean(coverage['requested_start'])}, {clean(coverage['requested_end'])}]；{request['lookback_days']} 个自然日",
             "说明：标注“事实摘录”的内容来自登记证据；传导、规模、重要性、综合理由和条件均为分析判断。", "",
             f"## 综合结论：{conclusion}",
             f"分析状态：{clean(synthesis['status'])}",
             f"主导原因（分析判断）：{clean(synthesis['reason'])} {event_refs(synthesis['primary_event_ids'] + synthesis['background_event_ids'])}", "",
             "## 近期最重要的一条重大事件"]
    if selected:
        item = assessments[selected]
        event = events[selected]
        lines.extend([f"{clean(event['title'])} — {DIRECTION_LABELS[item['direction']]}",
                      f"selected_event_id：{selected}", f"首次报道：{clean(event['first_reported_at'])}；实质更新：{clean(event['substantive_updated_at'] or '无')}",
                      f"发生日期：{clean(event['occurred_at'] or '未提供')}；生效日期：{clean(event['effective_at'] or '未提供')}",
                      f"证据原文：{clean(item['fact_summary'])} {citations(item['evidence_ids'])}",
                      f"影响解释（分析判断）：{clean(item['mechanism'])}",
                      f"为什么最重要：{clean(result['selection']['reason'])}",
                      f"定性比较依据：{clean(result['selection']['comparison'])}",
                      "选择范围：最近 7 个自然日。" if result["selection_window_days"] == 7 else "选择范围：最近 7 天无合格事件，已扩大至最近 30 个自然日；原始日期不变。"])
        if item["direction"] != synthesis["direction"]:
            lines.append("近期第一名与综合方向不同；综合理由及持续背景见上文，不将近期事件机械等同整体结论。")
    else:
        lines.extend(["未发现可确认的重大方向性事件（selected_event_id：null）",
                      f"原因：{clean(result['selection']['reason'])}", f"比较/限制：{clean(result['selection']['comparison'])}",
                      f"选择不确定：{'是' if result['selection']['uncertain'] else '否'}"])
    for title, categories in (("宏观背景与行业传导", {"macro", "industry"}), ("公司消息与持续背景", {"company"})):
        lines.extend(["", f"## {title}"])
        found = False
        for identity, item in assessments.items():
            event = events[identity]
            if event["category"] not in categories:
                continue
            found = True
            lines.extend([f"### {clean(event['title'])}",
                          f"方向：{DIRECTION_LABELS[item['direction']]}；重要性：{clean(item['importance'])}；证据状态：{clean(item['evidence_status'])}",
                          f"首次报道：{clean(event['first_reported_at'])}；实质更新：{clean(event['substantive_updated_at'] or '无')}；状态：{clean(event['status'])}",
                          f"事实摘录：{clean(item['fact_summary'])} {citations(item['evidence_ids'])}",
                          f"传导分析：{clean(item['mechanism'])}",
                          f"规模与重要性依据：{clean(item['impact_scale'])}；{clean(item['importance_reason'])}",
                          f"持续性/影响期限：{clean(item['persistence'])}；{clean(item['time_horizon'])}",
                          f"新信息：{clean(item['new_information'])}",
                          f"相反因素：{clean(item['counterargument'])} {citations(item['counter_evidence_ids'])}",
                          f"未确定条件：{clean(item['uncertainty'])}"])
            if event["background"]:
                lines.append("此事件属于持续/较早背景，不作为新消息重置日期。")
            if event["future_event"]:
                lines.append(f"待发生事项：{clean(event['scheduled_at'])}；不得描述为已经发生。")
            if event["conflicts"]:
                lines.append(f"更正/冲突历史：{clean(', '.join(event['conflicts']))}")
            for exposure_id in item["exposure_ids"]:
                exposure = exposures[exposure_id]
                lines.append(f"目标实际暴露：{clean(exposure['description'])}；生效/更新时间：{clean(exposure['effective_at'])} / {clean(exposure['updated_at'])} {citations(exposure['evidence_ids'])}")
        if not found:
            lines.append("未完成该领域的有证据分析；不是中性判断。")
    lines.extend(["", "## 财务背景"])
    lines.extend(f"- {clean(fact['metric'])}：{clean(fact['value'])} {clean(fact['unit'])} {clean(fact['currency'])}；报告期末 {clean(fact['period_end'])}；披露 {clean(fact['disclosed_at'])}；accession {clean(fact.get('accession') or '未提供')}；{clean(fact['accounting_basis'])} / {clean(fact['period_kind'])} / {clean(fact['basis'])} {citations(fact['evidence_ids'])}"
                 for fact in snapshot["context"]["financial_facts"])
    if not snapshot["context"]["financial_facts"]:
        lines.append("无通过口径与来源校验的财务事实；不补造财务数字或估值。")
    lines.extend(["", "## 相反证据", f"{clean(synthesis['counterargument'])} {citations(synthesis['counter_evidence_ids'])}",
                  f"为何仍维持判断：{clean(synthesis['why_not_counter'])}", "", "## 改变判断的条件 / 待观察事项"])
    lines.extend(f"- {clean(condition)}" for condition in synthesis["change_conditions"])
    lines.extend(["", "## 覆盖与限制", f"窗口覆盖：{coverage['window_status']}", clean(coverage["scope_note"])])
    if coverage["below_minimum_target"]:
        lines.append("**显著限制：至少一个配置来源未证明覆盖最低 30 天目标；不能称为完成目标窗口研究。**")
    for source in coverage["sources"]:
        lines.append(f"- {clean(source['source_id'])} / {clean(source.get('entity') or '未提供实体/主题')}：采集 {source['collection_status']}；窗口 {source['window_status']}；可核实覆盖 {source['covered_days']} 天；取回 {source['fetched_count']} 条。{clean('; '.join(source['reasons']))}")
        lines.extend(f"  - 成功范围：({clean(interval['start'])}, {clean(interval['end'])}]" for interval in source["successful_intervals"])
        lines.extend(f"  - 已查询但完整性未证明：({clean(interval['start'])}, {clean(interval['end'])}]" for interval in source.get("queried_intervals", []))
    counts = result["analysis_counts"]
    lines.append(f"候选事件 {counts['candidates']}；已分析 {counts['assessed']}；遗漏/待分析 {counts['omitted']}。")
    if result["unassessed_event_ids"]:
        lines.append("未分析队列：" + clean(", ".join(result["unassessed_event_ids"])))
    lines.append(f"时点/来源排除记录 {len(snapshot['excluded_records'])}；无可保存正文记录 {sum(not item['text'] for item in snapshot['records'])}。")
    lines.extend(f"- 排除 {clean(excluded.get('record_id', excluded.get('record_index', 'unknown')))}：{clean(excluded['reason'])}"
                 for excluded in snapshot["excluded_records"])
    lines.extend(f"- {clean(gap['domain'])}：{clean(gap['reason'])}" for gap in snapshot["context"]["gaps"])
    lines.extend(f"- {clean(limitation)}" for limitation in snapshot["limitations"])
    if coverage["missing_domains"]:
        lines.append("未完整取得来源回执的分域：" + clean(", ".join(coverage["missing_domains"])))
    lines.extend(["", "## 可追溯来源"])
    for identity in sorted(used):
        ev = evidence[identity]
        record = records[ev["record_id"]]
        url = safe_url(record["url"])
        link = f"[原文](<{quote(url, safe=':/?=&%[]@+;,~-._') }>)" if url else "无有效原文链接"
        lines.append(f"- [{identity}] {clean(record['title'])}；{clean(record['source_id'])}；类型 {clean(record['source_kind'])}；发布 {clean(record['published_at'])}；更新 {clean(record['updated_at'] or '未提供')}；取回 {clean(record['fetched_at'])}；文本 {clean(record['text_scope'])}；定位 {clean(ev['locator'])}；{link}")
    lines.extend(["", "本报告仅解释截止时点已知消息的影响，不保证未来价格方向，也不构成收益承诺。", ""])
    return "\n".join(lines)


def validate_reply(response: str, expected: str) -> dict:
    if response.replace("\r\n", "\n").strip() != expected.replace("\r\n", "\n").strip():
        raise ValueError("reply differs from the validated report; use the generated report without adding unsupported claims")
    return {"valid": True, "citation_check": "passed", "content_check": "exact frozen report"}
