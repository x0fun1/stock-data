---
name: stock-data
description: "Retrieve and analyze U.S. and Hong Kong stock data. Prefer Finnhub MCP for supported U.S. data and fall back to global-stock-data when that request fails or lacks required fields."
version: 1.0.0
---

# Stock Data

按用户问题选取最少的数据。美股优先使用 Finnhub MCP；对应调用失败或缺少所需字段时，再从 global-stock-data 取同类数据。港股以及 Finnhub 当前未覆盖的能力直接使用 global-stock-data。逐项回退，分别标注来源、时间和期间。

## 何时使用

- 查询美股或港股价格、走势、公司资料、基本面、新闻、分析师观点或事件。
- 查询 K 线、技术指标、期权、资金流、SEC 文件、空头成交量或全市场筛选。
- 比较股票或根据指定指标筛选股票。

## 运行前检查

- 只在当前会话实际暴露 Finnhub MCP 工具时调用 Finnhub。按当前工具清单和 schema 使用名称及参数；不要假设某个 host 一定采用 Hermes 别名。
- Finnhub 数据只能通过 MCP 获取。禁止改用 Finnhub REST API、浏览器请求或自行实现客户端。
- Finnhub 工具不可见时，将 Finnhub 标记为当前不可用，并按对应数据项尝试 global-stock-data；不得声称已查询 Finnhub。
- global-stock-data 是 Python 代码型 skill，依赖 requests 和可用的本地执行环境。按需读取 [global-stock-data](references/global-stock-data.md) 中的共用 helper 与目标数据层；不要在未获请求时安装依赖。执行环境或网络策略不允许调用时，说明限制，不要伪称已完成回退。
- Finnhub 运行时 schema 细节见 [Finnhub MCP schema](references/finnhub-mcp-1.21.3-schema.md)。该文档是版本基线；如当前工具清单不同，以当前 schema 为准。

## 来源路由

先判断市场、标的、时间范围和所需字段。明确 ticker 时直接使用；名称或代码有歧义时先解析，不把无效或不明确的标的误报成数据源故障。

| 用户所需数据 | 首选与回退 | 覆盖边界 |
|---|---|---|
| 美股当前报价 | Finnhub get-quote → global 的美股行情函数，如 us_stock_quote_sina、us_stock_quote_tencent | 价格必须附各自返回的时间戳；不得把历史收盘价称为当前报价。 |
| 美股区间走势、收益或 K 线 | Finnhub get-price-summary；需要原始 candles 时用 view=full → global 的新浪/Yahoo K 线，按请求计算区间指标 | 两种摘要口径不同时保留原始期间、分辨率和来源；不把最新收盘冒充实时价。 |
| 美股公司资料和关键指标 | 按意图调用 Finnhub get-company-profile、get-financials-snapshot → global 的 stock_search、yahoo_quote_summary(assetProfile) 或 key_statistics | global 字段不完全等价。只补缺失字段，明确期间、单位及空值，不推算缺失 KPI。 |
| 美股完整财报三表 | 直接使用 global 财报函数或 SEC XBRL | 当前 Finnhub 工具集没有等价的完整三表端点。 |
| 美股新闻 | Finnhub get-news-pulse → global stock_news | global 新闻列表不提供等价的 Finnhub 情绪分；不能据此补造情绪数值。 |
| 美股分析师评级/预期 | Finnhub get-recommendations → global analyst_estimates | period 和字段定义可能不同；分别注明来源及报告期间。 |
| 美股财报日历 | Finnhub get-calendar(kind=earnings) → global earnings_calendar(date=...) | global 函数按单日查询；若不能覆盖用户要求的日期范围，报告覆盖边界，不暗示已查完整范围。 |
| 美股内部人活动 | Finnhub get-insider-signal → global SEC Form 4 / daily_filings | Form 4 申报列表不是净买卖信号。仅在用户接受申报记录这一较窄数据时作为补充，不可冒充等价替代。 |
| 同行列表 | Finnhub get-peers；失败时 global 没有等价的同行发现能力 | 可以比较用户明确给出的股票；没有同行清单时如实说明。 |
| 股票搜索/代码解析 | Finnhub search-symbol → global stock_search | 只在标的含糊时搜索；匹配仍不明确则询问用户。 |
| 港股报价、基本面或 K 线 | 直接使用 global 对应港股函数 | Finnhub 本 skill 的首选路由仅用于其支持的美股数据。 |
| 技术指标 | Finnhub get-price-summary(view=full) 提供足够 OHLCV candles 时本地计算并标注为派生值；失败或 candles 不足时用 global K 线与指标函数 | 只在 candles 粒度、长度和字段满足指标计算条件时使用 Finnhub；不要从走势图估算。 |
| 期权、资金流、SEC filing、空头数据、全市场筛选 | 直接使用 global 对应层 | Finnhub 当前 schema 没有相同能力时，不做无意义调用。若能力是否存在不确定，先查当前工具清单；必要时用一次 search-tools 确认。 |

### Finnhub 内部的最小调用

- 当前报价：get-quote。
- 价格区间：get-price-summary，按用户期间选择 7d、30d、90d 或 1y；未指定时用 30d。
- 公司/基本面问题：只调用回答问题所需的 profile 或 snapshot；完整财报表格走 global 对应能力。
- 新闻、评级、财报日历、内部人活动：分别调用 get-news-pulse、get-recommendations、get-calendar、get-insider-signal。
- 同一轮内复用已取得且期间匹配的数据；不要为“完整”机械调用所有工具。

## 回退规则

对每个 Finnhub 调用先检查外层 MCP 错误状态和返回 envelope，再检查 is_success、error_type、premium、rate_limit 及实际 data。空的成功 envelope 不算数据成功。

- 工具不可见、MCP 调用错误、is_success=false、Premium/403、429、无可用数据，或用户必需字段为 null/缺失时，针对该数据项回退到 global 中确实支持的同类能力。
- 瞬时网络/服务错误可用同一工具重试一次；仍失败后回退。遇到 429 或 Premium/403 立即停止该 Finnhub 端点的重试；若有 remaining / reset_at，在回答中报告。
- 一部分字段成功、一部分缺失时只回退缺失字段；保留已成功的 Finnhub 字段，并分开标注两个来源。
- Finnhub 已成功返回所需数据时，不为交叉验证而自动再查 global；仅在用户明确要求交叉核对时另取数据并独立标注。
- 字段缺失不允许从其他字段、模型记忆或新闻标题补造。global 无同类能力或覆盖不足时，明确报告缺失或覆盖范围。
- 单凭时间戳较旧不判定 Finnhub 失败。若市场已收盘或返回的是最后可用快照，说明时间；不要称为实时。不要仅凭本地时钟推断市场开闭状态。
- 429 后停止本轮额外 Finnhub 请求。遇到 ticker 歧义或用户参数不合法时先处理输入，不要通过换源掩盖输入问题。

## global-stock-data 来源限制

- 遵守 global 文档中的来源分级、限速、条款和适用市场说明。自动回退只选该用户场景下允许使用的源；来源若要求事先授权，仅在已配置授权时使用。尤其不能默认把 CBOE 数据端点作为无条件回退。
- SEC 请求必须配置符合要求的真实 SEC_CONTACT；若仍是示例值或配置无效，停止 SEC 调用并报告配置问题。不要在回答中回显联系方式。
- global 函数中的 0、空数组或占位符可能表示字段不可用；结合函数说明核验后再展示，不能一概当成真实零值。
- global 数据源间的回退遵循其文档内路由；报告最终实际提供数据的源，不把数据归给中间路由或 Finnhub。

## 回答格式与核验

- 事实与解释分开。市场数据标注来源工具/数据源、返回时间戳或财务期间、币种和单位；源未提供 as-of 时说明未提供。
- 不混合来源后只写一个笼统来源。Finnhub 成功而 global 只补部分字段时，在相应字段或表格行分别注明来源。
- 发生回退时简要说明 Finnhub 失败类型，以及哪些字段由 global 的哪个来源补充。
- 用户请求的字段未返回时标为不可用；不因日历为空就断言没有事件，不因内部人记录就预测股价。
- 只总结实际查询的数据。不要提供买入/卖出/持有建议、目标价、仓位或确定性预测。涉及投资决策的回答附上：*This is informational research, not investment advice. Data may be delayed, incomplete, or wrong. You are responsible for your own decisions.*

## 参考材料

- 参数、字段、单位与 Finnhub envelope：[Finnhub MCP schema](references/finnhub-mcp-1.21.3-schema.md)
- global 数据层代码、helper、来源规则：[global-stock-data](references/global-stock-data.md)
- 迁移前的 Finnhub 路由说明，仅用于追溯：[原 Finnhub skill](references/source-material/finnhub-stock-analysis.md)
