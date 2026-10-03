---
name: finnhub-stock-analysis
description: "Use when analyzing U.S. stocks via the Finnhub MCP."
version: 0.1.0
author: "xo, Hermes Agent"
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, equities, Finnhub, quotes, research]
    related_skills: [global-stock-data]
---

# Finnhub Stock Analysis Skill

使用已配置的 `finnhub` MCP 查询美股行情、公司资料、基本面、新闻和事件，并把返回值整理为有时效标记的研究摘要。本 Skill 只通过 Finnhub MCP 取 Finnhub 数据；不调用 Finnhub REST API、不读取或管理 API key、不把模型推断伪装成数据。

创建时按运行中的 `finnhub-mcp@1.21.3` 检查了 `tools/list`。这是 schema 基线，不是永久版本限制；每次 MCP 升级或名称异常时，重新检查当前工具列表和 schema。

## When to Use

- 用户查询美股当前行情、公司资料、基本面、近期新闻、分析师观点、内部人活动、同行或日历事件。
- 用户要求单股分析、深度研究或多股票比较。
- 用户询问 Finnhub MCP 是否支持某种股票数据或指标。

不要用它替代港股专用数据流程；不要假设 Finnhub MCP 暴露了未列出的功能。

## Prerequisites

- 必须能看到并调用 Hermes 中已配置的 `finnhub` MCP。
- API key 由 Hermes 的 `FINNHUB_API_KEY` 环境变量配置；本 Skill 不读取、回显、保存、修改或索取该密钥。
- 所有 Finnhub 数据都经 MCP 工具返回。禁止用 `curl`、`requests`、浏览器或其他方式直接请求 Finnhub REST API。
- 当前 MCP 的规范工具名带连字符；Hermes 工具注册名形式为 `mcp__finnhub__<tool>`，并把工具名中的连字符转换为下划线，例如 `get-quote` → `mcp__finnhub__get_quote`。以当前会话实际工具清单为准，不猜测别的 namespace。

## How to Run

直接调用已注册的 Finnhub MCP 工具，不要启动服务器、安装包、拼接命令行或自行实现 API 客户端。若工具暂不可见，先确认当前会话是否已加载 MCP；可通过 `/reload-mcp` 或新会话刷新工具清单。

## Finnhub MCP Tools

当前 schema 基线：`finnhub-mcp@1.21.3`。

- `search-symbol`：解析公司名称或有歧义的 ticker。
- `get-quote`：当前/最新报价快照。
- `get-company-profile`：公司身份与基础资料。
- `get-financials-snapshot`：核心基本面 KPI。
- `get-price-summary`：7d / 30d / 90d / 1y 价格统计。
- `get-news-pulse`：最近 7 天公司新闻。
- `get-recommendations`：分析师评级趋势。
- `get-calendar`：earnings / IPO / economic calendar。
- `get-insider-signal`：内部人交易信号。
- `get-peers`：同行列表。
- `get-exchange-symbols`：交易所 symbol 样本。
- `search-tools`：MCP 能力发现。

精确参数、必填/可选项、返回字段、单位、view、日期范围与 Premium 限制见 [`finnhub-mcp-1.21.3-schema.md`](../stock-data/references/finnhub-mcp-1.21.3-schema.md)。正常股票分析不必读取整个 reference；只有参数/字段/单位不确定、schema error、MCP 升级或名称变更、需要核对 period/date/kind/view 限制，或用户询问未覆盖能力时才查。`search-tools` 只用于能力发现，不要因拆分 schema 而在每次股票分析时机械调用。

## Procedure

1. **界定问题与 ticker。** 明确 ticker 时（如 AAPL、NVDA、MSFT）直接用 ticker，不调用 `search-symbol`。名称不清、重名或匹配不唯一时，调用 `search-symbol`；只有高置信度/明确匹配才继续，否则询问用户。
2. **选择最小深度。** 按下方 Level 1/2/3 路由，不为“完整”机械调用 12 个工具。
3. **调用工具时使用真实参数。** 遵守上述 required/optional 参数及日期限制；不能添加 schema 没有的 ticker、date、period 或 view 参数。
4. **核验每个 envelope。** 先看 `is_success`、`error_type`、`premium`、`rate_limit`，再读取 `data`。缺失字段保留为缺失，不从其他字段猜填。
5. **写回答。** 将 MCP 返回的事实与模型解释分开；每个行情数字标出 Finnhub MCP 工具名及源时间戳。若工具未给时间戳、时间过旧或缓存可能影响新鲜度，明确说明。
6. **只总结实际调用过的内容。** 没调用新闻/日历/评级/内部人工具，就不写成已核验事实。

### Level 1 — 快速行情

适用“NVDA 现在多少钱”“AAPL 今天涨多少”等。通常只调用 `get-quote`；ticker 不清楚时才先 `search-symbol`。简洁列出价格、涨跌、日内高低/开盘/前收（只列实际返回字段）和 `timestamp_utc`。不要顺手启动基本面研究流程。

### Level 2 — 标准分析（用户意图优先）

先识别用户真正关心的维度，再调用最小必要工具；Level 2 不代表固定执行一条工具链。

#### 当前价格 / 日内行情

适用“NVDA 现在多少钱”“AAPL 今天涨多少”“TSLA 今天表现怎么样”。调用 `get-quote`；ticker 不明确时才先调用 `search-symbol`。不要顺手调用基本面、新闻、同行或完整研究工具。

#### 基本面 / 估值

适用“MSFT 基本面怎么样”“NVDA 估值怎么样”“AAPL 的 PE、EPS 怎么样”。调用 `get-company-profile` 与 `get-financials-snapshot`。ticker 已明确时不调用 `search-symbol`。只有问题涉及近期价格表现时才加 `get-price-summary`；只有问题涉及新闻/催化剂时才加 `get-news-pulse`。

#### 最近价格走势

适用“NVDA 最近走势怎么样”“AAPL 过去一个月表现如何”“TSLA 最近波动大吗”。调用 `get-quote` 与 `get-price-summary`。按用户时间范围从 schema 支持的 `7d`、`30d`、`90d`、`1y` 中选；用户未指定时用 `30d`。不要默认调用公司资料、基本面或新闻。

#### 新闻 / 催化剂

适用“NVDA 最近有什么消息”“AAPL 最近为什么波动”“MSFT 最近有什么催化剂”。优先调用 `get-news-pulse`；只有需要和当前表现结合时才加 `get-quote`；用户关注财报或近期事件风险时再加 `get-calendar`。不要默认拉完整基本面。

#### 分析师观点

适用“分析师现在怎么看 NVDA”“AAPL 当前评级怎么样”。调用 `get-recommendations`；只有需要关联当前价格时才加 `get-quote`。不要自动展开完整研究流程。

#### 财报 / 近期事件

适用“NVDA 什么时候财报”“AAPL 最近有没有重大事件”。调用 `get-calendar`；需要事件背景时再加 `get-news-pulse`。没有日历结果不等于已确认没有事件。

#### 内部人交易

适用“NVDA 最近内部人有没有买卖”“AAPL 高管最近有没有减持”。调用 `get-insider-signal`。不要因此自动拉取其他数据，也不要把交易方向写成价格方向的保证。

#### 同行 / 相对估值

适用“NVDA 和同行相比估值高吗”“AMD 和 NVDA 哪个估值更高”“MSFT 相对同行怎么样”。调用 `get-company-profile`、`get-financials-snapshot`、`get-peers`；随后最多选 3–5 家代表性同行调用 `get-financials-snapshot`。若用户已明确指定比较对象，不一定需要 `get-peers`。

#### 泛化分析请求

只有“分析 NVDA”“看看 AAPL”“MSFT 现在整体怎么样”等没有限定维度的请求，才采用标准综合流程：`get-quote`、`get-company-profile`、`get-financials-snapshot`、`get-price-summary`（默认 `30d`）、`get-news-pulse`。根据实际问题再决定是否增加 `get-recommendations` 或 `get-calendar`。仍不默认调用 `get-insider-signal`、`get-peers`、`get-exchange-symbols` 或 `search-tools`。

核心路由：用户意图 → 最小必要数据 → 必要的 MCP tools → 分析；不是“Level 2 → 固定调用五个工具”。

### Level 3 — 深度研究

仅在用户明确要求“深度研究/完整分析/研究报告”时考虑标准流程之外的 `get-recommendations`、`get-calendar`、`get-insider-signal`、`get-peers`。仅当估值/竞争位置问题需要时，挑 3–5 家代表性同行调用 `get-financials-snapshot`；不要遍历几十家公司。

### 多股票比较

每只股票优先调用 `get-quote` 与 `get-financials-snapshot`；需要确认名称时才调用 profile/search。用户关心催化剂时再逐只调用 `get-news-pulse`。用表格比较，但某项数据为空就标 N/A，不估算、不补齐。避免为每只股票运行完整深度流程。

## Analysis and Output

标准分析只输出有数据支持的部分：

- **市场概览**：价格、涨跌、时间戳、日内区间；引用 `get-quote`。
- **价格表现**：区间收益、高低、波动、最新收盘及时间；引用 `get-price-summary`，不把 `latest.close` 冒充实时价。
- **公司/基本面**：行业、规模、实际返回的 KPI；说明单位（market cap 为 USD 百万美元，shares 为百万股）。高 P/E 不自动等于高估，低 P/E 不自动等于低估；必须结合业务、成长、盈利和市场预期，且只讨论已取得的证据。
- **新闻/催化剂**：以 `get-news-pulse` 返回的 headline、source、datetime 为准；合并重复报道，区分新闻事实与可能影响。注意新闻可能混有广泛市场主题，不能只凭标题断定其对公司有直接影响。
- **分析师/日历/内部人/同行**：只在调用对应工具后展示；分析师共识是观点，日历事件是潜在波动源，内部人交易不是方向保证。
- **综合观察**：分列有数据支持的积极因素、风险和后续需跟踪变量；不做买入/卖出/持有建议、目标价、仓位或确定性预测。

把 **MCP 返回的事实** 与 **模型推断** 分开标注。股票价格属于强时效数据：必须先调用 `get-quote`；引用 `timestamp_utc`。若市场已收盘或时间戳较旧，称为“最后可用快照/成交”，不要说成此刻实时。当前 12 个工具没有市场开闭状态端点；不要从时钟或记忆推断市场状态。引用历史财务/资料时若没有时间戳，明确注明响应未提供 as-of 字段。

Finnhub MCP 是本 Skill 的第一优先数据源。仅当 Finnhub 调用失败、返回 Premium/403/429、所需字段缺失或无可用数据时，才使用 `global-stock-data` 回退，并仅用于该 Skill 确实支持的同类数据；回退时说明 Finnhub 失败原因，并明确标注 `global-stock-data` 来源，绝不将其数值归于 Finnhub。Finnhub 成功时，本 Skill 默认不自动调用其他来源作交叉验证；用户明确要求或更高优先级助手策略要求另取数据时例外，且必须独立标注来源。`related_skills` 仍仅为元数据关系，不触发自动调用。任何实际行情/基本面数字都标明来源工具及时间/期间。

## Efficiency

- 默认 `summary`；只有 summary 缺少回答问题所需字段时用 `standard`；只有用户明确要原始/完整数据或需要核验被省略内容时才用 `full`。
- `get-quote` 的 schema 虽接受 `view`，工具描述称 view 不改变其已整理输出，通常不传。
- 复用同一轮已取得的同一 ticker 数据；不要反复拉取同一快照，也不要调用 `search-tools` 作为每次分析的固定步骤。
- `get-news-pulse` 固定 7d；`get-recommendations` 无日期参数；不要传 schema 不支持的时间窗。
- 多股票/同行调用要限制数量；不为 token 完整性请求整份交易所 universe 或 `full`。

## Errors, Permissions, and Missing Data

- `premium=true`、PremiumRequired、403/no access：说明当前 Finnhub key/套餐无权访问该项，停止重试；对 `global-stock-data` 确实支持的同类数据回退，并明确标注数据源；不支持时报告缺失。
- 429：停止继续请求 Finnhub，不循环重试；对 `global-stock-data` 确实支持的同类数据回退，并报告限流以及响应中的 `rate_limit.remaining` / `reset_at`（若存在）。
- 网络/服务瞬时错误：仅在非 429、非 Premium 情况下可用同一 MCP 工具合理重试一次；仍失败时，对 `global-stock-data` 确实支持的同类数据回退，否则报告错误。
- `null`、空数组、missing/unavailable：明确写 Finnhub 字段/数据不可用；若 `global-stock-data` 能提供同类数据则作为独立回退并标明来源，否则保持缺失；不使用模型记忆、新闻标题或价格走势补数。
- `is_error` 与 envelope `is_success` 可能表达不同层次的失败，两者都检查；不要把空成功 envelope 当成数据成功。
- 原工具未返回 sentiment 时，不生成 Finnhub 情绪分或 bullish/bearish 百分比；没有财报日期时，不说“近期无财报”，除非日历查询成功且日期范围明确。

## Unsupported Indicators

若用户要 RSI、MACD、SMA/EMA、Bollinger Bands、stochastic、volume profile、期权流或 WebSocket tick，先用 `search-tools` 检查当前 MCP 能力。若没有匹配工具，明确说当前 Finnhub MCP 未暴露该指标/数据；不要凭走势图估数，也不要自行请求 Finnhub REST API。仅在用户明确要求“从 MCP 返回的历史 candles 计算派生指标”时，确认 `get-price-summary(view='full')` 实际返回所需 candles 后，才可单独、明确标记为本地派生值。

## Verification

- 先看当前 Hermes 工具清单是否含 12 个 Finnhub aliases；server-native 名与 Hermes alias 的映射以当前 MCP schema / tool list 为准。
- 对每个回答，确认调用参数都出现在 `inputSchema`；遇到升级或未知能力时，仅调用一次 `search-tools` 或重新读取当前 schema。
- 交付前复核：每个行情数字有来源和时间戳；每个财务字段确实出现在响应中；所有缺失/Premium/限流均被披露；没有直接 REST 请求、虚构指标或投资建议。
- 对涉及投资决策的输出，附上：*This is informational research, not investment advice. Data may be delayed, incomplete, or wrong. You are responsible for your own decisions.*
