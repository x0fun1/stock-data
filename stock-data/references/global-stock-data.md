# global-stock-data 运行与来源说明

`stock_kline_yahoo(..., include_metadata=true)` 保留 chart meta、events 和 adjclose；默认仍为 candles 列表。metadata 不证明完整交易日历或复权口径。News 保留 article ID/相关 ticker tags，不能从查询词伪造文章关联。HTTP 固定来源、同 origin redirect、20 MiB 上限；可用 `--output <new-file>` 避免全量 stdout。

global 是本包自带的 Python 事实背景实现，覆盖美股和港股。执行代码位于 [global_stock_data.py](../scripts/global_stock_data.py)，不从文档拼接代码片段。仅保留证券身份、资料、财务、公告、宏观与必要行情；新闻研究入口是 [news-first-workflow.md](news-first-workflow.md)。Yahoo `yahoo_*` 先试可选 yfinance，再在权限和预算允许时使用登记的语义兼容接口。

## 运行

需要 Python 3.10+；基本接口使用 `requests`，Yahoo yfinance 为 `requirements-yahoo.txt` 中的可选依赖，仅在调用时懒加载。请求失败、空结果或依赖/能力不可用时仅尝试有登记的安全兼容路由；终态限流/权限/预算错误不绕过。`--list` 不访问网络或创建 Yahoo 缓存。下面命令在 `stock-data/` 目录运行；其他目录使用脚本实际路径。

```text
python scripts/global_stock_data.py --list
python scripts/global_stock_data.py --function us_stock_quote_sina --params-file request.json
```

参数文件为 UTF-8 JSON 对象。例如行情请求：

```json
{"ticker":"AAPL"}
```

也可用 `--params-json` 传同一对象，或 `--params-file -` 从标准输入读取。直接导入模块不会发网络请求。依赖缺失时使用已有可用执行环境；安装依赖遵循用户授权和当前环境规则。没有技术指标或交易信号参数入口。

CLI 的 JSON 外层包含 `status`、`source`、`function`、`fetched_at_utc` 与 `data`；失败时包含 `error_type`、`error_message`。取数时间不代表行情时间。新 Yahoo 入口可返回 `partial`，缺失明细必须可见；退出 0 不意味着研究覆盖完整。`empty` 须有无数据依据，嵌套全空和网络失败不能包装成成功。除强制的 yfinance→旧兼容路由外，脚本不自动切换来源；也不保证接口持续可用。

## 市场代码

| 来源 | 美股 | 港股 |
|---|---|---|
| Finnhub / 新浪美股 / 腾讯美股 | 来源使用的 ticker，例如 `AAPL` | 不走此美股路由 |
| 腾讯 / 新浪港股行情 | — | 五位代码，例如 `00700` |
| Yahoo | ticker，例如 `AAPL` | 来源格式，例如 `0700.HK` |
| 东财 push2 / push2his | ticker + `secid_prefix`：105、106 或107；先查 `stock_search` 的 `mkt_num` | 五位代码 + `secid_prefix=116` |
| 东财 datacenter | `secucode`，例如 `AAPL.O`、`BABA.N`；按来源搜索结果确认市场 | `00700.HK` |
| SEC | 十位 CIK；先用 `ticker_to_cik` | 本包无此路由 |

不要仅凭代码猜东财市场前缀，也不要将跨市场代码互换。`stock_search` 当前筛选美股/港股市场编号，名称中的 global 不代表覆盖全部交易所。

## 函数与来源路由

查看 `--list` 取得当前精确签名；下表只说明必要背景能力，不保证来源当前连通。按任务、已取得证据、来源权限和字段语义选择，避免重复拉取成功结果。新闻历史查询使用 [来源矩阵](source-matrix.md)，不由行情 gateway 代替。选 Yahoo 时遵循 [yfinance-data.md](yfinance-data.md) 的可选依赖与受限回退。

| 能力 | 来源及函数 | 参数 / 边界 |
|---|---|---|
| 美股行情 | `us_stock_quote_sina`、`us_stock_quote_tencent`、`stock_quote_eastmoney` | 核对 ticker、源时间及东财市场前缀 |
| 港股行情 | `hk_stock_quote_tencent`、`hk_stock_quote_sina`、`stock_quote_eastmoney` | 来源代码格式；缺时间不称实时 |
| 必要价格背景 | `us_stock_kline_sina`、`yahoo_history`、`yahoo_history_batch` | 明确日期/频率/币种/口径；新闻不要求价格输入 |
| 报价/公司资料 | `yahoo_quote`、`yahoo_profile` | 当前资料不是历史身份或可得性证明 |
| 财报/财务指标 | `financial_statements_eastmoney`、`key_indicators_eastmoney`、`yahoo_financials`、`yahoo_statistics` | 报告期、披露日、单位、币种和会计口径必须核对 |
| 外部观点/披露持仓 | `yahoo_analysis`、`yahoo_holders` | 观点需发布者/时间；持仓不是实时成交 |
| 个股 SEC 文件 | `sec_filings` | 此背景接口只读 recent 且截取最近 50 条；完整新闻回填使用 `sec_submissions` 适配器 |
| SEC XBRL 事实 | `sec_xbrl_facts` | 此背景接口仅 us-gaap 的 10-K/10-Q；不声称 IFRS/20-F/6-K 全覆盖 |
| 身份检索 | `stock_search`、`yahoo_search`、`ticker_to_cik` | 搜索确认来源代码，不能猜 CIK 或跨市场代码 |
| ETF 资料 | `yahoo_funds` | 持仓/行业权重须有日期，缺项明示 |
| SEC 申报/全文 | `daily_filings`、`fulltext_search` | 原始索引和搜索命中不等于已解析正文/财务结论 |
| 财务背景比较 | `market_frame` | 指定标签、单位和期间；不是全市场完整覆盖或排名入口 |
| 宏观观测 | `treasury_yield_curve`、`cftc_cot` | 保留报告日/观测期，不当作价格方向信号 |
| 财报日历 | `earnings_calendar`、`yahoo_earnings` | 计划、预期与已公布结果分开 |

有多个备选源时，前一来源满足请求就停止。网络/权限/限流错误与确实没有数据分开；不将 403 当作不存在，也不通过不同身份或接口绕过限制。

## Yahoo 背景兼容接口

`yahoo_*` CLI 需显式 `--cache-dir runtime/yfinance-cache` 或 `STOCK_DATA_YAHOO_CACHE_DIR`；缓存不写安装目录。来源时间保留真实 bar-label 语义，未确认 unit 留空，不伪造收盘或披露时间。

`stock_kline_yahoo`、`yahoo_quote_summary`、`financial_statements_yahoo`、`key_statistics`、`analyst_estimates`、`institutional_holders` 保留事实背景接口。旧 K 线 range_ 较粗；新 history 使用明确日期并保留源时间。两种接口同属 Yahoo Finance，不是独立双源；原始新闻证据走新闻采集/导入流程。

## 数据口径与覆盖

- 所有行情、财务及宏观观测保留实际来源和源时间；缺失时注明，并另列取回时间，不把缓存或历史价格称为实时成交。
- 无效数值为 `None`，真实 0 与负数保留；不能凭零价格计算变化。
- 可选行情保留 provider OHLC、Adj Close、行动与口径；缺值不补零，修复默认关闭且须保留差异证据。行情不是新闻准入条件。
- 不混合复权口径、交易时段、币种和单位。Finnhub 的 USD millions 与其他来源原始金额不可直接拼接。
- 东财 datacenter 默认页数有限，缺页不得称完整；不同财报源未统一会计科目，必须核对。
- SEC facts 的 start/end、unit、accn、fy/fp、filed 区分报告期、口径与修订；不能把期末当发布时间。
- TTM 避免单季与累计季度重复相加；亏损时 PE 不适用，PS 注明年度/TTM与市值日期，FCF 明确现金流减资本开支的公式和符号。

授权、联系信息、限速与来源使用边界见 [来源限制](source-policies.md)。
