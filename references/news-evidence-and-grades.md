# 新闻证据与七档结论

实现：`acquisition/news_collector.py`、`control/news_workflow.py`、`context/news_impact.py`、`control/news_reviews.py`。独立程序批量调用Search/Fetch服务并保存分析前证据；宿主大语言模型随后逐项判断。CLI持久化队列、验证记录、去重和时间衰减，采集阶段不调用大语言模型。

## 采集与分析分开

`acquisition/news_collector.py`提供独立自动采集。先`news-workflow --step prepare`，再`news-collect --workflow-id ... --collection-id ... --provider tinyfish`，程序按原标题顺序执行Search、候选URL的Fetch及公司/事件/日期回退，写入`collections/.../items/`、`collection.json`和`evidence-table.md`。这里不调用大语言模型、不读取已有情绪判断、不输出利多利空。未完成项保留访问缺口，断点续跑不重新抓取已封存项；协议或已完成证据改变须用新collection ID。

采集使用TinyFish同一服务的Search/Fetch REST API，鉴权只读取本地`TINYFISH_API_KEY`，不依赖大语言模型逐条调用插件。插件连接和独立API鉴权是不同连接方式，不能因插件目录可用就声称已配置鉴权。Search/Fetch可在官方免费额度内使用；代码不会自动充值，401/402停止批次。当前实现没有调用付费Browser/Agent端点。接口依据：[Search参考](https://docs.tinyfish.ai/search-api/reference)、[Fetch参考](https://docs.tinyfish.ai/fetch-api/reference)。

已连接插件时采用`news-plugin-plan`→[固定JS驱动](tinyfish-plugin-batch.md)→`news-collect --provider tinyfish-plugin --plugin-batch-id ...`。宿主程序绑定已授权的search/fetch_content方法，整个循环由程序执行一次；无须另配API Key。导入要求批次完整、鉴权响应已确认、标题和任务ID一致；无效调试批次不得作证据。Python导入阶段没有网络调用，真正联网发生在前面的插件驱动。日期保留原始值，ISO偏移和RFC/GMT显式时区可规范化，日期或无时区时间不擅自当UTC。

表格保存最多200词正文截段、来源URL、元数据和完整内容哈希，明确是否截断；不把搜索摘要混成已抓取正文。大语言模型在采集完成后读取表格/JSON，逐项核对事实、主题、公司、日期和条件，采用`source_excerpt`范围。抓取成功只表示可供审核，并不等于语义已验证。截段不足时另建下一轮自动采集任务；不要把不足内容升级成完整正文。

`--provider archive`只重放旧搜索/阅读记录，不作新的标题/主题查询、不调用网络，也不创造不存在的正文；输出明确`is_live_provider_run=false`。原来的宿主逐标题搜索仅属于之前验收的执行方式，不再作为本轮要求的自动采集实现。

## 证据链

1. 发现记录：来源、URL、标题、时间、实体。
2. 许可检查：用途、存储、全文/衍生结果限制。
3. 规范化：实体、事件时间、首次可得时间。
4. 去重：URL、正文哈希、事件聚类三级。
5. 事实提取：只写已证实事实与仍待确认条件。
6. 分级：七档或“证据不足”。
7. 引用：来源、证据定位、可知时间。

转载十次不是十条独立利好。更正与后续进展保留独立版本，不覆盖旧状态。

## 证据范围

| 范围 | 可写 | 不可写 |
|---|---|---|
| 标题/摘要 | 标题级事件、来源、链接 | 已核对公告正文、完整因果 |
| 官方摘录 | 官方事实摘要 | 超出摘录的推断 |
| 原文相关段落 | 已实际阅读段落支撑的事实 | 声称已读完整付费正文 |
| 全文 | 详细事实、条件 | 未验证的因果或收益 |
| 数值 | 原始数值 | 未核对口径的衍生比率 |

只有标题、目录、索引或日历行时，默认 `证据不足`，不升级为强结论。SEC 8-K 主文件是封面页，事件正文通常在 EX-99.1 等附件；需要单独授权导入。

## 七档方向

| 等级 | 含义 |
|---|---|
| 极大利好 | 重大、明确、范围广的正向事实 |
| 高度利好 | 明确正向，影响较大但非极端 |
| 中度利好 | 有限正向或条件性正向 |
| 中性 | 无方向或信息不足以外的事实性更新 |
| 中度利空 | 有限负向 |
| 高度利空 | 明确负向，影响较大 |
| 极大利空 | 重大、明确、范围广的负向事实 |
| 证据不足 | 无法达到最低证据范围或事实不足 |

等级是文本结论，不是预设收益、数值权重或上涨概率。`grade_event` 的 lexicon 是可解释的占位分类器，只用于带正文但未经逐项审核的导入记录。

Agent核实的`source_review`不采用标题关键词自动定级：导入必须包含事实摘要、判断理由、原文URL和定位、来源发布日期与实际核实时间。只保存事实摘要和引用，全文不进入Skill分发包。报告优先显示这些事件，并保留未核实标题的待办记录。发布日期和核实日期不同；过去截止的报告不能读取后来才建立的核实结果。

## 自动逐项审核

1. `news-workflow --step prepare --workflow-id ...`冻结原标题任务清单，既支持 Finnhub，也支持用户导入的待核实消息。港股使用 `--market HK`；只审核已授权导入的公告，不新增 HKEXnews 自动抓取。`--step next --limit 8`返回待办，标题稳定ID用于关联所有检索与审核记录。
2. 每个原标题单独精确检索；无匹配正文时改用公司＋事件＋日期搜索，优先公司IR、监管公告或报道的原始来源。保存查询、工具引用/URL和时间。取得HTTP正文只代表读取成功；agent仍须检查主题、公司、日期和段落是否匹配，拒绝目录页、行情页、标题复刻和广告中的“下一家Nvidia”。
3. 实际阅读正文或相关原始段落。记录URL、发布日期、阅读时间、证据定位及内容哈希。只把已读范围标为`full_text`、`source_excerpt`或`official_excerpt`；不能把搜索摘要升级成原文。
4. 每条给出`verified`、`unrelated`或`unresolved`，附具体理由和`search_attempts`。无关联包含其他公司财报或仅标签/广告提及NVDA；无法核实保留失败来源与具体缺口，不进入方向贡献。
5. `verified.review`含`title/security_ids/event_cluster/fact_summary/grade/grade_reason/impact_reason/reviewed_at/evidence_scope/sources`及下面的影响字段。七档方向与符号必须一致；影响大小单独判断，不从乐观语气自动推定重大。区分商谈、签约、实施和收入实现；媒体指控与官方事实分开。
6. `--step apply --file assessments.json`导入，继续直到`pending=0`，再`--step finalize`。重新审核保留旧记录；撤回已核实结论时追加有日期的撤回事件。报告保存完整审核覆盖与任务状态哈希，展示条数限制不限制综合计算。

批量文件是JSON数组，每项结构如下（来源记录由实际阅读填入）：

```json
{
  "task_id": "原标题任务ID",
  "status": "verified",
  "reason": "具体证据与结论",
  "search_attempts": [{"kind":"search_MCP","query":"原标题","record":"检索记录"}, {"kind":"article_read","record":"原文证据记录"}],
  "review": {
    "title":"已核对事件", "security_ids":["US:XNAS:NVDA"], "event_cluster":"独立事件ID",
    "fact_summary":"事实与仍待确认条件", "grade":"中度利好", "grade_reason":"方向理由", "impact_reason":"强度理由",
    "reviewed_at":"2026-11-07T12:00:00Z", "evidence_scope":"source_excerpt",
    "sources":[{"url":"https://example.com/announcement", "published_at":"2026-11-05T12:00:00Z", "evidence_locator":"原文段落及内容哈希"}],
    "direction_sign":1, "impact_level":"medium", "event_kind":"partnership", "event_time":"2026-11-05T12:00:00Z",
    "stock_relevance":1.0, "evidence_confidence":0.8
  }
}
```

## 强度、衰减与综合

贡献=`direction_sign × impact_weight × evidence_confidence × stock_relevance × 2^(-age_days/half_life_days)`。符号为−1/0/+1；轻微、中等、重大默认权重1/2/4。可信度和关联度为0–1。重大潜在事件可因尚在商谈而降低可信度；中性只用于已核实但无方向或多空抵消的事件。

默认半衰期：财报/机构交易3天，价格变动/评论2天，监管/合作14天，产能45天，日历/其他7天。`event_time`取最初实质信息公开时间，不能用转载日、核实日或未来实施日刷新。仅知日期时使用明确保守时区边界并说明。新增实质进展另建事件；同一事件取最可靠来源，旧消息时间不刷新。

汇总全部窗口内已核实独立事件的正、负贡献和净贡献；净/总绝对贡献超过±0.15默认分别为利多/利空，其余为中性/抵消。配置和实际哈希进入报告。上述强度、可信度、半衰期及阈值是可审计研究参数，未证明实际市场冲击或未来收益。

全部源标题、查询、证据元数据和版本在测试阶段保留，`cleanup_enabled=false`。事件去重只避免重复计分。Skill安装包不携带运行数据。

## 去重

`canonical_url` 移除 utm/gclid 等追踪参数；`body_hash` 对空白和大小写规范化；`event_cluster` 用于同一事件多来源。重复事件在 `permissions.duplicate_of` 记录主事件 ID。

## 边界

- 新闻综合判断是研究分数，不是上涨概率；本 Skill 不含价格模型。
- 报告不计算事件后的价格反应；价格变化本身也不证明因果或已充分定价。
- 港股公告无合格证据时保留缺口，不新增 HKEX 自动采集，不反复索取无法提供的授权材料。
