# 数据来源方案（US/HK，免费源）· 2026-11-05

> **2026-11-07 小时级实测更新：** 已用现有Tiingo账户取得NVDA/SPY长小时OHLCV，并以同源分钟线核验小时聚合。下文“分钟历史仅当日/多年分钟历史未解决”等是旧探测结论，不能替代本次小时数据验收。最新可用区间、成交量口径与质量限制见[小时数据就绪度](HOURLY_DATA_READINESS_2026-11.md)。

> **2026-11-06 分析阶段更正：** Census 新密钥已通过数值验证。此前“45 个适配器通过，因此数据拿得准”的结论不成立：部分成功项只是目录、文件头或复合接口的局部成功，并非训练就绪。本文保留历史探测记录，分析准入以 [数据就绪度核对](ANALYSIS_DATA_READINESS_2026-11.md) 为准；预测与验证方法见 [量化分析设计](QUANT_ANALYSIS_DESIGN_2026-11.md)。不应再依据旧记录声称已获得全部正文、完整历史或已验证概率。

本页是**实测记录**，不是清单抄录。每条结论都对应 `runtime/probe/` 下的原始证据；没有实测过的写“未验证”，推测不写成事实。

- 证据文件：`runtime/probe/out/probes.jsonl` … `probes8.jsonl`（共 8 轮）、`run*.txt`（可读表格）、`terms.txt`、`runtime/probe/dl/terms/*.txt`（条款全文抓取）。
- 探针脚本：`runtime/probe/scripts/probe.py` … `probe8.py`、`terms_sweep*.py`、`apply_round2_findings.py`（只读 GET/POST，TLS 校验开启，不用任何密钥）。
- 机器可读版本：`docs/source-registry.json`。
- 本次共探测 101 + 52 + 23 + 12 + 38 + 28 + 10 + 17 = **281 个端点**（含 30 余个 robots.txt）。第 5–8 轮的端点清单由 `simonlin1212/global-stock-data` 提供，但每条都在本机**重测**过，不采信它的结论。**HTTP 200 不等于可用**：`klines: []`、`data:null`、`{"code":1,"msg":"bad params"}`、`"Service not valid"` 都出现过，因此探针按“载荷是否像真数据”判定。

---

## 1. 结论速览

| 层次 | 来源 | 判定 | 依据 |
|---|---|---|---|
| 美国日线 | 新浪美股 kline | **采用（首选）** | 1984-09-07 起全历史；**含退市股**（ATVI 从 1993-10-25 起） |
| 美国/香港日线 | 东方财富 push2his | **采用（并列）** | AAPL 10,624 根、0700 5,518 根；`fqt=0` 与新浪交叉核对仅 2/10,067 天差 0.01 |
| 香港日线 | 腾讯 kline | **采用（备源）** | 未复权日线 18KB；行内附带分红/回购注记 |
| 美国/香港实时与当日分时 | 东方财富 quote + trends2 + klt=1 | **采用** | 报价字段 f43/f44/f45/f46/f47/f48/f60/f169/f170 齐全 |
| 美国基本面 | SEC XBRL / 申报 | **采用（唯一权威）** | companyfacts、companyconcept、frames、submissions、full-text、季度数据集、N-PORT、Form 4、13F 全部 200 |
| 香港基本面 | 东方财富 HK F10 主要指标 | **采用（有限）** | 89 字段、32 页历史；**但只有报告期，无公告日 → 不可做 point-in-time** |
| 美国卖空 | FINRA consolidatedShortInterest API | **采用** | 无密钥返回真实数据（98KB / 可按 symbol 过滤） |
| 事件（美） | SEC 8-K / Form 4 / 13F / N-PORT / 全文检索 | **采用** | 与既有结论一致 |
| 事件（港） | 无免费授权源 | **缺口** | HKEXnews 条款禁止自动化；只能由你提供授权材料 |
| 新闻发现 | GDELT 2.0 doc API | **采用** | 条款最宽松；须 ≥5 秒/次，否则 429 |
| 宏观 | FRED/ALFRED、Treasury、CFTC、BLS、Census、World Bank、OECD、Ken French | **采用** | 均 200；FRED 需密钥 |
| 宏观（港） | HK 政府统计处 API | **采用** | robots 明确允许，且官方发布 `data/SKILL.MD`、`API_Guide` |
| ETF 持仓 | SEC N-PORT + 发行人文件 | **采用** | SSGA XLSX 54KB、iShares AJAX 400KB+ 可达 |
| **波动率历史（VIX 全期限）** | CBOE 指数 CSV | **采用（C 级，仅个人研究）** | VIX 1990-01-02 起 474KB；VIX9D/3M/6M/1Y、VVIX、SKEW、OVX、GVZ、VXN、RVX 全部 200 |
| **央行政策利率与美元利率** | BIS + 纽约联储 + 美联储 H.10/H.6 | **采用（S 级，可商用）** | BIS 单国 1.5KB / 多国 10KB；SOFR 3.97%、EFFR 3.88%（2026-11-03）实测；H.10 含港元、H.6 含 M1/M2 |
| **能源与劳动市场高频** | EIA + 劳工部 ETA + BLS | **采用（S 级）** | 原油/天然气周库存 200；失业金申领 POST 得 18.8KB XML（2026-01-03 起）；BLS 免 key 每日 25 次上限 |
| **港股指数与南向资金** | 东财 `100.HSI` / `m:124` / `kamt` | **采用（灰色）** | 恒指 1990-05-14 起 9,001 根；345 个港股指数（含恒指波幅 VHSI）；南向序列有数、北向读 0 |
| **除权/拆股台账** | 美股改用 Tiingo；港股不取（你的批复） | **美股采用 / 港股回到缺口** | Yahoo 实测能取（AAPL 39 次派息 + 2020-08-31 4:1；0700 19 次 + 2014 年 5:1）但遵批复港股不取；美股改用 Tiingo 的 `divCash`/`splitFactor` |
| **分析师预期与机构持仓** | 美股：Finnhub / 待定；港股：不取 | **港股回到缺口** | Yahoo 实测港股也能取（0700.HK 7 强买 / 32 买 / 3 持有 / 1 卖出）但遵批复不取；美股改用 Finnhub（个人使用） |
| **美股做空余额（全市场）** | FINRA 双周 CSV | **采用（B 级）** | `shrt20261015.csv`、`shrt20260930.csv` 均 600KB+（竖线分隔）；未发布日期返回 403 |
| SEC（今日状态） | — | **已跑通** | 正确路径下 `data.sec.gov` 与 `www.sec.gov/Archives` 均返回 200；此前的 403 是路径写错触发的 Akamai "Access Denied"，不是 IP 级限流（见 §9） |
| Nasdaq 非公开 API | — | **否决** | `api.nasdaq.com/robots.txt` = `Disallow: /`；且历史接口只返回 2 条 |
| Stooq / Yahoo | — | **否决 / 采用（D 级）** | Stooq 仍返回 “enable javascript”；Yahoo chart 实测可用（美股/港股/指数/汇率，日线须用有界区间）但条款禁止自动抓取，v7/v10 需 cookie+crumb，详见 §2.5 与 §8 |

---

## 2. 分学科清单（含调用配方）

### 2.1 量价

| 用途 | 端点 | 配方要点（实测） |
|---|---|---|
| 美股日线 | `https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK?symbol=AAPL` | 返回 `[{d,o,h,l,c,v,a}]`；`jsonp_v2.php/var _x=/` 前缀版同源可用；`quotes.sina.cn` 那个 host 已失效（`Service not valid`） |
| 美股/港股日线 | `https://push2his.eastmoney.com/api/qt/stock/kline/get` | **必须带 `beg`/`end`**：省略或用 `lmt` 单独取会返回 `rc:102, data:null`。全历史用 `beg=0&end=20500101`；`secid=105.AAPL` / `116.00700`；`fqt=0` 取原始价 |
| 分钟线 | 同上 `klt=1` | 美、港均可用（香港 23KB）。`klt=15/30/60` 返回空数组；`klt=5` 亦空 |
| 周线 | 同上 `klt=102` | 美、港均可用（1.1KB/1.1KB） |
| 港股日线备源 | `https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=hk00700,day,,,100` | 未复权；行内 `FHcontent`/`HGcontent` 带分红/回购文字注记 |
| 复权因子探测器 | `…/appstock/app/hkfqkline/get?param=hk00700,day,,,100,qfq` | 20KB；只作拆股/派息因子交叉核对，**不要当价格基础**（见既有结论：`fqt=1` 出现负价） |
| 实时/当日分时 | `https://push2.eastmoney.com/api/qt/stock/get?secid=…` 、`…/stock/trends2/get?ndays=1` | quote 230 字节；trends 美 31KB / 港 27KB |
| 美股实时快照 | `https://hq.sinajs.cn/list=gb_aapl,gb_nvda` | 需 `Referer: https://finance.sina.com.cn` |
| 期权（仅当前延迟链） | `https://cdn.cboe.com/api/global/delayed_quotes/options/AAPL.json` | AAPL 400KB+、SPX 300KB+；`historical_data/` 只是快照（VIX/SPX 年高），**无历史** |
| 汇率 | `https://api.frankfurter.app/2025-01-01..2025-11-05?from=USD&to=HKD` | 6KB，无密钥 |

未解决：**港股退市股**（`secid=116.02888` 返回 null，未找到可靠代码表）；**美/港分钟级历史**（免费源只有当日）。

### 2.2 基本面

- 美国：`data.sec.gov` 全套（`api/xbrl/companyfacts`、`companyconcept`、`frames`、`submissions`）、`www.sec.gov/Archives/…`、`daily-index`、`efts.sec.gov/LATEST/search-index`、`files/dera/data/financial-statement-data-sets/<quarter>.zip`。
  - **UA 必须是可识别字符串并带联系方式**：用无联系方式的 UA 时 `cgi-bin/browse-edgar` 返回 **503**；换成 `stock-data-research/0.1 (contact: github.com/x0fun1)` 后同一端点 **200**（18.5KB）。
  - Point-in-time 锚点是 `filed`/accession 日期。ml4t 的做法可以借鉴：从 `submissions` 取 filing date 表并 join 到 frames 事实（见其 `data/equities/fundamentals/xbrl_download.py`）。
  - 附带发现：`www.sec.gov/robots.txt` 在本次探测量下返回 **403 “Request Rate Threshold Exceeded”** → 说明 SEC 有阈值，采集必须节流（官方要求 ≤10 请求/秒 + 声明式 UA）。
- 香港：`https://datacenter.eastmoney.com/securities/api/data/get?type=RPT_HKF10_FN_MAININDICATOR&…&filter=(SECUCODE="00700.HK")`
  - 实测 89 字段（营收/毛利/净利/ROE/ROA/资产负债率/经营现金流/DPS_HKD/股息率/股本/PE/PB/币种/财年），32 页历史；**GBK 编码**，按 utf-8 解码会出现乱码。
  - 字段里只有 `STD_REPORT_DATE`、`REPORT_DATE`、`START_DATE`，**没有公告日期** → 结论：可做当期分析，**不可做 point-in-time**。这与既有“东财无披露日”的结论一致。
  - 其它猜名（`RPT_HKF10_FN_INCOMESTATEMENT` / `_BALANCESHEET` / `_CASHFLOWSTATEMENT` / `_BONUSFINANCING` / `_DIVIDEND` / `_INFO_HOLDING` / `_INFO_EXECUTIVE` / `_INFO_INDEX`）全部返回 `报表配置不存在`；只有 `RPT_HKF10_INFO_ORGPROFILE`（2.7KB 公司概况）可用。

### 2.3 事件驱动

- 美国：SEC（8-K 正文在 EX-99.1，需走 `index.json` 枚举；Form 4；13F；N-PORT；daily index；full-text search）。FINRA `https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest`（无密钥，字段含 `currentShortPositionQuantity`、`previousShortPositionQuantity`、`averageDailyVolumeQuantity`、`accountingYearMonthNumber`；支持 `compareFilters=issueSymbolIdentifier==AAPL`）与 `cdn.finra.org/equity/regsho/daily/CNMSshvol<YYYYMMDD>.txt`（376KB）。
- 香港：**无**。HKEXnews 条款禁止自动化（既有结论不变），CCASS/DI 同属 HKEX 体系；只能由你导入授权材料。

### 2.4 新闻与文本

- GDELT `https://api.gdeltproject.org/api/v2/doc/doc?query=…&mode=artlist&format=json`：返回 `url/title/seendate/domain/language/sourcecountry`，覆盖中文与港澳来源；**限速实测**：并发或快速连发即 429（“limit requests to one every 5 seconds”），间隔 ≥15 秒后 200（1.4KB，真实文章）。条款页明确“unlimited and unrestricted use … Redistributing GDELT 允许，须引用”。
- 官方 RSS/页面：`federalreserve.gov/feeds/press_monetary.xml`（9.7KB）、`hkma.gov.hk/.../press-releases/`（119KB）、`prnewswire.com/rss/news-releases-list.rss`（41KB）均 200。
- 历史文本语料（用于事件研究校准，非实时）：HuggingFace `Zihan1004/FNSPID`（1999–2023）、`danidanou/Bloomberg_Financial_News`（2006–2013）。
- 现有 Finnhub 新闻（有密钥时）：**条款规定不得再分发数据或派生结果**，只能自用。

### 2.5 宏观

已实测 200：Treasury 收益率 CSV、`api.fiscaldata.treasury.gov`、`publicreporting.cftc.gov`（Socrata）、`api.bls.gov`（v1/v2 均可）、`api.census.gov`、`api.worldbank.org`、`sdmx.oecd.org`（dataflow 55KB）、`mba.tuck.dartmouth.edu`（Ken French zip 178KB）、`api.frankfurter.app`。

- FRED/ALFRED：无密钥返回 `{"error_code":400,…"Variable api_key is not set"}` → **必须密钥**。版本链走 `fred/series/vintagedates` + `realtime_start/end`（ml4t 的 ALFRED 脚本即此配方）。
- 香港：`censtatd.gov.hk` API 存在、robots 明确允许，且官方提供 `data/SKILL.MD`（3.7KB）与 `API_Guide`（58KB）——面向 agent 的接口文档。参数需按其指南填写（我猜的参数返回 `Validation error`）。
- HKMA `api.hkma.gov.hk` 今天三种写法全部 **405**（返回阿里 CDN 错误页）；与既有“HKMA 开放 API 可用”的记录冲突 → **本次记为未复现，需换网络环境复测**。
- ECB `data-api.ecb.europa.eu`：本机 TLS 链校验失败（`unable to get local issuer certificate`）→ 当前环境不可用。
- DBnomics：两种 URL 形式均 404，未解决。

**第 5–8 轮新增（全部实测，零密钥）**：

| 用途 | 端点 | 实测 |
|---|---|---|
| 央行政策利率（38 家） | `https://stats.bis.org/api/v1/data/WS_CBPOL/D.US/all?format=csv&lastNObservations=5` | 200，1,475B；多国用 `D.US+JP+GB+CN` → 10,315B；香港基准利率 616B。key = `D.` + 地区，`format=csv` 必带 |
| 美元参考利率 | `https://markets.newyorkfed.org/api/rates/{secured\|unsecured\|all}/{rate}/last/10.json`、`…/search.json?startDate=&endDate=` | SOFR 2,398B（3.97%，含 1/25/75/99 分位与 3,260bn 成交量）；EFFR 6,527B（3.88%，附目标区间 3.75–）；SOFRAI 893B（30/90/180 天均值 + 指数 1.26564788） |
| 美元对 23 种货币 / M1·M2 | `https://www.federalreserve.gov/datadownload/Output.aspx?rel=H10\|H6&series=<hash>&lastobs=6&filetype=csv&layout=seriescolumn&type=package` | H.10 2,925B（含港元）；H.6 4,564B（M1/M2 及构成）。`lastobs>5000` 返回空文件 |
| 原油/天然气周库存 | `https://ir.eia.gov/wpsr/table1.csv`、`https://ir.eia.gov/ngs/wngsr.json` | 6,594B（2026-10-23 当周原油 691.944 百万桶，周环比 −1.264）；7,198B（发布日 2026-Oct-29） |
| WTI/布伦特/亨利港现货 | `https://www.eia.gov/dnav/pet/hist_xls/RWTCd.xls` | 472KB 的**老式 .xls**（1986 起），需 xls 解析器；B 级（数据源为 Refinitiv） |
| 周度失业金申领 | POST `https://oui.doleta.gov/unemploy/wkclaims/report.asp`，表单 `level=us&strtdate=&enddate=&filetype=xml` | 18,854B XML，根节点 `r539cyNational`，2026-01-03 当周 NSA 298,700 / SA 207,000 / SA4WK 212,250。**GET 只返回 HTML 表单页**，必须 POST |
| BLS 系列 | `https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0` | 3,290B（2026-09 CPI）。免 key 每日 25 次、每次 25 序列、10 年；UA 不带邮箱一律 403；`bls.gov/.../bls.ics` 发布日程 403 |
| VIX 及全期限波动率 | `https://cdn.cboe.com/api/global/us_indices/daily_prices/{IDX}_History.csv` | VIX 474KB（1990 起）、SKEW 204KB（1990 起）、VVIX 109KB（2006）、VIX1Y 254KB（2007）、VIX6M 242KB（2008）、VIX3M/OVX/GVZ/VXN/RVX（2009）、VIX9D 202KB（2011）、VIX1D 56KB（2022）。**前导下划线版 `_VIX_History.csv` 是 403**，正确写法是 `VIX_History.csv` |
| 全市场做空余额 | `https://cdn.finra.org/equity/otcmarket/biweekly/shrt<YYYYMMDD>.csv` | `shrt20261015.csv`、`shrt20260930.csv` 均 600KB+，竖线分隔带表头（含 `daysToCoverQuantity`）；`20261030/1031/1113` 返回 403 = 尚未发布，不是路径错。**纠正上一版"双周文件路径 403"的结论：错在扩展名与结算日历** |
| RegSHO 分市场日文件 | `https://cdn.finra.org/equity/regsho/daily/{CNMS\|FNSQ\|FNYX}shvol<YYYYMMDD>.txt` | CNMS 536KB、FNSQ 509KB、FNYX 254KB，表头 `Date\|Symbol\|ShortVolume\|ShortExemptVolume\|TotalVolume\|Market` |
| 恒生指数历史 | `https://push2his.eastmoney.com/api/qt/stock/kline/get?...&secid=100.HSI&klt=101&fqt=0&beg=0&end=20500101` | 200，9,001 根，1990-05-14 起；报价 24,796.98 与新浪 `hq.sinajs.cn/list=int_hangseng` **逐位一致**（交叉验证通过） |
| 港股指数全集 | `https://push2.eastmoney.com/api/qt/clist/get?...&fs=m:124` | 200，345 个指数，含 **VHSI 恒指波幅指数**（港股版的 VIX） |
| 南向资金 | `https://push2.eastmoney.com/api/qt/kamt/get`、`…/kamt.kline/get`、`…/kamt.rtmin/get` | 200（southbound 序列有值）；北向 `hk2sh/hk2sz` 的 `dayNetAmtIn` 恒为 0，与 2024 年 8 月内地停止披露北向实时净买入一致。字段口径无官方说明，**须先做语义核对再入分析** |
| 美/港资金流 | `https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?...&secid=105.AAPL\|116.00700` | US 7,389B / HK 7,584B，逐日主力与大中小单净流入；默认窗口约 100 行 |
| 美/港全市场快照 | `https://push2.eastmoney.com/api/qt/clist/get?fs=m:116+t:3`（港股股票 2,620）/ `fs=m:105,m:106,m:107`（美股 13,899） | 港股**必须带 `+t:3`**：不带时 `m:116` 返回 17,465 行，大部分是窝轮/牛熊证 |
| 港股除权/拆股 | `https://query1.finance.yahoo.com/v8/finance/chart/0700.HK?range=max&interval=1d&events=div%2Csplit` | 0700：19 次派息（最早 2008-05-06，0.032）+ 2014-05-15 的 5:1 拆股；AAPL 10 年 39 次派息 + 2020-08-31 的 4:1。恒指（^HSI）正确返回 0 派息 0 拆股 |
| 分析师预期/机构持仓/财报日历 | `https://query2.finance.yahoo.com/v10/finance/quoteSummary/0700.HK?modules=...&crumb=<crumb>` | 200，13,634B。**crumb 必须先取 cookie**：先 GET `https://fc.yahoo.com/`（404 但种下 cookie），再 `v1/test/getcrumb`；跳过播种就 401 "Invalid Crumb"。HK 期权链返回 0 个到期日（港股无期权链） |

---

## 3. 条款与 robots 状态（实测原文）

| 源 | robots/条款 | 原文或状态 |
|---|---|---|
| GDELT | 允许 | “available for unlimited and unrestricted use for any academic, commercial, or governmental use of any kind without fee”；再分发须引用 |
| 十二数据 Twelve Data | 仅个人/内部 | 禁止 “(h) Use automated tools to exceed API Rate Limits”、“(g) Store or cache Data beyond permitted timeframes”、“(l) Use Free Tier data for commercial purposes” |
| FMP | 仅个人非商业 | “This license may only be used by a Customer who is an individual, and strictly for their own personal, non-business and non-commercial purposes” |
| Finnhub | 仅个人 | “you agree to not redistribute or share access to data or derived results … with anyone or any 3rd party without written approval” |
| 东方财富 | **API host 禁止** | `push2.eastmoney.com/robots.txt` 与 `push2his.eastmoney.com/robots.txt` = `User-agent: * / Disallow: /`；而 `www.eastmoney.com` = `Allow: /`。站点条款页为 JS 壳，未取到正文 |
| 新浪 | API host 无规则 | `www.sina.com.cn` = `Allow: /`；`stock.finance.sina.com.cn/robots.txt` 返回 HTML 页面（无规则）；`finance.sina.com.cn` 返回空文件 |
| 腾讯 | 无规则 | `qt.gtimg.cn/robots.txt` 返回 “bad request”；`web.ifzq.gtimg.cn` 返回 “No dispatch info found” |
| Nasdaq | **禁止** | `api.nasdaq.com/robots.txt`：`Disallow: /`（注释写明“disallows all web crawlers … to prevent search engine indexing of API endpoints”） |
| CBOE | 未取到 | `cdn.cboe.com/robots.txt` = S3 `AccessDenied`（无该文件）；`cboe.com/legal` 正文无相关条款 → **未验证** |
| Tiingo | 未取到 | `/about/terms` 404、`/terms` 403、about 页 JS 壳 → **未验证** |
| Alpha Vantage | 未取到 | 条款页为 JS 渲染，全文无关键词 → **未验证** |
| Polygon | 需确认身份 | `polygon.io/terms` 返回 1.4KB 占位页，提到 “Massive for Individuals / Businesses”（疑似改名），条款正文未取到 |
| Alpaca / EODHD / FINRA 条款页 | 未取到 | 文档站/Cloudflare 403 |
| 纽约联储 | 允许，须附声明 | "The New York Fed grants you a non-exclusive license… to use, copy, and distribute Content for your **personal or business purposes**… through an **automated process**"；对外展示须附其规定声明（方括号处填名称） |
| BIS | 允许，须引用 | "The use of the statistics is **unrestricted**, provided that: if the statistics are reproduced, the BIS must be **cited** in your publication or product as the source of the statistics" |
| 美联储理事会 | 公有领域 | "information on Board's website is in the **public domain** and may be copied and distributed without permission. Please **cite to the Board**" |
| EIA（自有数据） | 公有领域 | "U.S. government publications are in the **public domain** and are not subject to copyright protection"；⚠️ 现货价不是 EIA 自有数据（Refinitiv 授权，B 级） |
| 劳工部 ETA | 公有领域 | "Materials created by the federal government are generally part of the **public domain** and may be used, reproduced and distributed without permission" |
| BLS | 公有领域 + 硬限制 | 免 key 版**每天 25 次请求 / 每次 25 个序列 / 10 年**；"Users may not modify or falsely represent content…"；不带联系方式的 UA 一律 403 |
| FINRA（双周做空余额与 RegSHO） | **B 级** | 文件属主动公开发布；但站点条款禁 "data mining, scraping or harvesting tools (including robots)"，并声明 "FINRA Data provides **non-commercial use** of data" |
| CBOE（期权链与波动率指数） | **C 级，须事先授权** | 使用任何 Cboe Content 须 "receive **approval in advance** from Cboe" 并 "**execution of a license agreement**"；条款**不区分**商用/非商用、实时/延时 |
| Yahoo Finance（港股 K 线/除权拆股/分析师） | **D 级，禁止自动抓取** | 禁 "access or collect data… using **any automated means**… **for any purpose without our express, prior permission**" |
| 官方源 | 公共数据 | SEC、FRED、Treasury、CFTC、BLS、Census、World Bank、OECD、Ken French、HK 统计处 robots 允许（FRED robots 仅 `Crawl-delay: 2`） |

**推论（需你确认）**：价格层的东财 API host 属于 `Disallow: /`。既有旧包文档里也写着“公开网页不自动意味着允许自动化；个人研究不等于已授权；再分发须先确认许可”。这是一条需要你拍板的边界，不是我能替你决定的事。

---

## 4. 免费源覆盖不了的缺口（写清楚，不用“以后再说”带过）

1. **港股企业行动台账**：**回到缺口**（你的批复：港股不从 Yahoo 取）。原先靠 Yahoo 的 `events=div,split`；现在只剩腾讯行内注记与东财 F10 的 `DPS_HKD`，只能当探测器。
2. **港股 point-in-time 基本面**：**永久缺口**（你无法提供授权材料）。东财 F10 无公告日；除非将来购买授权源，否则港股不做 PIT。
3. **港股退市股**：**永久缺口**。未找到可用代码表；Yahoo 亦被排除，此路不再考虑。
4. **分钟级历史**：**部分解决**。Yahoo `interval=1m` 返回约 2,000 根覆盖最近 5 个交易日，但 Yahoo 已被你的批复排除在港股路径之外，所以港股分钟线只剩东财的当日分时；多年分钟历史仍无免费源。
5. **期权历史**：**未解决**。CBOE 延时链可拿当前隐含波动率与未平仓量（C 级，须事先授权）；无免费历史链。
6. **分析师一致预期**：**港股回到缺口，美股由 Finnhub 覆盖（个人使用）**。Yahoo 能取港股（0700.HK 实测 7 强买 / 32 买 / 3 持有 / 1 卖出）但按你的批复不取。
7. **港股除权/拆股台账**：**回到缺口**。原先靠 Yahoo 的 `events=div,split`，按你的批复不再取；只剩腾讯行内注记与东财 F10 的 `DPS_HKD` 作探测器，不能当台账。
8. **凭证与限流**：密钥已由你提供（`docs/api.txt`，已被 .gitignore 排除，采集层**就地读取、不复制、不落盘**）；SEC 联系方式仍建议补一个邮箱形式的 `Company Name AdminContact@domain.com`。SEC 侧对合规 UA 的 403 已查明是**路径写错**（见 §9），正确路径下 `data.sec.gov` 与 `www.sec.gov/Archives` 均返回 200；预算仍按远低于 10 req/s 留出并对限流退避。
---

## 5. 需要你决定的三件事

1. **价格层合法性边界**：继续用东财/新浪/腾讯（免费但东财 API host 明确 `Disallow`，属灰色），还是改用有密钥的免费额度供应商（Tiingo / Polygon / FMP / Twelve Data，条款为“个人非商业”，但只覆盖美股），或是你提供授权行情（港股几乎只有这条路干净）。
批：继续用东财/新浪/腾讯，但是我还增加了一个额外的免费的供应商的密钥tiingo
2. **要不要申请免费密钥**：一个美股供应商密钥可同时补上**除权日（ex-date）**、拆股、财报日历与备源价格——这正是缺口 #1 里美股侧的堵口方式（SEC 只给金额不给除权日）。
批：看看 Tiingo 里面有没有
3. **港股授权材料**：你能否提供港股年报/中报/公告原文用于导入？港股侧的事件驱动与 point-in-time 基本面完全依赖这件事。
批：这个我提供不了。
4. **要不要用 Yahoo（D 级）**：它现在是**唯一**免费拿到港股除权/拆股日、港股分析师预期的来源，而条款明禁"任何自动化手段、任何目的、未经事先书面许可"。要么接受"仅个人研究"，要么把这两项也列回缺口。
批：我们就是个人研究啊。如果雅虎禁用了自动化手段获取这个港股相关的信息，那我们就不从雅虎获取港股相关的信息。

**批复与落地（2026-11-05，按你的批注执行）**

| 决定 | 你的批复 | 落地动作 |
|---|---|---|
| 价格层合法性 | 继续用东财/新浪/腾讯，并新增 Tiingo 密钥 | 灰色层保留并继续标注；Tiingo 作为**美股第二个价格源**接入（`tiingo_eod_actions`），条款为个人非商业 |
| 美股除权/拆股/财报日历 | “看看 Tiingo 里面有没有” | 已实测并接入，结果见 §9 |
| 港股授权材料 | 提供不了 | 港股 point-in-time 基本面、港股事件驱动**永久列为缺口**（除非将来购买授权源），不再作为待办反复提出 |
| Yahoo | 个人研究，但**港股不从雅虎取** | Yahoo 从**所有港股路径移除**（K 线、除权/拆股、分析师预期）；港股这两项回到缺口。美股侧也不接入默认链，仅保留为“最后备源”记录 |

---

## 6. 六个仓库的取舍

| # | 仓库 | 能用吗 | 具体用法 / 判定依据 |
|---|---|---|---|
| 1 | awesome-quant | **当索引，不当依赖** | 30486 星、2026-11-05 仍在更新，但它只是链接表，本身没有数据。有价值条目：OpenBB、pandas-datareader、pystlouisfed、edgartools、akshare、FinanceToolkit、commitments-of-traders、`itch-book`（免费 emi.nasdaq.com ITCH 样本）。注意：列表近期混入大量 2026 年新登记、描述奇特的仓库，只能当线索不能当背书 |
| 2 | machine-learning-for-trading（第 3 版） | **最有用** | 六者里唯一带“免费源地图 + 下载脚本 + dataset card”的仓库：`data/` 覆盖 SEC XBRL/申报、FRED+ALFRED 版本链、Ken French + AQR 因子、CFTC 持仓、FINRA 卖空、Nasdaq Data Link WIKI（1962–2018、含退市）、Binance/CoinGecko/DefiLlama、Kalshi/Polymarket、HuggingFace 新闻语料、OANDA 汇率，付费项（Databento/AlgoSeek）也标注清楚。其 `pyproject` 显示已用 `yfinance`+`edgartools`+`secedgar`，**并且弃用了 zipline**，改用自研 `ml4t-backtest`。用法：抄端点与 PIT 处理，不抄代码 |
| 3 | quant-trading | 仅策略参考 | 11,199 星、Apache-2.0、2026-06 有更新。根目录就是一堆 `* backtest.py`（配对交易、统计套利、MACD、布林带、Dual Thrust、London Breakout、期权跨式、VIX 计算）加一个 `data/` 目录，**不提供数据源**。等我们要补策略库时再回来看 |
| 4 | backtrader | 不上 | 23,667 星，但最后推送 2024-08-19，处于停更状态。它是回测框架不是数据源；自带 feed 为 IB/Oanda（实盘，需本地环境）+ CSV/pandas。若将来要引擎，优先 vectorbt / backtesting.py / ml4t-backtest |
| 5 | zipline | 不上（上游已死） | 最后推送 2024-02-13；README 仍写 “supports Python 2.7, 3.5, and 3.6”，数据靠 `zipline ingest` 下载 Quandl/Quantopian 托管数据（该托管随 Quantopian 2020 年关停已不可得）。要用事件驱动回测只能走 zipline-reloaded，且数据必须由我们按本页配方自行打包 |
| 6 | ccxt | 本次不用 | 44,746 星、MIT、活跃，但只覆盖 100+ 加密交易所与预测市场，超出已定的 US/HK 股票范围；并且本机访问 Binance 公共 API 返回 **HTTP 451**（地域限制），CoinGecko 可用。若哪天纳入加密资产再启用 |

**一句话**：这六个仓库里没有一个能提供数据——数据问题得靠上面第 1–5 节的实测端点解决；它们能提供的是索引（1）与配方（2），框架层面（4/5）已被本领域自身的发展淘汰，加密（6）不在范围内。

---

## 7. 下一步（建议顺序）

1. 你回答第 5 节三个决定；同时重新提供 SEC 联系方式与 FRED 密钥（Finnhub 若仍要用新闻）。
2. 按 `docs/source-registry.json` 把已采用源做成采集层（每源一个 adapter，统一 receipt：请求窗口、成功区间、失败原因、条数）。
3. 补齐 SEC 侧的 PIT 事实组装（frames + submissions 的 filing date join），作为美股基本面的唯一权威路径。
4. 港股先只做“当期分析 + 授权材料导入”两条路，不假装有 PIT。
5. 复测 HKMA 与 ECB（换网络环境），并把 GDELT 的 ≥5 秒节流写进 transport。

---

## 8. 追加两个仓库的评估（2026-11-05 晚间）

`global-stock-data` **能直接用**（它给的是"端点地图 + 条款原文"，不是代码）；`Vibe-Trading` 在数据来源上**没有新增任何我们没测过的接口**。

### 8.1 simonlin1212/global-stock-data — 本轮的端点来源，可用

- **形态**：纯文档仓库（README + 4,323 行 SKILL.md + CHANGELOG），1,840 星，2026-11-02 有推送。把 17 层 / 50+ 端点 / 18 个源编成 **S/B/C/D 四级合规**表，每级**引条款原文**。没有可安装的代码。
- **价值**：把官方源一层从我们已有的 Treasury/CFTC/BLS/Census/OECD 扩到了 **BIS 38 家央行政策利率、纽约联储 SOFR/EFFR/SOFRAI、美联储 H.10（含港元）/H.6（M1·M2）、EIA 周度油储与气储、劳工部 ETA 周度申领（1967 起）、CBOE 全期限波动率指数历史**，并给出 **CBOE 的条款原文**（此前只能写"未验证"）。本轮 34 个新端点里 30 个可用，全部记入 `docs/source-registry.json`。
- **它写错、我们实测纠正的两处**：
  1. 它称"东财 push2his kline 实测不返回美股/港股数据（2026-05-20），仅支持 A 股"。**错**：AAPL 10,624 根、0700 5,518 根我们都取到了。它多半漏了 `beg`/`end`——漏掉时恰好返回 `rc:102, data:null`，正是我们第一轮踩过的坑。
  2. 它称"港股 K 线只有 Yahoo，全历史日线"。**半错**：Yahoo 港股 `range=max` 返回的是**月采样**却仍标成日线（0700.HK 22 年只有 270 根），要日线必须用有界区间（`range=10y` → 2,461 根）。它自己的 CHANGELOG 记过这个坑，但描述里仍写"全历史"。
- **不照抄的地方**：它把东财/新浪/腾讯列为 C 级"仅供个人研究"，与我们实测的 `push2his` robots `Disallow: /` 一致；D 级的 Yahoo/Nasdaq 它照样提供代码，文档里明说"用不用由你按条款自行判断"。这与我们"写清边界、由你拍板"的做法一致，但它默认给代码、我们默认不给。

### 8.2 HKUDS/Vibe-Trading — 数据层没有新增，仅作架构参考

- **形态**：37,694 星、活跃（2026-11-05 推送）、约 94MB 的 agent + 回测 + 89 个 skill 平台。
- **数据层**：`backtest/loaders/*` 的 **28 源自动回退链**（tushare / akshare / yfinance / gildata / baostock / pykrx / mt5 / ccxt / okx / 巴西 B3 系）+ OpenBB Workspace 桥。**这些源里没有一个是本轮新发现的**；A 股/巴西/加密部分不在 US/HK 范围内；Gildata、IWENCAI 是付费 token。它的 `get_stock_profile`（US/HK 分析师预期与机构持仓）走的正是我们刚验证的 Yahoo quoteSummary + crumb；`get_southbound_flow` 走的是我们刚测的东财 `kamt` 系列。
- **可借鉴的三点（是架构，不是数据）**：
  1. **市场→加载器回退链**：用代码后缀路由（`_SOURCE_PATTERNS`）到首选加载器，不可用时按 `FALLBACK_CHAINS` 降级。我们的采集层正需要"一个市场一条链、失败自动降级"的显式结构。
  2. **代码与单位口径**：它明确做了港股代码归一化（`700.HK` → `0700.HK`）与成交量单位（港股为"股"，2026-08-11 用 00700 核对比率 1.00）——与我们口径表的方向一致，可核对我们的归一化规则。
  3. `openbb_bridge/adapter.py` 是把自有数据接进 OpenBB Workspace 的适配层，将来要出图表/仪表盘可直接复用其协议实现。
- **判定**：**不作为数据源**，作为架构参考。

### 8.3 一句话

`global-stock-data` 帮我们把"官方可商用层"补齐，并且它的条款原文让我们能把 CBOE 从"未验证"升级为"C 级：须事先授权"；`Vibe-Trading` 提醒我们采集层该有的形状，但它的数据接口对我们没有增量。

---

## 9. 声明来源跑通实测（2026-11-05）

这一节回答你的要求：**先把已声明的数据源全部跑通，证明真的取得到数据**，之后才谈分析。
每个来源一个适配器，逐源落一份回执与一份载荷样本；密钥就地读取 `docs/api.txt`，回执与样本里的密钥一律以 `***` 脱敏。

```bash
python -I runtime/live/run_smoke.py --tag r4                        # 全量
python -I runtime/live/run_smoke.py --tag r5 --only eia_energy,sec_form4_13f
python -I runtime/live/run_smoke.py --tag r6 --only sec_form4_13f  # 修 URL 后复跑
```

回执：`runtime/live/receipts/smoke-r*.jsonl`；载荷样本：`runtime/live/samples/<id>.txt`（含失败时的原始错误体）。
每个来源在下面表格里的数字都是**最后一轮实测**留下的，不是估计值。

采集层的适配器 id 与 §1–§8 注册表条目的 id 不一一对应：注册表按“来源”记一条，采集层有时会把一条拆成多个适配器（例如 `sec_primary_data` 拆成 XBRL / submissions / ticker map / 全文检索 / Form 4 五个）。

**最终结果：45 个来源跑通，1 个未跑通（缺 Census key），另有 14 项按条款或范围特意不调用。**

未跑通：`census_api`（no shape returned rows without a key -> 2022/acs/acs5?get=NA: MISSING ）

### 9.1 价格与行情

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `cboe_vol_history` | 200 | 13618 | VIX 01/02/1990..11/04/2026 rows=9309 ; VIX3M 09/18/2009..11/04/2026 rows=4309 |
| `em_hsi_kline` | 200 | 9001 | 恒生指数 dktotal=9001 1990-05-14..2026-11-05 |
| `em_kline_1m_us_hk` | 200 | 2 | 苹果:390 bars 腾讯控股:330 bars |
| `em_kline_hk` | 200 | 5518 | 腾讯控股 dktotal=5518 2004-06-16..2026-11-05 |
| `em_kline_us` | 200 | 10624 | 苹果 dktotal=10624 1984-09-07..2026-11-04 |
| `em_quote_and_trends` | 200 | 391 | 0700 quote=430.400 (f59=3) vs kline close=430.400 drift=0.00% ; AAPL intraday 391 points |
| `sina_hk_index` | 200 | 1 | var hq_str_int_hangseng="恒生指数,24796.98,877.48,3.67"; |
| `sina_us_daily` | 200 | 10068 | first=1984-09-07 last=2026-11-04 close=349.51 |
| `sina_us_delisted` | 200 | 6293 | ATVI 1993-10-25..2023-10-12 (delisted 2023) |
| `sina_us_snapshot` | 200 | 2 | var hq_str_gb_aapl="苹果,349.5100,0.00,2026-11-05 22:21:58,0.0000,0.0000,0.0000,0.0000,351.7 |
| `tencent_hk_kline` | 200 | 320 | 2025-07-22..2026-11-05 rows=320 annot=True |
| `tiingo_eod_actions` | 200 | 714 | rows=714 divCash rows=11 split rows=0 ; keys=adjClose,adjHigh,adjLow,adjOpen,adjVolume,close,date,divCash,high |
| `tiingo_meta_news_fx_fund` | 200 | 7 | ok=[eod_meta,fund_daily,fund_statements,fund_defs,fx_top,fx_hist,iex] miss=[fund_meta(http=404),news(http=403),news_hk(http=403)] fund_daily: [{"date":"2026-01- |

### 9.2 公司基本面与申报

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `em_hk_f10` | 200 | 8 | rows=8 fields=89 has_announce_date=False; report=2026-06-30 00:00:00 |
| `sec_ticker_map` | 200 | 10409 | companies=10409 AAPL CIK=320193 |
| `sec_xbrl` | 200 | 25507 | entityName=Apple Inc. us-gaap facts=25507 |

### 9.3 事件与申报细节

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `sec_form4_13f` | 200 | 4 | Form 4 filings in recent=601 ; index items=4 |
| `sec_fulltext_and_daily` | 200 | 428 | full-text hits=799 ; daily index lines=6654 8-K=428 |
| `sec_submissions` | 200 | 1003 | recent=1003 8-K=101 latest=424B2 2026-11-05 |

### 9.4 持仓与做空定位

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `cftc_positioning` | 200 | 3 | rows=3 market=GULF # 6 FUEL OIL CRACK - NEW YORK MERCA date=2026-10-27T00:00:00.000 |
| `em_fund_flow` | 200 | 2 | AAPL:60 0700:60 ; last row 2026-11-05,1587345616.0,-484438800.0,127 |
| `em_hk_connect` | 200 | 20 | sh2hk dayNetAmtIn=4200000.0 ; s2n series 20 days, last=2026-11-05,0.00,10400000.00,525188835.75 |
| `etf_holdings` | 200 | 1 | iShares IVV=fail(1652 lines) SPDR SPY=ok(54474 bytes) |
| `finra_biweekly_short` | 200 | 22537 | file 20261015 rows=22537 cols=14 |
| `finra_regsho_daily` | 200 | 12305 | file 20261104 rows=12305 header=Date/Symbol/ShortVolume/ShortExemptVolum |

### 9.5 新闻与舆情

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `finnhub_news_and_calendar` | 200 | 248 | AAPL news=247 earnings_calendar=1 ; 0700.HK earnings={'error': "You don't have access to this resource."} |
| `gdelt_doc` | 200 | 25 | articles=25 languages={'Korean', 'Chinese', 'English'} newest=20261104T013000Z |
| `official_rss` | 200 | 65 | fed=15 hkma=30 prn=20 |

### 9.6 宏观与利率

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `bis_policy_rates` | 200 | 6 | areas=['HK', 'US'] rows=6 |
| `bls_api` | 200 | 35 | CUUR0000SA0 2026 September = 336.940 |
| `census_api` | 200 | 0 | no shape returned rows without a key -> 2022/acs/acs5?get=NA: MISSING KEY / 2020/dec/pl?get=NAME: MISSING KEY / 2019/pep/population?: MISSING KEY |
| `dol_weekly_claims` | 200 | 52 | weeks=52 last filled 10/10/2026 initial NSA=191,797 SA=200,000 (newest week 12/26/2026 is a placeholder) |
| `eia_energy` | 200 | 1 | WPSR week="691.944" rows=58 ; WNGSR WITHHELD http=403 (EIA release window) |
| `fed_board_h10_h6` | 200 | 10 | H10 lines=11 HKD at col [9] ; H6 4384 bytes |
| `frankfurter_fx` | 200 | 25 | USDHKD 2026-11-04 = 7.8458 |
| `fred_and_alfred` | 200 | 5 | CPIAUCSL 2026-09-01=336.298 ; vintages=5 oldest=2026-06-10 ; as-first-published 1947-01-01=21.48 |
| `hk_censtatd` | 200 | 1 | header.status=Success; keys=['header', 'dataSet']; payload=2090 bytes |
| `nyfed_rates` | 200 | 5 | SOFR 2026-11-04 = 3.94% volume=3207bn |
| `oecd_sdmx` | 200 | 10 | format=json-structure-2.0.0 dataflows=10 e.g. DSD_KEI@DF_KEI |
| `treasury_rates` | 200 | 212 | days=212 header=Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr"," |
| `worldbank_api` | 200 | 6 | rows=6 e.g. Hong Kong SAR, China 2024 = 408368682415.423 |

### 9.7 商品

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `eia_spot_prices` | 200 | 1 | xls ok, 472064 bytes (needs an xls reader; WTI from 1986) |

### 9.8 期权与衍生品

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `cboe_delayed_options` | 200 | 3526 | contracts=3526 first=AAPL261104C00275000 iv=0.000 oi=43.0 |

### 9.9 因子

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `ken_french_factors` | 200 | 1 | zip ok, 178326 bytes |

### 9.10 筛选与股票池

| 来源 id | HTTP | 行/条 | 实测返回 |
|---|---|---|---|
| `em_hk_index_list` | 200 | 345 | total=345 e.g. 恒指波幅指数 / 恒生科技指期货2倍杠杆指数 / 恒生科指股息累计2倍杠杆指数 / 恒生科指2倍杠杆指数 / 恒生国指波幅指数 |
| `em_screen_hk_us` | 200 | 40 | HK universe=2620, US universe=13899 |

### 9.11 跑通之前先修掉的缺陷（全部有实测证据）

| 缺陷 | 现象 | 处置 |
|---|---|---|
| **东财报价标度** | `f43` 没按载荷自带的 `f59`(=3) 换算，00700 被读成 4304.00（实为 430.400） | 改为 `f43 / 10**f59`，并用东财自己的 K 线收盘价交叉校验：`quote=430.400 vs kline close=430.400 drift=0.00%` |
| **SEC Archives 403** | 路径写成 `<无横线accession>-index.json`（该文件不存在），Akamai 回 "Access Denied" 403，前几轮误判为 IP 限流 | 改为 `<无横线accession>/index.json`（200，555 字节）与 `<无横线accession>/<带横线accession>-index.htm`（200，8066 字节） |
| **FINRA 做空 API** | 两个数据集都冻结：`consolidatedShortInterest` 全停在 2020-04-15，`equityShortInterest` 停在 2018-09-14，且 `compareFilters` 被忽略 | 弃用该 API；现货数据改由双周监管文件覆盖（`20261015`，22537 行 × 14 列，另有 RegSHO 日文件 12305 行） |
| **GDELT 查询语法** | `OR` 词没加括号，返回 "Queries containing OR'd terms must be surrounded by ()"；且本机被限流的时长远超它声明的 5 秒 | 查询改为 `(Tencent OR "Hong Kong") earnings`，首次调用前留 20 秒、重试 30/60 秒 → 25 篇，语言含英/中/韩 |
| **DOL 周度失业金** | 返回 52 周，但最新一周是占位行（数值为空），取末行会得到空值 | 取最后一个有数的周：10/10/2026，NSA=191,797 / SA=200,000 |
| **美联储 H.10** | `Time Period` 不在 CSV 第 0 行，形状判定误报失败 | 改为全行扫描：H.10 正常，港币在第 9 列；H.6 同步通过 |
| **香港统计处** | 返回体本来是好的（`header.status=Success`），是我方判定条件写错 | 按 JSON 结构判定 → 通过（2090 字节，GDP 平减指数表） |
| **Census API** | 2025 年起所有数据端点都要求 key；无 key 一律返回 8000+ 字节的 "Missing Key" HTML，HTTP 仍是 200 | 本轮判为**未跑通**：需要一把免费 key，登记为 adopt_limited，不计入已证明可用的来源 |
| **iShares 持仓** | Ajax CSV 路径现在返回 2.3MB 的产品网页（试了三种 URL 形态都一样） | 该路由不可用；ETF 持仓改由 SPDR（SPY xlsx，54KB）+ SEC N-PORT 覆盖 |
| **EIA 天然气库存** | 发布窗口外返回 403 "Access Restricted ... outside of a release window"（同一源的石油表 WPSR 正常） | 按发布窗口缺省处理，不再算失败（WPSR 58 行照常可用） |

### 9.12 按条款与范围不调用的来源

| 来源 | 不调用的原因 |
|---|---|
| `hkex_announcements` | HKEX terms forbid automated access - user decision stands |
| `nasdaq_undocumented_api` | rejected: robots Disallow: / |
| `finra_short_interest_api` | rejected: every FINRA API dataset is frozen (consolidatedShortInterest at 2020-04-15, equityShortInterest at 2018-09-14) and compareFilters is ignored; the live route is the biweekly regulatory file |
| `stooq` | rejected: returns "enable javascript" |
| `yahoo_*` | user ruling: do not take HK data from Yahoo (terms ban automated collection) |
| `yahoo_us_analyst` | US only via Yahoo, not wired: Tiingo/finnhub cover US, kept as last resort |
| `dbnomics` | unverified: both URL shapes 404 |
| `ecb_sdw` | unverified: local TLS chain failure |
| `hkma_api` | unverified: 405/503 from this host, retested here only if reachable |
| `historical_news_corpora` | HuggingFace corpora are bulk downloads, not a live interface |
| `keyed_price_vendors` | only Tiingo was chosen; other vendor keys not provided |
| `coingecko` | out of scope: crypto, US/HK only |
| `binance` | out of scope: crypto, and HTTP 451 from this network |
| `cboe_terms_note` | documentation record, nothing to call |

### 9.13 仍未证实的来源（本轮复测）

| 注册表 id | 本轮结果 | 实测 |
|---|---|---|
| `hkma_api` | 仍不可用（TLS 握手被服务端中断） | 三个 URL 形态全部失败：`SSL: UNEXPECTED_EOF_WHILE_READING`，与此前记录的 405/503 一致；HKMA 的新闻稿走官方 RSS 是通的（30 条），港股宏观另有 BIS 与统计处覆盖 |
| `ecb_sdw` | 仍不可用（本机证书链缺中间证书） | `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`；不使用跳过校验的方式规避（安全策略），欧元区利率可由 fred_alfred 直接覆盖，故不阻塞 |
| `dbnomics` | 可达但序列路径未解开 | 返回的是 JSON 错误而非阻断：`/v22/series/USA/FRED/DGS10?observations=1` → 404（回显 dataset_code=FRED），`/v22/series?q=DGS10` → 400 invalid value；与 fred_alfred 功能重复，暂不接 |

### 9.14 结论：哪些能力现在真的有数据

- **美股**：日线（新浪 10068 根 / 东财 10624 根，含退市股 ATVI 6293 根）、原始价 + 除权拆股台账（Tiingo EOD：`close` 为原始价，`divCash` 命中 11 行，2020-08-31 `splitFactor=4.0`）、当日分时（东财 390 点）与盘口快照（新浪）、财报口径基本面（SEC XBRL 25507 个 facts，PIT 锚为 `filed`）、Form 4 / 13F 申报（SEC Archives，最近 601 份 Form 4）、做空定位（FINRA 双周 22537 行 + RegSHO 日 12305 行）、新闻与财报日历（Finnhub 247 条 + 1 条日历；GDELT 25 篇）、期权延时链（CBOE 3526 张合约）。
- **港股**：日线（东财 5518 根 / 腾讯 320 根并带行内派息注记）、当日分时（东财 330 点）、恒指 9001 根与 345 个指数、南向资金 20 日、东财 F10（8 期 × 89 字段，**无公告日，不能当 PIT**）、港股宏观（BIS 政策利率、统计处 GDP 表、金管局新闻稿 30 条）。
- **仍然没有免费源**：港股除权/拆股台账、港股 point-in-time 基本面、港股事件驱动正文、港股分析师一致预期、多年分钟历史、期权历史链（逐项见 §4）。
- **凭证额度边界**：Tiingo 免费额度给 EOD、外汇、`fundamentals/daily`（估值比率）与 `fundamentals/statements`（季度三大表），但 `news` 是 403（付费档），也没有财报日历产品；Finnhub 免费额度给美股新闻与美股财报日历，港股财报日历要付费。
- **一个需要说明的口径**：所有价格与行情都是**原始价**，除权拆股单独走台账（Tiingo 的 `divCash`/`splitFactor`），不使用任何厂商的复权序列。
