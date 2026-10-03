# global-stock-data 运行与来源说明

global 是本包自带的 Python 回退实现，覆盖美股和港股。执行代码位于 [global_stock_data.py](../scripts/global_stock_data.py)，不再从文档拼接代码片段。函数保留原有能力；各函数只查询一个来源，来源间回退由调用者按下表逐项执行。

## 运行

需要 Python 3.10+ 与 `requests`。先用 `--list` 检查函数、参数和来源；它不访问网络。下面命令在 `stock-data/` 目录运行；其他目录使用脚本实际路径。

```text
python scripts/global_stock_data.py --list
python scripts/global_stock_data.py --function us_stock_quote_sina --params-file request.json
python scripts/global_stock_data.py --function calc_rsi --params-file candles.json
```

参数文件为 UTF-8 JSON 对象。例如行情请求：

```json
{"ticker":"AAPL"}
```

也可用 `--params-json` 传同一对象，或 `--params-file -` 从标准输入读取。技术指标参数是 `{"klines":[...], ...}`，每根 K 线使用函数要求的 OHLCV 键。直接导入模块也可复用函数，不会在导入时发网络请求。依赖缺失时使用已有可用执行环境；安装依赖遵循用户授权和当前环境规则。

CLI 的 JSON 外层包含 `status`、`source`、`function`、`fetched_at_utc` 与 `data`；失败时包含 `error_type`、`error_message`。取数时间不代表行情时间。`empty` 表示顶层无数据，嵌套空列表或部分空值仍须结合请求检查。脚本不自动切换数据源、不保证接口持续可用。

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

查看 `--list` 取得当前精确签名；下表给出常用参数与适用边界。

| 能力 | 来源及函数顺序 | 参数 / 边界 |
|---|---|---|
| 美股行情 | 新浪 `us_stock_quote_sina` → 腾讯 `us_stock_quote_tencent` → 东财 `stock_quote_eastmoney` | `ticker`；新浪 `timestamp` 不带时区，不能据此单称实时；东财另需 `secid_prefix` |
| 港股行情 | 腾讯 `hk_stock_quote_tencent` → 新浪 `hk_stock_quote_sina` → 东财 `stock_quote_eastmoney` | `code`；东财 `ticker_or_code` +116 |
| 美股 K 线 | 新浪 `us_stock_kline_sina` → Yahoo `stock_kline_yahoo` | 新浪 `num`；Yahoo `symbol, interval, range_`；匹配用户期间和粒度 |
| 港股 K 线 | Yahoo `stock_kline_yahoo` | 如 `symbol="0700.HK"`；本包没有第二个港股 K 线来源 |
| MA / MACD / RSI / KDJ / BOLL | 本地 `calc_ma`、`calc_macd`、`calc_rsi`、`calc_kdj`、`calc_boll` | `klines`；按参数准备足够历史与必需字段 |
| 公司资料 | Yahoo `yahoo_quote_summary` | `symbol, modules=["assetProfile"]`；空资料不推算 |
| 中文三表 | 东财 `financial_statements_eastmoney` | `secucode, statement=balance/income/cashflow, page_size`；按 REPORT_DATE 分组 |
| 结构化三表 | Yahoo `financial_statements_yahoo` | `symbol, quarterly=false/true`；检查三表实际是否齐全 |
| 中文财务指标 | 东财 `key_indicators_eastmoney` | `secucode, page_size` |
| 英文指标 / 分析师 / 持仓 | Yahoo `key_statistics`、`analyst_estimates`、`institutional_holders` | `symbol`；预期与事实分开 |
| 日级资金流 | 东财 `fund_flow_daily` | `ticker_or_code, secid_prefix, limit`；来源分档口径不等于可确认的投资者身份 |
| 美股期权链、希腊字母 | 已授权 CBOE `options_chain_cboe` → Yahoo `options_chain` | `ticker` / `symbol, expiration`；Yahoo 无等价希腊字母 |
| 期权筛选与摘要 | 本地 `parse_osi`、`filter_expiry`、`unusual_activity`、`chain_summary` | 解析合约、到期日或相对成交量；不能确定开平仓或交易方向 |
| CBOE 标的快照 | 已授权 CBOE `cboe_quote` | `ticker`；此能力不替代常规行情首选顺序 |
| 个股 SEC 文件 | EDGAR `sec_filings` | `cik, form_type`；当前 submissions 的 recent 列表，不保证完整历史 |
| SEC XBRL 指标 | EDGAR `sec_xbrl_facts` | `cik, metrics`；当前读取 us-gaap 的 10-K/10-Q facts；保留原始单位与各披露记录，不是完整三表，也不覆盖全部会计标准/申报类型 |
| 代码搜索 | 东财 `stock_search` | `keyword, count`；无已实现的 Yahoo 代码搜索回退 |
| 新闻 | Yahoo `stock_news` | `keyword, count`；搜索结果不保证完整日期覆盖或 Finnhub 情绪分 |
| ticker → CIK | EDGAR `ticker_to_cik` | `ticker`；无匹配时返回空，不猜 CIK |
| 市场列表 | 东财 `market_stock_list` | `market=us_nasdaq/us_nyse/us_etf/hk, page, page_size, sort_field, sort_desc`；须分页 |
| 空头成交量 | FINRA `short_volume_all`、`short_volume_symbol`、`short_volume_ranking` | `date` / `symbol, days, market`；指定报告市场，并非空头持仓量 |
| 全市场申报索引 | EDGAR `daily_filings` | `date, forms`；按 CIK 筛出目标公司；索引不解析 Form 4 成交明细 |
| 申报全文搜索 | EDGAR `fulltext_search` | `query, forms, date_from, date_to, limit`；匹配不是已确认的财务结论 |
| 财务横截面 | EDGAR `market_frame`、`frame_ranking`、`frame_screen` | `tag, year, quarter, unit`；frame 有覆盖/口径差异，不能代表全部上市公司 |
| 美债曲线 / COT | Treasury `treasury_yield_curve` / CFTC `cftc_cot` | `year` / `limit, market_contains`；按来源报告日解读 |
| 财报日历 | Nasdaq `earnings_calendar` | `date=YYYY-MM-DD`；单日查询，全市场记录须按目标 symbol 筛选；没有等价港股日历 |

有多个备选源时，前一来源满足请求就停止。网络/权限/限流错误与确实没有数据分开；不将 403 当作不存在，也不通过不同身份或接口绕过限制。

## 数据口径与覆盖

- 所有行情和派生指标保留来源及行情时间。部分源没有返回时间；此时注明“源未提供”，并另列取数时间。不要将缓存、历史收盘或 K 线最后一根称为实时成交。
- 无效数值转换为 `None`，真实 0 与负数保留。来源自己给出的零仍需结合字段语义判断，不凭价格为零计算收益。
- Yahoo K 线保留 `timestamp_epoch`、`timestamp_utc` 和原始精度；`date` 是 UTC 日期。需要交易所交易日口径时根据其时区核对，不依赖运行主机时区。缺失 OHLCV 不补零；指标函数要求统一时间键且严格升序。Finnhub candles 使用前将字段映射为 date/close 等计算输入，并保留原始时间。
- 指标算法沿用原实现：RSI 使用窗口内涨跌的简单平均（不是 Wilder 平滑），EMA 从首个收盘值起算，MACD 柱值为 DIF 与 DEA 差的两倍，布林带使用总体标准差。比较其他平台时先确认这些参数和口径；窗口不足的输出不能作为成熟指标。
- 不混合未声明的复权口径、交易时段或币种。成交量/成交额和市值按来源单位展示；Finnhub 的 USD millions 与 Yahoo 原始金额不可直接拼接。
- 东财市场列表保留原始价格及精度字段。当前缩放协议未经确认时，归一化价格返回不可用；不得用原始值当作显示价格或做价格筛选。服务端排序不意味着价格单位已核实。
- 东财 datacenter 和市场列表默认只取一页；检查页数、`total`、所需报告科目与时间范围，缺页不得声称完整。两种财报来源没有统一会计科目映射。
- SEC facts 的 start/end、unit、frame、accn、fy/fp、filed 用于区分期间、口径与重述。筛选同口径记录后再比较，不简单合并同一 end 的值。
- FINRA 空头成交量是所选场所/文件中的成交统计，不等于市场完整空头量、空头持仓量或投资者净做空。
- 期权 volume/OI 只反映成交量相对持仓量；`volume_weighted_delta_proxy_shares` 是无交易方向的代理值，不是净 delta 暴露。CBOE/Yahoo 缺字段不能互相补造。

授权、联系信息、限速与来源使用边界见 [来源限制](source-policies.md)。
