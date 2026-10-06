---
name: stock-data
description: Collect and analyze U.S. and Hong Kong stocks and ETFs through the stock-data gateway. Use for prices, fundamentals, valuation, technical and factor analysis, news and sentiment, event impact, screening, comparisons, and validated quantitative direction research.
metadata:
  version: "2.1.1"
---

# Stock Data & Quant Research

按用户问题收集股票 / ETF 数据并进行分析。数据采集统一经过本 Skill 的 `stock-data` gateway；涉及未来方向、收益概率或以技术、新闻、事件推断未来走势时，执行 **Quant → 验证/审计 → News/Sentiment → Final Synthesis**。Quant 包含 A/B/C 三条独立预测路径和 D 因子诊断路径，E 是共识之后的偏差审计阶段。普通事实查询和描述性分析只取必要字段，不强行生成方向预测。

## 适用范围

- 美股：Finnhub MCP 覆盖范围内优先使用 Finnhub；对应字段不足或失败时，只对缺项按 `references/` 规则回退到 global-stock-data。
- 港股及 Finnhub 未覆盖能力：使用 global-stock-data 已支持的来源。
- ETF：按上市市场走相同路由，以 ETF 自身 OHLCV 为研究输入；指数、行业和主要成分股只作有来源的背景。ETF 资料、持仓、NAV 等字段按实际能力取用，缺失时不以公司财务或指数价格代填。当前没有专用 ETF 穿透估值、跟踪误差或杠杆路径模型。
- 其他市场：说明当前路由覆盖边界，不推测来源。
- 方向研究：支持 `1D`、`5D`、`20D` 有效交易日，默认 `5D`。只有数据、标签和验证支持时才报告概率。

## 执行流程

### 任务路由与按需参考

先分清“描述已有事实”和“判断未来方向”；仅出现“走势”“新闻”或“技术指标”不代表必须预测。若回答将涉及未来涨跌或收益概率，无论用户从哪个维度切入，都转入“方向或概率研究”的完整流程，不得另建预测方法或用新闻替代 Quant。

| 用户任务 | 执行路径 | 按需阅读 |
|---|---|---|
| 当前/历史价格、公司或 ETF 基本资料 | 普通请求，最小数据查询 | [专项分析路径](references/analysis-paths.md#事实与描述性分析) |
| 基本面、财报、绝对/相对估值 | 普通请求，资料与财务证据、口径核对、同行比较 | [专项分析路径](references/analysis-paths.md#基本面与估值) |
| 历史走势、波动、MA/MACD/RSI 等指标 | 普通请求，历史行情与本地计算；未来判断转方向研究 | [专项分析路径](references/analysis-paths.md#历史走势与技术指标) |
| 新闻、评级、情绪、财报日历、内部人或事件影响 | 普通请求，事实/观点/行情反应分开；未来判断转方向研究 | [专项分析路径](references/analysis-paths.md#新闻情绪与专项事件) |
| 期权、资金流、空头成交量、SEC、宏观数据 | 普通请求，仅调用已支持能力；未来判断转方向研究 | [专项分析路径](references/analysis-paths.md#其他专项数据与筛选) |
| 筛选、多股票 / ETF 比较、综合或深度研究 | 按维度组合普通请求；涉及方向时每个标的分别执行方向研究 | [专项分析路径](references/analysis-paths.md#筛选比较与综合研究) |
| 未来走势、上涨概率、量化方向/回测研究 | 方向研究，A/B/C/D → 共识 → E 审计 → News → 综合 | [量化路径与运行产物](references/quant-paths.md) |

专项路径不代表新增预测器。代码执行映射和已实现边界见 `quant-paths.md`；不要仅凭上游方法名称宣称运行了完整上游框架。

### 普通数据请求

1. 明确 ticker、市场、日期范围、频率和字段。
2. 按“来源路由”选择实际可用的 Finnhub MCP 或 global-stock-data 工具。Finnhub 参数以当前会话工具 schema 为准；使用 global 前阅读 [运行与来源说明](references/global-stock-data.md) 和 [来源限制](references/source-policies.md)。
3. 逐项验证 envelope、字段、时间、单位、覆盖和来源；只对失败或缺失项回退，保留已有成功结果。
4. 分开报告观察事实、来源观点、计算值、缺失字段及实际来源时间。不得猜测空值。

### 股票 / ETF 方向或概率研究

1. 将请求标准化为 `ticker`、`market`、`horizon`、`asof` 和 `mode`。完整契约见 [researcher-contract.md](references/researcher-contract.md)。
2. 通过本 Skill 的 data gateway 完成一次数据采集。默认 Quant 主研究窗口为最新已确认收盘交易日前推 3 个日历年；用户明确指定其他区间时遵从用户范围。尽量取得该窗口的日线原始 OHLCV，并在同一次采集中请求可用的公司/宏观新闻、评级/分析师观点或情绪记录；记录源实际覆盖区间，不能把短新闻窗口伪装成完整三年档案。源返回更长历史时，完整响应保留在 raw envelope，规范化 `market.data.bars` 只包含本次研究窗口；不得因为某一路径样本不足而自动扩窗。不可用字段保留缺口；不要为某个研究器单独重新取数。
3. 按 [collection-adapter.md](references/collection-adapter.md) 保留工具响应，OHLCV 明确映射到日线 `bars`，记录 `market.data.history_window` 的实际起止日和选择规则，生成 request 和 raw-responses JSON；不要从字段名猜缺失含义。
4. 用 `adapt` 写成规范化 `collected.json`，保存原始响应 envelope、来源、来源时间、取数时间、单位、币种、fallback 和披露/发布时间。
5. 冻结一个带 provenance 和内容摘要的 Point-in-Time Snapshot。无时区的时间戳不得擅自解释为 UTC。未确认收盘的 as-of 当日 K 线会被排除；Snapshot 校验失败时停止方向结论。
6. A/B/C/D 各自只读取同一个 Snapshot，独立构造特征，不读取彼此输出；按 [quant-paths.md](references/quant-paths.md) 核对四条路径及 E 审计的执行映射，隔离和时间规则见 [architecture.md](references/architecture.md) 与 [point-in-time-policy.md](references/point-in-time-policy.md)。
7. 运行 Research A（假设与历史条件频率）、B（因子验证与去冗余）、C（逻辑回归与防泄漏验证）和 D（因子 IC/forward-return 诊断）。D 在单证券输入下只报告时间序列 IC 与历史 forward-return 分组，不产生概率；横截面 Rank IC、组合 NAV、基准和换手/成本回测需要多证券面板及匹配市场数据。路径失败时继续其余路径并降低结论等级。
8. 所有研究器完成后，只有 A/B/C 的有效预测概率进入共识；D 的诊断结果不投票、不加权。随后执行 E（Adversarial / Backtesting Bias Audit），检查声明的特征/标签时序、时间外验证、Brier 基准、候选因子多重检验，并明确标出 PBO、DSR、成本/冲击和幸存者偏差中因输入不足而未评估的部分。E 不产生第五份概率；出现 veto 时以运行结果为准抑制方向和可报告概率，不得使用 `pre_veto_prob_up` 绕过否决。规则见 [consensus-protocol.md](references/consensus-protocol.md) 与 [probability-policy.md](references/probability-policy.md)。
9. Quant 路径、共识和审计全部完成后，先将完整 Quant Result 写入 `quant_result.json` 并生成 SHA-256 冻结凭据。snapshot-only loader 随后构造 NewsInput；News 分析器只接收这个内存契约和可选衰减配置，没有 Quant 结果或目录路径。按 [news-analysis.md](references/news-analysis.md) 做 PIT 筛选、去重、事件/观点分离、事件分类、独立情绪筛查、时间衰减和快照行情反应验证。新闻不可用或没有可靠发布时间时必须返回 `not_assessed`/`partial`，不二次抓取。
10. 冻结独立 `news_result.json` 后，只有 Final Synthesis 可读取 Quant Result 和 News Result。只报告证据对齐、分歧、消息面风险和分类置信度；原 Quant P(up) 必须逐字数值保留，第一版禁止生成 News P(up) 或融合概率。保存 JSON 与 Markdown 报告。交付时分开说明数据面、消息面、交叉判断、风险、数据覆盖和验证限制。

最终自然语言回复按以下顺序组织：

1. **数据面**：Quant 方向、原始 P(up)、路径/样本验证和主要限制。
2. **消息面**：公司与宏观事件分别列出，说明发生了什么、可能影响公司的哪项现金流/风险/预期、正负或未知方向；每项依据时间戳、来源和去重后的标题。因果机制是待验证解释，不得写成已证明的股价原因。
3. **情绪面**：只总结明确标记的分析师/投资者/社交观点；把 provider aggregate 单列，指出窗口和来源，不与文章情绪分数混算。
4. **行情验证**：说明快照内发布后价格、跳空、成交量/波动率反应；日线只表示同一时段的价格反应，不能证明新闻导致价格变化。
5. **交叉判断**：给出对齐/分歧类别和最终分类置信度，列出冲突来源或关键风险。没有可用消息时直说“未评估”。

快照存于 `runtime/snapshots/<snapshot_id>/`，报告存于 `runtime/research/<analysis_id>/`。以下是仓库根目录下的命令；安装后把 `stock-data/scripts/quant_research.py` 替换为已安装 Skill 中脚本的实际绝对路径，输入和产物仍保存在用户可写工作目录：

```text
python stock-data/scripts/quant_research.py request --input request.json --output normalized-request.json
python stock-data/scripts/quant_research.py adapt --request normalized-request.json --responses gateway-responses.json --output collected.json
python stock-data/scripts/quant_research.py freeze --input collected.json --output-root runtime/snapshots
python stock-data/scripts/quant_research.py analyze --snapshot-dir runtime/snapshots/<snapshot_id> --output-root runtime/research
# optional: --news-policy path/to/news-decay-policy.json
```

一次 `analyze` 已自动执行 A/B/C/D、共识、E 审计、两份结果冻结、News/Sentiment 和 Final Synthesis；不要重复手工执行路径或把 A–E 理解为必须创建五个 Agent。逐项检查 `researchers/*.json`、`quant_result.json`、`news_result.json` 及两份 `.freeze.json`、`report.json` 和 `report.md`，不能仅凭 CLI 的 `status: success` 宣称所有路径有效。CLI 成功表示产物写出；路径有效性、审计 veto 和最终可报告方向由报告内字段决定。

`adapt`、`freeze` 和 `analyze` 只处理已采集数据，不连接外部供应商。当前 ML baseline 为确定性的 L2 Logistic Regression；其未实现 boosted-model 比较，数值结果仍标为 `partial`。未校准概率必须标记 raw/uncalibrated；样本不满足代码门槛时返回 `insufficient_data`，不得包装成完整模型验证。

## 数据源路由与回退

Finnhub 名称以当前 MCP 工具清单为准，详情见 [Finnhub runtime schema](references/finnhub-mcp-1.21.3-schema.md)。global 函数、参数和来源见 [运行与来源说明](references/global-stock-data.md)。

| 所需数据 | 美股 | 港股及覆盖边界 |
|---|---|---|
| 当前报价 | Finnhub `get-quote` → global 行情 | global 港股行情；保留行情时间 |
| 原始 OHLCV | Finnhub 可用 candles；需要更长日线或更细范围时由 gateway 补缺 | global Yahoo K 线；核对日期和频率 |
| 技术指标 | 优先提供原始 OHLCV，由研究路径各自计算 | 同左；单独要求技术指标时可调用本地函数 |
| 公司资料/财务/预期 | Finnhub 对应能力及 global 字段级回退 | global Yahoo/东财已支持字段 |
| 新闻/评级/日历/内部人 | Finnhub 有对应工具时优先，缺项再回退 | global 已支持能力；港股日历/内部人能力可能缺失 |
| SEC、期权、FINRA 空头成交量、资金流、Treasury、CFTC | global 对应函数并遵守授权和来源边界 | 本包未覆盖的能力应报告缺口 |

只有所需字段/范围/分辨率缺失时才升级视图或回退。Finnhub 的 Premium/403 不重试同一端点；429 时停止新增 Finnhub 调用。不得把失败响应改写为空列表，也不得用不匹配的周线、短新闻窗口或空头成交量代理成请求数据。

## 研究硬性约束

- 每次方向研究只冻结一个快照；Quant、News、情绪和市场反应验证使用相同 `snapshot_id`。
- 研究期间不得再调用数据 gateway。原始行情为共享基础；因子、信号和特征由各研究路径分别生成。
- News 只在 Quant 完成并冻结后运行，但它在产出自己的结果前看不到 Quant 方向、概率、因子、ML、回测或偏差审计。News 输入契约没有 Quant 字段。
- 新闻文章按 canonical event 聚类；重复转载不得计为独立事件。事实/事件记录与分析师、社交和其他观点记录分开处理，不能把观点当作已发生事实。
- 所有新闻发布时间和更新时间必须不晚于 snapshot `asof_timestamp`。市场反应只能使用同一 snapshot 已冻结的 OHLCV；不能为 News 再取行情。日线反应是时间关联，不证明因果。
- 第一版情绪/事件筛查为确定性启发式且未校准；缺失的来源质量、事件强度或其他分数保留 `null`。不允许 News 生成或调整 Quant P(up)。只有 Final Synthesis 可比较两份冻结结果。
- 对可读标题/摘要只能复述能由原文支持的事件事实。没有明确记录类型且无法匹配受支持事件的条目进入未分类，不得硬归入事件或观点。
- PIT 依据可得/发布/申报时间，不以财务期间结束日代替可用时间。无法证明可用时间的字段不进入严格历史验证。
- 新闻可在数据采集中保留为带发布时间的背景材料；当前 A/B/C/D 仍只使用 OHLCV。News 可分析短窗口内已验证的事件、观点和行情反应，但不能由此声称完成三年历史新闻事件研究或收益因果归因；缺乏 PIT 档案与可靠时间的历史动因标为未评估。
- 未来收益只能作为 label，严禁进入特征。目标 horizon 按有效交易 session，不按自然日。
- LLM 可以提出可证伪假设、解释计算结果和写报告，不能编造上涨概率、预期收益、IC、AUC、Brier 或回测指标。单证券 D 输出必须称为时间序列诊断，不能称为横截面 Rank IC 或投资组合回测。
- 任何确定的未来收益承诺、交易指令或个性化建议都不属于本 Skill。

## 失败与降级

- Snapshot 结构错误、未来数据或关键时间错位：标为 invalid 并停止方向结论。
- 数据不足、模型不可用或校准样本不足：概率返回 `null` 或明确标成 raw/uncalibrated；不补数字。
- 仅 1 条路径成功：报告 single-path result，不称为 ensemble consensus。
- 2/3 条路径成功：可以形成降级共识，但必须披露失败路径并降低置信度。
- 高 disagreement、低研究多样性、低质量/陈旧/回退数据：发出警告并降低最终 confidence。

## 详细参考

- [专项分析路径：基本面、技术、事件、ETF 与比较](references/analysis-paths.md)
- [量化 A/B/C/D 与 E 审计的代码映射、限制和产物](references/quant-paths.md)
- [架构与执行边界](references/architecture.md)
- [采集适配器输入契约](references/collection-adapter.md)
- [Snapshot Schema](references/snapshot-schema.md)
- [Researcher Output Contract](references/researcher-contract.md)
- [Point-in-Time Policy](references/point-in-time-policy.md)
- [Probability Policy](references/probability-policy.md)
- [Consensus 与 Adversarial Audit](references/consensus-protocol.md)
- [上游来源与许可记录](references/upstream-provenance.md)
- [News/Sentiment 与 Final Synthesis](references/news-analysis.md)

旧的 Finnhub schema 和 global 来源/口径细节仍以对应 reference 文件为准；source-material 是迁移档案，不覆盖本入口的运行规则。
