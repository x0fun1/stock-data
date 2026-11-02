# Yahoo Finance / yfinance 数据路由

Yahoo 是 US/HK 股票/ETF 的可选事实背景来源，不是新闻研究的必需依赖。选择 Yahoo 后，先调用 `global_stock_data.py` 的 `yahoo_*` 路由；错误、空结果、缺依赖或能力不可用时只尝试登记的语义兼容接口。终态 `RateLimited/AccessDenied/BudgetExceeded` 或已开启作业熔断不回退。partial 不覆盖，实际来源、接口和失败原因保留。新闻采集/授权导入见 [来源矩阵](source-matrix.md)，不能以 Yahoo 行情替代新闻覆盖。

## 环境与离线发现

Python 3.10+；只有显式需要 Yahoo 且获授权时才安装 `requirements-yahoo.txt`。基础 `requirements.txt` 不引入 Yahoo。库只在调用时懒加载；导入、`--list`、`--help` 和离线新闻分析不创建 Yahoo 缓存或联网。运行时不安装依赖、不读取浏览器 cookie、不绕过登录/Premium。

适配基线为 yfinance `1.7.0` / commit `5cae563642b59f49adf6a04a5ad6744f8b0e084d`，curl_cffi `0.15.0`；SciPy 用于按需价格修复。provider 检查实际版本/后端，但依赖文件和源码阅读不证明已安装或联网验收。安装前核对执行解释器和来源，不修改其他全局环境。

在仓库根目录运行（安装后使用实际脚本路径，输入和输出放用户可写目录）：

```text
python stock-data/scripts/global_stock_data.py --list
python stock-data/scripts/global_stock_data.py --function yahoo_history --params-file history-request.json --output history-envelope.json --cache-dir runtime/yfinance-cache
```

Yahoo gateway 必须传 `--cache-dir` 或显式配置 `STOCK_DATA_YAHOO_CACHE_DIR`。仅实际调用初始化用户可写缓存，不隐式写 HOME/安装目录。示例中的 `python` 指实际运行 gateway 且具备所需依赖的解释器。

精确参数以当前 `--list` / `--help` 为准；不透传任意库参数。gateway 输出事实背景 envelope，不输出新闻研究报告，不能直接替代 import 契约。

## 按需能力表

| 函数 | 任务与参数边界 |
|---|---|
| `yahoo_history` / `yahoo_history_batch` | 明确日期/频率的必要价格背景；start 含、end 不含；逐标的保留状态 |
| `yahoo_quote` / `yahoo_profile` | 指定 symbol 和允许的报价 fields；缺源时间不称实时 |
| `yahoo_financials` | symbol、frequency、statements；年度/季度及上游支持的利润/现金流 TTM |
| `yahoo_search` | query、count、lookup_type；名称/代码和资产类型确认，不提供排名筛选 |
| `yahoo_statistics` | symbol、valuation；当前财务/估值背景，不回填历史 |
| `yahoo_analysis` | symbol、modules；外部 EPS/营收预期、评级/观点，需来源与日期 |
| `yahoo_holders` | symbol、modules；披露持仓/内部人事实，不等于即时交易 |
| `yahoo_earnings` | symbol、modules、limit；日历/披露结果，计划与已发生分开 |
| `yahoo_funds` | symbol、modules；基金说明/费用/持仓/行业权重，缺日期明示 |

## 当前 gateway 参数

以实际 `--list` 为准。history 支持 symbol、start/end 或 period、interval、price_basis、prepost、repair、timeout；batch 支持 symbols 与受限 concurrency。quote fields 和 modules 必须来自 provider 的固定允许表，不允许任意方法透传。

analysis 模块包括 earnings_estimate/revenue_estimate/eps_trend/eps_revisions/growth_estimates/target_prices/recommendations/upgrades_downgrades；这些是外部观点，不是系统自己的价格预测。holders 默认 major；earnings 默认 calendar；funds 默认 description/overview。Search 的 lookup_type 支持 all/equity/etf，实际缺库能力时返回明确错误。只取任务必要字段并保留逐模块缺口。

## 行情、日期与复权

`yahoo_history` 用 symbol、start/end 或 period、interval、price_basis、prepost、repair、timeout。日期窗口与 period 互斥。默认 provider 口径；显式 adjusted 时必须保留比例及原值，不作为新闻研究的前置门槛。底层不填零、不前向填充，也不按价格比例调整成交量。

adjusted OHLC 由同根 `Adj Close / Close` 等比例转换；保留 ratio、provider 值和变换记录。缺失/非有限/无效 ratio 记录缺口，不能冒称已复权。repair 默认 False；显式要求或已记录异常条件才修复一次，保留修复前后 OHLC、Adj Close、行动、币种及差异。`Repaired?` 不是完整修复证据。

日线 `date` 使用来源交易所时区并保留 UTC 时间。午夜索引不是成交/收盘时刻，chart regularMarketTime 不替代历史 bar 时间。价格背景仅按明确需要的区间获取，不设固定一年窗口；news request 的 30～60 天窗口只约束新闻查询。

最后一根是否闭合需来源时间/时段证据；未知时说明，不用固定时钟补造。metadata 和空行动列表不证明完整日历或“没有公司行动”。下载时修订历史不能冒充原始历史版本。

旧直连和 yfinance 同属 `Yahoo Finance`，库版本另列，不算独立双源。比较价格只使用相同日期、币种及价格口径；不要混合 adjusted close 与原始 close。

批量默认最多 10 个标的，US/HK 分开，默认串行，显式并发最多 2；限流停止新增请求。固定批量下载口径并按标签拆列，区分源缺失和日期轴对齐空位；全空标的非成功。逐标的记录 status、数量、实际窗口、源时间、错误与缺口。部分成功保留成功结果，只对已确认可重试的缺项补取，不重跑整批，不依赖库私有错误容器。

## 财报、新闻和专项映射

财报保留原始科目名、statement、period_end、frequency、value、unit/currency，以及来源确实给出的披露/可得时间。财务币种与交易币种分开；期末不是披露日期。逐表检查空表和状态，部分空表 partial，三份空表不标完整。ETF 不使用公司三表代替基金资料。

本 gateway 不提供 Yahoo 新闻历史。新闻使用 [来源矩阵](source-matrix.md) 的已授权适配器或宿主导入，按 [collection-adapter.md](collection-adapter.md) 进入冻结证据；不是仅限某个 MCP，也不把旧端点测试当作本次账户连通性证明。

基金持仓缺日期必须说明，不回填历史；财务、估值、预期和披露持仓仅作有来源与时间的事实/观点背景，不生成技术、情绪、交易或排名结果。

## 会话、缓存与失败

会话在作业开始确定，复用 Ticker 和成功结果，不跨线程切换 locale/session；yfinance 全局配置/YfData 单例要求同进程统一配置。默认关闭 debug/progress，异常不隐藏。网络边界、鉴权 redirect 例外和预算见 [source-policies.md](source-policies.md)。不得归档 cookie/crumb/代理凭证；未取得 wire payload 时归档只能称 yfinance 原生整理结果，不称原始 HTTP。

首次 Yahoo 调用前将内置时区/cookie 缓存指向显式用户可写 cache-dir，缓存与快照分开，不写安装目录。无 requests_cache，不将 cookie/timezone 或内存缓存称长期业务缓存。复用已有文件保留原 fetched_at、源时间和参数指纹，复核 freshness/覆盖，不改时间掩盖过期。

错误类别：DependencyUnavailable、UnsupportedCapability、InvalidParameters、RateLimited、AccessDenied、NetworkError、InvalidResponse、DataUnavailable、InsufficientCoverage、BudgetExceeded、NoLegacyRoute、LegacyFallbackFailed。gateway 保留 success/empty/error 并支持 partial；全部成功、有依据无数据及可用部分成功退出 0，依赖/请求错误或核心全部失败退出 1。退出 0 不代表研究完整。网络失败不会包装成成功空数组；market 域失败不清空已成功的另一域。兼容旧接口只在 yfinance 失败/空结果时自动调用，不调用网页 fallback。

终态传输失败（RateLimited、AccessDenied、预算耗尽、超界/无效响应）会 latch 为**进程级 Yahoo 作业熔断**：同一进程内后续的 yfinance 调用与旧兼容回退都直接拒绝，直到进程结束；不能靠新建 provider 或改走旧接口绕过已打开的熔断。每次旧回退尝试按一次作业请求计入同一预算（回退内部 HTTP 不在该 transport 边界内，不逐请求计数）；失败/空结果之外的非终态原因才允许一次语义等价回退。

## 研究衔接与兼容

Yahoo 结果是可选背景，不触发新闻流程隐式联网，也不作为新闻充分性的价格门槛。需要作为财务/暴露证据时按 [collection-adapter.md](collection-adapter.md) 明确映射源身份、披露时间和单位；不能直接将任意 envelope 宣称为已校验新闻输入。

`bar_label_not_trade_time` 是日线索引，不是收盘/行情更新时间；未确认的 unit 留空，不从 currency 猜补。`fast_info` 缺可靠源时间、批量日期轴/空位/身份、币种/时区或修复覆盖不明时应保留限制。

背景兼容路由包括 `stock_kline_yahoo`、`yahoo_quote_summary`、`financial_statements_yahoo`、`key_statistics`、`analyst_estimates`、`institutional_holders`。`yahoo_search` 回退到 `stock_search` 时实际来源是 Eastmoney search，必须明确披露。旧 history range_ 较粗且日期语义不同，归一化结果仍可能 partial，不能据此承诺完整区间。

离线 fixture/transport/CLI 检查与真实连通性分开报告；本页不证明已安装依赖、已连通账户或通过真实 US/HK 验收。不提供 WebSocket、登录/Premium 绕过、任意 URL/方法透传或长期全市场爬取。
