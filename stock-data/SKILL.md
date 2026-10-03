---
name: stock-data
description: Retrieve and analyze U.S. and Hong Kong stock quotes, price history, company financials, news, and market events. Use for stock research, comparisons, technical indicators, and data screening.
metadata:
  version: "1.1.0"
---

# Stock Data

按用户问题选取需要的数据。美股在已暴露 Finnhub MCP 的能力范围内优先使用 Finnhub；对应数据项失败或覆盖不足时，再使用 global-stock-data。港股和当前 Finnhub 未覆盖的能力直接使用 global。其他市场尚无完整路由，明确说明覆盖边界。

## 准备与调用

1. 确认市场、ticker、日期范围、粒度和所需字段；代码有歧义时先解析，不把输入错误当作数据源故障。
2. 检查当前会话的 Finnhub 工具与 schema。参数基线见 [Finnhub schema](references/finnhub-mcp-1.21.3-schema.md)，当前 schema 优先。Finnhub 仅通过 MCP 调用；工具不可见时直接尝试 global，不声称已查询 Finnhub。
3. 使用 global 前读取 [运行与来源说明](references/global-stock-data.md) 和 [来源限制](references/source-policies.md)，执行随包提供的 `scripts/global_stock_data.py`。需要 Python 3.10+、requests 和允许访问来源的网络；环境不可用时说明限制，不伪称已完成回退。

## 来源路由

以下 Finnhub 名称为 server-native 名称，实际调用名称取当前工具清单。global 函数、参数及来源见运行说明。

| 所需数据 | 美股路由 | 港股路由及边界 |
|---|---|---|
| 当前报价 | `get-quote` → global 行情 | global 港股行情；保留行情时间 |
| 区间价格、K 线 | `get-price-summary` → global K 线 | Yahoo K 线；须核对日期与粒度 |
| 技术指标 | Finnhub 足量原始 candles → global K 线；本地计算 | Yahoo K 线后本地计算；标为派生值 |
| 公司资料、关键指标 | `get-company-profile` / `get-financials-snapshot` → Yahoo / 东财 | Yahoo / 东财；按字段补缺 |
| 财报三表 | global 东财 / Yahoo 财报函数 | global 东财 / Yahoo；SEC facts 只是结构化指标 |
| 新闻 | `get-news-pulse` → Yahoo 新闻搜索 | Yahoo 新闻搜索；不承诺完整历史或情绪分 |
| 分析师评级 | `get-recommendations` → Yahoo `analyst_estimates` 中已有评级 | Yahoo；空值如实报告 |
| EPS / 营收预期 | global `analyst_estimates` | Yahoo；Finnhub 评级工具无此字段 |
| 财报日历 | `get-calendar(kind=earnings)` → Nasdaq 单日日历 | 本包没有等价港股日历 |
| 内部人活动 | `get-insider-signal`；缺失时可提供 SEC Form 4 记录 | 本包没有等价信号；申报记录不等于净买卖信号 |
| 同行列表 | `get-peers`；global 无等价发现函数 | 无等价发现函数；可比较用户给定标的 |
| 代码搜索 | `search-symbol` → global `stock_search` | 直接 global `stock_search` |
| 期权、SEC、空头成交量 | global 对应美股函数 | 本包无等价港股能力 |
| 资金流、市场列表及筛选 | global 对应函数 | global 对应港股函数 |

宏观收益率曲线、CFTC COT 和 SEC 横截面直接使用 global 对应函数。Finnhub 交易所工具返回的有限样本不能当作完整股票池。

## 成功检查与逐项回退

- 先检查外层 MCP 错误与 envelope 的 `is_success`、错误类型、实际 `data`，再确认字段、日期范围、分辨率和列表完整性。`premium=true` 单独出现不代表全部数据失败；保留已经成功的部分。
- 先排除视图裁剪：profile 的联系方式用 standard/full，snapshot 的 raw 与原始 candles 用 full；完整新闻、日历或同行列表用 full。只在所需视图能提供字段时升级一次，不因可选空值机械重查。
- 视图升级仍缺用户必需字段、范围或粒度，或工具不可用、请求失败、数据为空时，只回退对应缺项。新闻固定 7 天、`1y` 价格摘要为周线；不能将它们当作任意日期新闻或一年日线。
- 瞬时网络或服务错误可重试一次。Premium/403 不重试该端点；429 停止本轮新增 Finnhub 请求，使用已取数据或 global，并报告返回的限额/重置时间（若有）。
- Finnhub 已满足请求时复用结果，不自动交叉取数。用户要求交叉核对时分别标注来源。global 的回退也仅选择已有、适用且允许使用的来源；缺少等价能力时报告缺口。
- 空列表只说明本次查询未返回记录；旧时间戳结合其口径说明，不单凭本地时间判定数据失败。SEC Form 4 补充须按目标 CIK 筛选，不拿全市场申报流替代个股信号。

## 数据与回答

- 每个数据项标注实际来源、行情时间或财务期间、币种及单位；取数时间与来源时间分开。源未提供时间/单位时说明未提供，不猜测。新浪美股报价的 `timestamp` 是未带时区的来源字符串；不转换为 UTC，也不单凭它称为实时。需要判断最新交易日时用日K或另一报价源核对，发现日期不一致时单独披露。
- 缺失值保留为不可用，真实零值保持为零。计算指标前核对必需 OHLCV、排序、粒度和足够长度；不要用缺失价格或混合周期计算。
- XBRL 保留 start/end、单位、申报时间和编号，区分季度、累计、年度与重述。报价、复权口径和财务单位不能混用。
- 发生回退时简述原因与补充来源。事实、计算值和来源中的分析师观点分别呈现；可以引用来源目标价及其日期/币种，不自行生成目标价、交易指令或确定性预测。
- SEC 联系信息由环境配置，不在回答或错误中回显；CBOE 必须已有授权配置。详细约束见来源限制。
