# Yahoo Finance / yfinance 数据路由

选择 Yahoo 后，结构化取数必须先调用 `global_stock_data.py` 的 `yahoo_*` yfinance 路由。请求错误、空结果、缺依赖或能力不可用时自动尝试登记的旧兼容接口；不提供后端选择开关。`RateLimited/AccessDenied/BudgetExceeded` 或已打开的作业熔断是终止性失败，不触发兼容回退。yfinance 已返回的 partial 结果保留，不被回退响应覆盖；回退的实际接口、来源和触发原因写入 provenance。没有安全等价旧接口时返回 `NoLegacyRoute`。不默认用 web fetch 或网页摘要。美股仍优先使用实际已连接且覆盖所需字段的 Finnhub；只补缺项，不重复拉取已有成功结果。原文网页仅作补充核对证据，明确来源和用途。数据范围仍为 US/HK 股票和 ETF；接入不增加预测模型、交易执行或数据授权。

## 环境与离线发现

本 Skill 的 Yahoo 路由由原生 Rust runtime 直接请求，不需要安装 Python 依赖或 yfinance。`--list`、`--help` 和离线 Quant 不联网、不创建 Yahoo 缓存。原生请求失败、空结果或能力不可用时自动尝试兼容旧接口；若兼容路由也不可用，则保留 `DependencyUnavailable` / `UnsupportedCapability` 触发原因和回退失败状态。运行时不自动安装依赖、不切换到网页接口。

适配目标为 spec 的 yfinance `1.7.0` / commit `5cae563642b59f49adf6a04a5ad6744f8b0e084d`；生产 provider 严格检查版本及 curl_cffi 后端，不提供 requests 诊断后端切换。`requirements-yahoo.txt` 声明固定适配来源及 `curl_cffi==0.15.0`（已阅读 streaming callback 实现），SciPy 是调用 repair 时需要的可选能力依赖；Yahoo 主路由依赖本身由 `requirements.txt` 强制声明。源码阅读/依赖声明不等于已验证安装。安装前核对依赖文件中的实际发布 pin 或固定 commit、执行解释器与可安装来源，记录实际版本/来源，不跟随 main。依赖安装、联网验收和已安装 Skill 替换需遵循用户授权，不读取浏览器 cookie。

在仓库根目录运行（安装后使用实际脚本路径，输入和输出放用户可写目录）：

```text
"$SKILL_DIR/scripts/run.sh" gateway --list
"$SKILL_DIR/scripts/run.sh" gateway --function yahoo_history --params-file history-request.json --output history-envelope.json --cache-dir runtime/yfinance-cache
"$SKILL_DIR/scripts/run.sh" collect yahoo --help
"$SKILL_DIR/scripts/run.sh" collect yahoo --request request.json --output gateway-responses.json --cache-dir runtime/yfinance-cache
"$SKILL_DIR/scripts/run.sh" research pipeline --request request.json --responses gateway-responses.json --output-root runtime
```

新 Yahoo gateway CLI 必须传 `--cache-dir runtime/yfinance-cache`，或明确设置 `STOCK_DATA_YAHOO_CACHE_DIR`；不传时无法执行 Yahoo 调用。collector 必须显式传 `--cache-dir`，不以 gateway 环境变量替代。目录需用户可写；仅实际 Yahoo 调用初始化缓存，离线发现不会创建缓存。示例使用 Skill 启动器；`$SKILL_DIR` 为该 Skill 的实际挂载路径。

精确参数及可配置预算以当前 `--list` / `--help` 为准；不要假定 yfinance 的全部参数可原样透传。gateway 输出单个 JSON envelope；collector 输出按 market 组织的 response map，而非 receipt 或研究报告。

## 按需能力表

| 优先级 / 函数 | 公开库能力与任务 | 解释边界 |
|---|---|---|
| P0 `yahoo_history` | Ticker.history；单标的精确区间、行动与所需 metadata | start 包含，end 不包含；研究仅日线 |
| P0 `yahoo_history_batch` | yf.download；同市场同频率同口径 | 逐标的状态；MultiIndex 拆分不以整表非空判断成功 |
| P0 `yahoo_quote` | 按需 fast_info、get_info、chart metadata | lastPrice 不自动等于实时价 |
| P0 `yahoo_profile` | get_info；公司资料、证券类型 | get_info 中库补写 symbol 不独立证明身份 |
| P0 `yahoo_financials` | get_income_stmt/get_balance_sheet/get_cash_flow，pretty=False | 年度/季度；仅库支持的利润表/现金流 TTM |
| P0 `yahoo_search` | Search；显式类型检索使用 Lookup 公开方法 | 名称/代码与股票/ETF 类型确认；`lookup_type=all/equity/etf`，缺能力返回明确错误 |
| P1 `yahoo_statistics` | get_info、get_valuation_measures | 当前指标/可用估值序列不是历史 PIT 特征 |
| P1 `yahoo_analysis` | EPS/revenue 预期、趋势/修正、目标价、评级 | 来源观点不等于已报告业绩 |
| P1 `yahoo_holders` | 主要/机构/基金持有人及内部人 | 披露日期不等于实时成交 |
| P1 `yahoo_options` | options、option_chain | 只取指定可用到期日；不补造 Greeks/方向 |
| P1 `yahoo_earnings` | calendar、get_earnings_dates、earnings_history | 来源财报日程/业绩；部分库实现底层解析 HTML |
| P1 `yahoo_funds` | 按需 funds_data | 说明、费用、资产/行业权重、主要持仓；缺日期明示 |
| P1 `yahoo_screen` | EquityQuery/ETFQuery、yf.screen | 登记字段/操作符的 JSON 条件，无 eval/任意方法透传 |

## 当前 gateway 参数（已对照本机 `gateway --list` 输出）

`gateway --list` 共 56 个函数，其中 `source="Yahoo Finance"` 20 个（13 个 capability + 7 个 legacy 直连）。签名以实测 `--list` 为准：

| 函数 | 参数（省略 symbol/query 等必填值以外的参数时使用默认） | 原生当前实际生效范围 |
|---|---|---|
| yahoo_history | symbol, start, end, period, interval="1d", price_basis="provider", prepost=False, repair=False, timeout=15 | 全部生效；`prepost`→wire `includePrePost=true`；`repair=true` 请求前 `UnsupportedCapability`；`timeout` 作为 job/单请求超时上限 |
| yahoo_history_batch | symbols，以上窗口/口径参数，concurrency=1 | 全部生效；符号 1–10、去重、美港分开、concurrency∈{1,2} |
| yahoo_quote / yahoo_profile | symbol（yahoo_quote 另有 fields） | 原生模块集合固定（quote: price/summaryDetail/financialData/defaultKeyStatistics；profile: assetProfile/summaryProfile/price）；**`fields` 接受但不改变原生模块集合** |
| yahoo_financials | symbol, frequency="yearly", statements | 生效：`frequency=yearly/quarterly`；`trailing/ttm` 原生不支持并回退旧 `financial_statements_yahoo`；**`statements` 接受但不改变原生报表集合** |
| yahoo_search | query, count=8, lookup_type | 原生走 `v1/finance/search`（q/quotesCount/newsCount=0）；**`lookup_type` 在原生成功时不生效（不做 Lookup 类型过滤）**；原生无数据且 `lookup_type` 非空时无兼容路由（`NoLegacyRoute`），否则回退旧 `stock_search`（东财） |
| yahoo_statistics | symbol, valuation=False | 原生固定 key_statistics 投影；**`valuation` 接受但不改变原生输出** |
| yahoo_analysis / yahoo_holders | symbol, modules | 原生固定 analyst_estimates / institutional_holders 投影；**`modules` 接受但不改变原生集合** |
| yahoo_options | symbol, expiration | 生效：expiration 必须 `YYYY-MM-DD` 且命中已列到期日，否则 `DataUnavailable`；失败回退旧 `options_chain`（Unix 秒、仅美股） |
| yahoo_earnings | symbol, modules, limit=12 | 原生固定 calendarEvents/earnings/earningsHistory/earningsTrend；**`modules`/`limit` 接受但不改变原生模块集合或条数** |
| yahoo_funds | symbol, modules | 原生固定 fundProfile/summaryProfile/topHoldings/fundPerformance；**`modules` 接受但不改变原生集合** |
| yahoo_screen | query, query_type="equity", size=25, max_pages=1, max_results=250 | **完全不支持**：请求前 `UnsupportedCapability`，无兼容路由 |

13 个 capability 都必须显式传 `--cache-dir`（或 `STOCK_DATA_YAHOO_CACHE_DIR`），否则 preflight 返回 `InvalidParameters("Explicit writable cache_dir required for Yahoo calls")` 且不发请求。单作业默认 timeout=15s、deadline=120s、请求预算 30；超时/预算/限流/拒绝类终态失败会把进程级 yahoo job 熔断 latch 住。collector 另公开 timeout=15、deadline=120、request-budget=30，cache-dir 必填；批量预算须在其实际支持的作业配置处明确设置，不靠声明自动增加。

上述“接受但不生效”的参数是当前实现的真实边界，不是透传承诺；不要用它们假定 yfinance 的同名过滤/投影已经具备。上面的 `--list` 签名与下面矩阵都按实测输出维护。

当前登记模块：analysis 为 earnings_estimate/revenue_estimate/eps_trend/eps_revisions/growth_estimates/target_prices/recommendations/upgrades_downgrades（默认 earnings_estimate/target_prices）；holders 为 major/institutional/mutualfund/insider_transactions/insider_purchases/insider_roster（默认 major）；earnings 为 calendar/dates/history（默认 calendar，limit 1–100）；funds 为 description/overview/operations/asset_classes/top_holdings/sector_weightings（默认 description/overview）。search query 长度 1–200、count 1–100，lookup_type 非空返回 UnsupportedCapability。screen max_pages 1–10、max_results 1–1000，operator 仅 AND/OR/EQ/IS-IN/BTWN/GT/GTE/LT/LTE，条件字段由 query 类校验，树深度上限 8。

## 行情、日期与复权

`yahoo_history` 的核心参数是 symbol、start/end 或 period、interval、price_basis、prepost、repair、timeout。日期窗口与 period 互斥，先校验再请求。默认普通查询使用 provider 口径；研究采集明确请求 adjusted。原生输出保留未自动调整的 provider OHLC、Adj Close、成交量与按 bar 的 `dividends`/`stock_splits`/`capital_gains`（来自同一份 chart `events`，只覆盖返回的 bars；事件日期按交易所时区 session 日期对齐）；keepna=True、rounding=False 的语义是“不补零、不前向填充、不按复权比例调整成交量”。无事件的 bar 保持 `null`，**不填 0**；这与 yfinance 把缺失 action 列填 0 的行为不同，A-PARITY-01 差分把该差异记录为表示层差异而非数据丢失。`action_coverage_confirmed=false`：空 action 列不得读作“无分红/无拆股”。

adjusted OHLC 由同根 `Adj Close / Close` 等比例转换；保留 ratio、provider 值和变换记录。缺失/非有限/无效 ratio 记录缺口，不能冒称已复权。repair 默认 False；显式要求或已记录异常条件才修复一次，保留修复前后 OHLC、Adj Close、行动、币种及差异。`Repaired?` 不是完整修复证据。

日线 `date` 为来源交易所时区的 session 日期，同时保留 UTC 时间和时区。本地午夜索引不是成交/收盘时间；chart regularMarketTime 不替代历史 bar 的源时间。研究 request 的 history_window.end 为闭合 session 日期，转换到 yfinance 时 end 使用下一本地自然日并留档。用户窗口优先，默认 52 周（364 个自然日，约 252 个交易日）；不因样本不足扩窗。日内各 interval 的覆盖限制分别检查，不能把 1m/分钟/小时线同等承诺，不能送入现有日线预测器。

最后一根闭合状态须有覆盖该日期的交易日历或具体时段证据；未知时交给中央检查，不用主机时钟/固定 16:00 假定收盘。currentTradingPeriod/tradingPeriods 不构成完整 52 周日历；metadata 惰性读取仅限所需字段。`adjustment_evidence` 保留 source、price_basis、checked_through、实际行动与覆盖；覆盖未知的空行动不是“无行动”。今天下载的历史复权数据保留下载/修订时间，不伪造历史 vintage。

旧直连与 yfinance 的 actual_source 均为 `Yahoo Finance`；库名/版本另列，不能当独立双源。仅对同 session、币种、价格口径的独立闭合价格交叉验证；不能把 adjusted close 与其他源原始 close 直接送进 0.3% 冲突检查。

批量默认最多 10 个标的，US/HK 分开，默认串行，显式并发最多 2；限流停止新增请求。固定批量下载口径并按标签拆列，区分源缺失和日期轴对齐空位；全空标的非成功。逐标的记录 status、数量、实际窗口、源时间、错误与缺口。部分成功保留成功结果，只对已确认可重试的缺项补取，不重跑整批，不依赖库私有错误容器。

## 财报、新闻和专项映射

财报保留原始科目名、statement、period_end、frequency、value、unit/currency，以及来源确实给出的披露/可得时间。财务币种与交易币种分开；期末不是披露日期。逐表检查空表和状态，部分空表 partial，三份空表不标完整。ETF 不使用公司三表代替基金资料。

**本包已移除全部 Yahoo 新闻能力。** `yahoo_news`、旧 `stock_news`、`normalize_news` 及 collector 的 news 域、`--news-count`、`--news-tab` 参数一并删除，`yahoo_collect.py` 只采 market 域。原因是实测上游 ticker 端点已不可用（`/xhr/ncp` 返回 404，全部标的 0 篇），且 Finnhub MCP 已能稳定提供带时区的新闻。新闻唯一入口改为宿主 Finnhub MCP `get-news-pulse`，其文章按 [collection-adapter.md](collection-adapter.md) 规范化后进入同一 News 契约。宿主未连接 Finnhub MCP 时，消息面按 [news-analysis.md](news-analysis.md) 记 `not_assessed`，不做二次联网。

期权先列可用 YYYY-MM-DD 到期日，再取所需链；lastTradeDate 保留 UTC，港股可用性不靠库存在推断。基金持仓无日期明确缺失，不回填历史。筛选页上限 250，限制页数/结果数，记录 offset/total/覆盖；当前名单不是历史 universe。估值/财务/预期/持仓只作描述性资料，不自动加入 A/B/C/D。

## 会话、缓存与失败

会话在作业开始确定，复用 Ticker 和成功结果，不跨线程切换 locale/session；yfinance 全局配置/YfData 单例要求同进程统一配置。默认关闭 debug/progress，异常不隐藏。网络边界、鉴权 redirect 例外和预算见 [source-policies.md](source-policies.md)。不得归档 cookie/crumb/代理凭证；未取得 wire payload 时归档只能称 yfinance 原生整理结果，不称原始 HTTP。

首次 Yahoo 调用前将内置时区/cookie 缓存指向显式用户可写 cache-dir，缓存与快照分开，不写安装目录。无 requests_cache，不将 cookie/timezone 或内存缓存称长期业务缓存。复用已有文件保留原 fetched_at、源时间和参数指纹，复核 freshness/覆盖，不改时间掩盖过期。

错误类别：DependencyUnavailable、UnsupportedCapability、InvalidParameters、RateLimited、AccessDenied、NetworkError、InvalidResponse、DataUnavailable、InsufficientCoverage、BudgetExceeded、NoLegacyRoute、LegacyFallbackFailed。gateway 保留 success/empty/error 并支持 partial；全部成功、有依据无数据及可用部分成功退出 0，依赖/请求错误或核心全部失败退出 1。退出 0 不代表研究完整。网络失败不会包装成成功空数组；market 域失败不清空已成功的另一域。兼容旧接口只在 yfinance 失败/空结果时自动调用，不调用网页 fallback。

终态传输失败（RateLimited、AccessDenied、预算耗尽、超界/无效响应）会 latch 为**进程级 Yahoo 作业熔断**：同一进程内后续的 yfinance 调用与旧兼容回退都直接拒绝，直到进程结束；不能靠新建 provider 或改走旧接口绕过已打开的熔断。每次旧回退尝试按一次作业请求计入同一预算（回退内部 HTTP 不在该 transport 边界内，不逐请求计数）；失败/空结果之外的非终态原因才允许一次语义等价回退。

## 研究衔接与兼容

原生 `collect yahoo`（历史实现见仓库 `legacy/python/yahoo_collect.py`）只采 market 域，复用宿主已有 Finnhub 成功结果；不把 yfinance 参数塞进研究 request，不让 pipeline 隐式联网。字段与 provenance 见 [collection-adapter.md](collection-adapter.md)。使用 snapshot schema 1.3；forecast 必须提供 `policy_window` 与拆股调整的 provider close。

方向研究仍按 Quant → 验证/审计 → News → Final Synthesis；新闻失败记 not_assessed，不能当 neutral 或改 Quant P(up)。中央 [reliability-gates.md](reliability-gates.md) 权威：复权/日历未知是降级，不自动致命；明确污染/身份/价格冲突按原 gate 处理。

**当前采集不是已审计合格预测输入的承诺。** 单标的 history 的 `source_timestamp_kind=bar_label_not_trade_time` 明示日线索引时间，不是收盘/行情更新时间；即使交易日历确认该 bar 已闭合，仍不能把此标签改写为收盘时刻或 fetched_at。该真实源时间可能触发现有 24h freshness / `source_before_close` gate；保持 gate 不变，审计失败时报告阻断而非伪造时间。来源未确认的 `unit` 留 null，不从 currency 猜补；缺 unit、calendar-backed closure、身份或覆盖时 collector 记录 partial，freezer 对缺失 unit 记 `MARKET_UNITS_UNVERIFIED` 降级置信度（不单独否决），unit 与其他元数据**相互矛盾**才是数据完整性问题（`MARKET_UNITS_CONFLICT`，致命）。`fast_info` 报价当前不提供可靠源时间；batch 的对齐轴、空位来源、交易所身份/币种/时区及修复覆盖也不能视为已证实。只有实际数据通过既有完整验证后才可称为有效研究输入。

旧 `stock_kline_yahoo`、`yahoo_quote_summary`、`financial_statements_yahoo`、`key_statistics`、`analyst_estimates`、`institutional_holders`、`options_chain` 保留原签名/返回结构，并作为 yfinance 失败后的强制自动回退路径。历史、报价/资料、财报、统计、预期、持仓、期权、财报事件和基金有明确映射；`yahoo_search` 先走原生 `v1/finance/search`，原生空/失败且未指定 `lookup_type` 时才回退旧 `stock_search`（东财，provenance 标记实际来源为 Eastmoney search），`lookup_type` 非空时无兼容路由并返回 `NoLegacyRoute`；原生成功时 `lookup_type` 不做类型过滤。结构化筛选（`yahoo_screen`）没有原生实现也没有语义等价旧入口，显式返回 `UnsupportedCapability`（不是 `NoLegacyRoute` 伪回退信封）。特别是旧 K 线 date 仍为 UTC 日期、range_ 仍非精确 start/end，不能与新交易所日期混同；结果标 partial 并保留窗口覆盖警告。

离线 fixture/transport/管道验收与实时连通性分开报告；文档和注册表不是联网成功、依赖已验证安装或有效研究数据的证明。不含 WebSocket、登录/Premium、任意 URL/方法、长期全市场爬取或 PIT 新闻档案。

## 原生路由能力矩阵（A-PARITY-01，对照 `gateway --list` 实测）

20 个 Yahoo 函数 = 13 个 provider capability + 7 个 legacy 直连。下表逐条给出：原生是否支持、参数语义、显式不支持的边界、输出来源、失败处置。`gateway --list` 的签名与下表一致；不在 `--list` 里的参数名不会被接受（`unexpected keyword argument`）。

| capability | 原生支持 | 参数语义（`--list` 实测） | 输出来源 | 显式不支持 / 失败处置 |
|---|---|---|---|---|
| yahoo_history | 支持：`query2.finance.yahoo.com/v8/finance/chart/<symbol>` | symbol, start, end, period, interval, price_basis, prepost, repair, timeout；`prepost`→`includePrePost=true`；`price_basis=provider/adjusted`；start/end 先转粗 `range` 再在客户端按 [start,end) 过滤；`source_timestamp` 是 bar label | 原生归一化（`native_payload_kind=native_chart_normalized_from_wire`，`wire_route` 见上）；含 provider OHLC、Adj Close、volume、按 bar 的 dividends/stock_splits/capital_gains 与 `adjustment_evidence.actions` | `repair=true` 请求前 `UnsupportedCapability`（零请求）；空/错误等非终态失败回退旧 `stock_kline_yahoo`（`fallback_used=true`、`provider_library=legacy_compat`） |
| yahoo_history_batch | 支持：逐 symbol 复用 history | symbols（1–10、去重、美港必须分开）、窗口/口径参数、concurrency∈{1,2} | 原生逐 symbol 归一化；`data.symbols.<symbol>` | 单 symbol 失败只标记该 symbol（`status=failed`/`error.category`）；外层 success/partial/empty；终态策略错误不回退 |
| yahoo_quote | 支持：quoteSummary | symbol；`fields` 接受但不改变固定模块集合 | 原生 quoteSummary：price/summaryDetail/financialData/defaultKeyStatistics | 空/错误回退旧 `yahoo_quote_summary` |
| yahoo_profile | 支持：quoteSummary | symbol | 原生 quoteSummary：assetProfile/summaryProfile/price | 空/错误回退旧 `yahoo_quote_summary` |
| yahoo_financials | 支持年度/季度报表 | symbol, frequency=yearly/quarterly, statements；`statements` 接受但不改变报表集合；`trailing/ttm` 不原生 | 原生 quoteSummary 报表（经 `financial_statements_yahoo` 路由） | `trailing/ttm` → `UnsupportedCapability` 后回退旧 `financial_statements_yahoo`（带“无法提供 TTM”警告） |
| yahoo_search | 支持：`v1/finance/search`（q/quotesCount/newsCount=0） | query, count, lookup_type；`lookup_type` 原生成功时不生效（无 Lookup 类型过滤） | 原生 quotes[{symbol,name,exchange,quote_type}], total | 原生空/失败：`lookup_type` 非空 → 无兼容路由（`NoLegacyRoute` 错误信封）；否则回退旧 `stock_search`（东财，`source=Eastmoney search`） |
| yahoo_statistics | 支持：quoteSummary→key_statistics 投影 | symbol；`valuation` 接受但不改变输出 | 原生 key_statistics 投影 | 空/错误回退旧 `key_statistics` |
| yahoo_analysis | 支持：quoteSummary→analyst_estimates 投影 | symbol；`modules` 接受但不改变集合 | 原生 analyst_estimates 投影 | 空/错误回退旧 `analyst_estimates` |
| yahoo_holders | 支持：quoteSummary→institutional_holders 投影 | symbol；`modules` 接受但不改变集合 | 原生 institutional_holders 投影 | 空/错误回退旧 `institutional_holders` |
| yahoo_options | 支持：options chain（先列到期日） | symbol, expiration=`YYYY-MM-DD`；须命中已列到期日 | 原生 options chain（lastTradeDate 保留 UTC） | 未命中 → `DataUnavailable`；空/错误回退旧 `options_chain`（Unix 秒、仅美股） |
| yahoo_earnings | 支持：quoteSummary | symbol；`modules`/`limit` 接受但不改变原生模块集合/条数 | 原生 calendarEvents/earnings/earningsHistory/earningsTrend | 空/错误回退旧 `yahoo_quote_summary`（同模块） |
| yahoo_funds | 支持：quoteSummary | symbol；`modules` 接受但不改变集合 | 原生 fundProfile/summaryProfile/topHoldings/fundPerformance | 空/错误回退旧 `yahoo_quote_summary`（同模块） |
| yahoo_screen | **不支持** | query, query_type, size, max_pages, max_results | 无 | 请求前显式 `UnsupportedCapability`，零请求；无兼容路由；不构造伪回退信封。保持失败直至 Rust 实现 + 差分证据完成 |
| stock_kline_yahoo / yahoo_quote_summary / key_statistics / analyst_estimates / institutional_holders / financial_statements_yahoo / options_chain（7 个 legacy 直连） | 支持（各自直连 wire；不经过 provider 回退层） | `--list` 各自的原始签名（`range_`、`include_metadata`、`modules`、`quarterly`、Unix `expiration` 等） | `actual_source=Yahoo Finance`，`provider_library` 标记为 legacy 直连；K 线 date 仍是 UTC 日期、`range_` 仍是粗窗口 | 无自动回退；错误直接返回 |

补充边界：

- **actions 投影（A-PARITY-01 修复后）**：`yahoo_history` 把同一份 chart `events`（`dividends`/`splits`/`capitalGains`）按交易所时区 session 日期投影到 bar 的 `dividends`/`stock_splits`/`capital_gains` 和 `adjustment_evidence.actions`；兼容回退路径使用同一 helper。覆盖范围仅限返回的 bars（`coverage_scope=returned_bars_only`、`action_coverage_confirmed=false`），空 action 列不等于“无行动”。无事件行保持 `null`，不填 0。
- **HTTP 状态映射**：原生 transport 失败把 HTTP 429 映射为 `RateLimited`、HTTP 403 映射为 `AccessDenied`，二者是终止性策略错误：latch 进程级 job 熔断、**不尝试 legacy 回退**。其他 4xx/5xx 保持 `HTTPError`（非终态，可回退一次）。
- **`repair=true`**：yfinance repair 是库级重新下载与差异比较，chart wire 无等价能力；原生路由在任何请求发出前显式返回 `UnsupportedCapability`，绝不接受后忽略。保持失败直至 Rust 实现 + 差分证据完成。
- **`yahoo_screen`**：当前没有原生 screener 实现，也没有语义等价的 legacy 入口；显式 `UnsupportedCapability`。
- **provenance**：原生结果 `actual_source="Yahoo Finance"`、`provenance.library="native_rust"`、`archive_kind="normalized_native_result_not_wire_payload"`；兼容回退结果 `provider_library="legacy_compat"`、`fallback_used=true`、`provenance.raw_http_payload_captured=false`。
- **离线差分证据**：A-PARITY-01 用 `audit/post-acceptance/A-PARITY-01/fixtures/` 的冻结上游响应字节，分别喂给原生主路径（`MockTransport` + 真实 `yahoo_gateway::call_with_job`）和 yfinance 1.7.0（真实解析），逐字段比较 close/adjclose、actions、null、时区/session 日期、pre/post、batch partial 和异常路由；工具在 `tools/refactor/parity/`。R15 的四层映射/legacy/回退/路由夹具不能替代该原生主路径差分。
