# Yahoo Finance / yfinance 数据路由

选择 Yahoo 后，结构化取数默认调用 `global_stock_data.py` 的 `yahoo_*` 入口；不默认用 web fetch、网页摘要或旧直连接口。美股仍优先使用实际已连接且覆盖所需字段的 Finnhub；只补缺项，不重复拉取已有成功结果。原文网页仅作补充核对证据，明确来源和用途。数据范围仍为 US/HK 股票和 ETF；接入不增加预测模型、交易执行或数据授权。

## 环境与离线发现

Python 3.10+；Yahoo 使用可选 `requirements-yahoo.txt`。只在调用 Yahoo 时懒加载 yfinance，导入、`--list`、`--help` 和离线 Quant 不联网、不创建 Yahoo 缓存。缺依赖报告 `DependencyUnavailable`，缺版本能力报告 `UnsupportedCapability`；运行时不自动安装、不静默换后端或改用旧接口。

适配目标为 spec 的 yfinance `1.7.0` / commit `5cae563642b59f49adf6a04a5ad6744f8b0e084d`；生产 provider 严格检查版本及 curl_cffi 后端，不提供 requests 诊断后端切换。`requirements-yahoo.txt` 声明固定适配来源及 `curl_cffi==0.15.0`（已阅读 streaming callback 实现），SciPy 为修复可选环境依赖；源码阅读/依赖声明不等于已验证安装。安装前核对依赖文件中的实际发布 pin 或固定 commit、执行解释器与可安装来源，记录实际版本/来源，不跟随 main。依赖安装、联网验收和已安装 Skill 替换需遵循用户授权，不读取浏览器 cookie。

在仓库根目录运行（安装后使用实际脚本路径，输入和输出放用户可写目录）：

```text
python stock-data/scripts/global_stock_data.py --list
python stock-data/scripts/global_stock_data.py --function yahoo_history --params-file history-request.json --output history-envelope.json --cache-dir runtime/yfinance-cache
python stock-data/scripts/yahoo_collect.py --help
python stock-data/scripts/yahoo_collect.py --request request.json --domains market news --output gateway-responses.json --cache-dir runtime/yfinance-cache
python stock-data/scripts/quant_research.py pipeline --request request.json --responses gateway-responses.json --output-root runtime
```

新 Yahoo gateway CLI 必须传 `--cache-dir runtime/yfinance-cache`，或明确设置 `STOCK_DATA_YAHOO_CACHE_DIR`；不传时无法执行 Yahoo 调用。collector 必须显式传 `--cache-dir`，不以 gateway 环境变量替代。目录需用户可写；仅实际 Yahoo 调用初始化缓存，离线发现不会创建缓存。示例中的 `python` 指已配置 gateway 通用依赖和 Yahoo 可选依赖的实际解释器。

精确参数及可配置预算以当前 `--list` / `--help` 为准；不要假定 yfinance 的全部参数可原样透传。gateway 输出单个 JSON envelope；collector 输出按 market/news 组织的 response map，而非 receipt 或研究报告。

## 按需能力表

| 优先级 / 函数 | 公开库能力与任务 | 解释边界 |
|---|---|---|
| P0 `yahoo_history` | Ticker.history；单标的精确区间、行动与所需 metadata | start 包含，end 不包含；研究仅日线 |
| P0 `yahoo_history_batch` | yf.download；同市场同频率同口径 | 逐标的状态；MultiIndex 拆分不以整表非空判断成功 |
| P0 `yahoo_quote` | 按需 fast_info、get_info、chart metadata | lastPrice 不自动等于实时价 |
| P0 `yahoo_profile` | get_info；公司资料、证券类型 | get_info 中库补写 symbol 不独立证明身份 |
| P0 `yahoo_financials` | get_income_stmt/get_balance_sheet/get_cash_flow，pretty=False | 年度/季度；仅库支持的利润表/现金流 TTM |
| P0 `yahoo_news` | Ticker.get_news 或 Search.news | 证券 stream 与关键词结构独立映射 |
| P0 `yahoo_search` | Search；显式类型检索使用 Lookup 公开方法 | 名称/代码与股票/ETF 类型确认；`lookup_type=all/equity/etf`，缺能力返回明确错误 |
| P1 `yahoo_statistics` | get_info、get_valuation_measures | 当前指标/可用估值序列不是历史 PIT 特征 |
| P1 `yahoo_analysis` | EPS/revenue 预期、趋势/修正、目标价、评级 | 来源观点不等于已报告业绩 |
| P1 `yahoo_holders` | 主要/机构/基金持有人及内部人 | 披露日期不等于实时成交 |
| P1 `yahoo_options` | options、option_chain | 只取指定可用到期日；不补造 Greeks/方向 |
| P1 `yahoo_earnings` | calendar、get_earnings_dates、earnings_history | 来源财报日程/业绩；部分库实现底层解析 HTML |
| P1 `yahoo_funds` | 按需 funds_data | 说明、费用、资产/行业权重、主要持仓；缺日期明示 |
| P1 `yahoo_screen` | EquityQuery/ETFQuery、yf.screen | 登记字段/操作符的 JSON 条件，无 eval/任意方法透传 |

## 当前 gateway 参数（已对照公开包装器）

| 函数 | 参数（省略 symbol/query 等必填值以外的参数时使用默认） |
|---|---|
| yahoo_history | symbol, start, end, period, interval="1d", price_basis="provider", prepost=False, repair=False, timeout=15 |
| yahoo_history_batch | symbols，以上窗口/口径参数，concurrency=1 |
| yahoo_quote / yahoo_profile | symbol，quote 可用 fields |
| yahoo_financials | symbol, frequency="yearly", statements |
| yahoo_news | symbol 或 query, count=10, tab="news" |
| yahoo_search | query, count=8, lookup_type；Lookup 不可用须返回 UnsupportedCapability |
| yahoo_statistics | symbol, valuation=False |
| yahoo_analysis / yahoo_holders / yahoo_funds | symbol, modules |
| yahoo_options | symbol, expiration（省略时列可用到期日） |
| yahoo_earnings | symbol, modules, limit=12 |
| yahoo_screen | query, query_type="equity", size=25, max_pages=1, max_results=250 |

这些是 gateway 的公开参数，不等于 provider 内部字段名。选定 modules、枚举值、quote 字段和筛选条件必须符合当前 provider allowlist；不将库任意方法透传。collector 已公开 timeout=15、deadline=120、request-budget=30、news-count=10、news-tab=news，cache-dir 必填；批量预算须在其实际支持的作业配置处明确设置，不靠声明自动增加。

当前登记模块：analysis 为 earnings_estimate/revenue_estimate/eps_trend/eps_revisions/growth_estimates/target_prices/recommendations/upgrades_downgrades（默认 earnings_estimate/target_prices）；holders 为 major/institutional/mutualfund/insider_transactions/insider_purchases/insider_roster（默认 major）；earnings 为 calendar/dates/history（默认 calendar，limit 1–100）；funds 为 description/overview/operations/asset_classes/top_holdings/sector_weightings（默认 description/overview）。news tab 仅 news/all/press releases，count 1–100。search query 长度 1–200、count 1–100，lookup_type 非空返回 UnsupportedCapability。screen max_pages 1–10、max_results 1–1000，operator 仅 AND/OR/EQ/IS-IN/BTWN/GT/GTE/LT/LTE，条件字段由 query 类校验，树深度上限 8。

## 行情、日期与复权

`yahoo_history` 的核心参数是 symbol、start/end 或 period、interval、price_basis、prepost、repair、timeout。日期窗口与 period 互斥，先校验再请求。默认普通查询使用 provider 口径；研究采集明确请求 adjusted。底层保留未自动调整 OHLC、Adj Close、行动列，keepna=True、rounding=False；不补零、不前向填充，不按复权比例调整成交量。

adjusted OHLC 由同根 `Adj Close / Close` 等比例转换；保留 ratio、provider 值和变换记录。缺失/非有限/无效 ratio 记录缺口，不能冒称已复权。repair 默认 False；显式要求或已记录异常条件才修复一次，保留修复前后 OHLC、Adj Close、行动、币种及差异。`Repaired?` 不是完整修复证据。

日线 `date` 为来源交易所时区的 session 日期，同时保留 UTC 时间和时区。本地午夜索引不是成交/收盘时间；chart regularMarketTime 不替代历史 bar 的源时间。研究 request 的 history_window.end 为闭合 session 日期，转换到 yfinance 时 end 使用下一本地自然日并留档。用户窗口优先，默认三年；不因样本不足扩窗。日内各 interval 的覆盖限制分别检查，不能把 1m/分钟/小时线同等承诺，不能送入现有日线预测器。

最后一根闭合状态须有覆盖该日期的交易日历或具体时段证据；未知时交给中央检查，不用主机时钟/固定 16:00 假定收盘。currentTradingPeriod/tradingPeriods 不构成完整三年日历；metadata 惰性读取仅限所需字段。`adjustment_evidence` 保留 source、price_basis、checked_through、实际行动与覆盖；覆盖未知的空行动不是“无行动”。今天下载的历史复权数据保留下载/修订时间，不伪造历史 vintage。

旧直连与 yfinance 的 actual_source 均为 `Yahoo Finance`；库名/版本另列，不能当独立双源。仅对同 session、币种、价格口径的独立闭合价格交叉验证；不能把 adjusted close 与其他源原始 close 直接送进 0.3% 冲突检查。

批量默认最多 10 个标的，US/HK 分开，默认串行，显式并发最多 2；限流停止新增请求。固定批量下载口径并按标签拆列，区分源缺失和日期轴对齐空位；全空标的非成功。逐标的记录 status、数量、实际窗口、源时间、错误与缺口。部分成功保留成功结果，只对已确认可重试的缺项补取，不重跑整批，不依赖库私有错误容器。

## 财报、新闻和专项映射

财报保留原始科目名、statement、period_end、frequency、value、unit/currency，以及来源确实给出的披露/可得时间。财务币种与交易币种分开；期末不是披露日期。逐表检查空表和状态，部分空表 partial，三份空表不标完整。ETF 不使用公司三表代替基金资料。

Ticker stream 按已确认结构读取 `content.pubDate`/`displayTime` 等；Search 读取 `providerPublishTime`，分别保留 ID、标题、摘要、publisher/provider、URL、发布时间与更新时间、明确证券 tags。更新时间不当发布时间；query ticker 单列，不从搜索词生成 tickers。广告排除；无可靠时间者保留缺口但不进入严格 News。contentType/tab 仅在语义确定时映射 record_type，否则 unknown；不生成情绪/相关性分。count/tab 不同的请求避免复用库不区分参数的新闻缓存。短 feed 不支持完整历史新闻窗口或全文承诺。

期权先列可用 YYYY-MM-DD 到期日，再取所需链；lastTradeDate 保留 UTC，港股可用性不靠库存在推断。基金持仓无日期明确缺失，不回填历史。筛选页上限 250，限制页数/结果数，记录 offset/total/覆盖；当前名单不是历史 universe。估值/财务/预期/持仓只作描述性资料，不自动加入 A/B/C/D。

## 会话、缓存与失败

会话在作业开始确定，复用 Ticker 和成功结果，不跨线程切换 locale/session；yfinance 全局配置/YfData 单例要求同进程统一配置。默认关闭 debug/progress，异常不隐藏。网络边界、鉴权 redirect 例外和预算见 [source-policies.md](source-policies.md)。不得归档 cookie/crumb/代理凭证；未取得 wire payload 时归档只能称 yfinance 原生整理结果，不称原始 HTTP。

首次 Yahoo 调用前将内置时区/cookie 缓存指向显式用户可写 cache-dir，缓存与快照分开，不写安装目录。无 requests_cache，不将 cookie/timezone 或内存缓存称长期业务缓存。复用已有文件保留原 fetched_at、源时间和参数指纹，复核 freshness/覆盖，不改时间掩盖过期。

错误类别：DependencyUnavailable、UnsupportedCapability、InvalidParameters、RateLimited、AccessDenied、NetworkError、InvalidResponse、DataUnavailable、InsufficientCoverage、BudgetExceeded。gateway 保留 success/empty/error 并支持 partial；全部成功、有依据无数据及可用部分成功退出 0，依赖/请求错误或核心全部失败退出 1。退出 0 不代表研究完整。网络失败不改成成功空数组；market/news 独立失败，不清空合格另一域。无隐式旧接口/网页 fallback。

## 研究衔接与兼容

[yahoo_collect.py](../scripts/yahoo_collect.py) 只采指定 market/news（默认两域），复用宿主已有 Finnhub 成功结果，只补缺域；不把 yfinance 参数塞进研究 request，不让 pipeline 隐式联网。字段与 provenance 见 [collection-adapter.md](collection-adapter.md)。仍为 schema 1.2，无新增必填字段。

方向研究仍按 Quant → 验证/审计 → News → Final Synthesis；新闻失败记 not_assessed，不能当 neutral 或改 Quant P(up)。中央 [reliability-gates.md](reliability-gates.md) 权威：复权/日历未知是降级，不自动致命；明确污染/身份/价格冲突按原 gate 处理。

**当前采集不是已审计合格预测输入的承诺。** 单标的 history 的 `source_timestamp_kind=bar_label_not_trade_time` 明示日线索引时间，不是收盘/行情更新时间；即使交易日历确认该 bar 已闭合，仍不能把此标签改写为收盘时刻或 fetched_at。该真实源时间可能触发现有 24h freshness / `source_before_close` gate；保持 gate 不变，审计失败时报告阻断而非伪造时间。来源未确认的 `unit` 留 null，不从 currency 猜补；缺 unit、calendar-backed closure、身份或覆盖时 collector 记录 partial，后续 adapter/freezer/审计仍可能拒绝。`fast_info` 报价当前不提供可靠源时间；batch 的对齐轴、空位来源、交易所身份/币种/时区及修复覆盖也不能视为已证实。只有实际数据通过既有完整验证后才可称为有效研究输入。

旧 `stock_kline_yahoo`、`yahoo_quote_summary`、`financial_statements_yahoo`、`key_statistics`、`analyst_estimates`、`institutional_holders`、`options_chain`、`stock_news` 保留原签名/返回结构，作为显式兼容调用，不作为新默认路径。特别是旧 K 线 date 仍为 UTC 日期、range_ 仍非精确 start/end，不能与新交易所日期混同。失败不自动转兼容入口。

离线 fixture/transport/管道验收与实时连通性分开报告；文档和注册表不是联网成功、依赖已验证安装或有效研究数据的证明。不含 WebSocket、登录/Premium、任意 URL/方法、长期全市场爬取或 PIT 新闻档案。