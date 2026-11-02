# 来源能力与权限矩阵（news-1）

本页是来源规划说明，不是账户授权或联网验收证明。运行 `news_research.py sources` 获取实际注册表；以本次请求回执核对真正取得的内容。**文档宣称、账户实测、本次请求结果是三个独立层次**；目前不在此宣称任何真实 US/HK 样例已经通过。

## 注册能力

| source_id / 来源 | 作用和当前接入方式 | 历史/分页与文本边界 | 权限与本次必须披露的缺口 |
|---|---|---|---|
| `sec_submissions` / SEC | US 发行人公告元数据适配器 | recent 加上 filings.files 中与窗口重叠的历史文件；不只截最近 50 条；不代表取得正文 | 使用真实 `SEC_USER_AGENT` 或 `SEC_CONTACT`；表单索引不是财务事实或重大结论正文 |
| `sec_companyfacts` / SEC | 已实现有界核心财务事实采集；需用户提供真实 SEC 身份配置。授权宿主导入亦遵守同一证据契约 | 遍历可用 us-gaap / ifrs-full 核心标签，保留 accession、filed、报告期、taxonomy、unit 和原始 JSON 摘录；财务历史独立于新闻窗口 | US GAAP 与 IFRS、10-K/10-Q/20-F/6-K 仅覆盖实际标记的事实；不拼接债务组件、不自动生成 TTM/FCF，不声称外国发行人完整覆盖；账户尚未实测 |
| `finnhub` | 日期查询的公司新闻适配器；US/HK 依账户而定 | 分日期段查询；无可靠末页/截断证明时即使 HTTP 200 仍为部分覆盖；正文/摘要由实际权限决定 | `FINNHUB_API_KEY` 由用户配置；MCP 旧 7 天返回不能证明 60 天历史或 HK 支持 |
| `fed` / 美联储 | 官方货币政策发布与历史归档适配器 | 逐年归档加窗口内原文；RSS 仅补更新，不证明历史完整；日期精度不补时刻 | 只声明已实现的货币政策主题，不自动覆盖所有美国宏观 |
| `fred` | 截止日历史版本的宏观观测适配器 | count/offset/limit；real-time 截止日固定；观测日期不等于公布日期 | `FRED_API_KEY`；系列可能受第三方许可限制；日级历史版本不能证明盘中可得 |
| `hkex` | HKEXnews / 授权 IIS 材料由宿主导入 | 日期查询和分页完整性须有宿主回执；中文/英文同公告应归并 | 公开搜索不等于自动化授权；本包不绕过登录/付费墙，不宣称无账户接口已连通 |
| `company_ir` | US/HK 公司 IR 原始发布由宿主导入 | 保存授权文本、原始链接、发布/修订时间；无任意 URL 自动抓取 | 逐条声明保存权限，公告链接不等于有全文 |
| `china_macro` / `hkma` | 中国央行/统计及香港金管局官方资料由宿主导入 | 保留查询范围、公告原日期、版本和主题 | 接入前核对端点与许可；港股有内地业务时不能忽略内地暴露 |
| `eia` | 能源数据/发布由宿主导入 | 价格、库存、产量和外部预测材料分别标注；保留观测期、频率、单位、发布日期 | 当前不是自动采集适配器；外部预测不当系统自己的预测 |
| `host_search` | 宿主授权搜索仅发现/补充材料 | 保留原始来源和检索回执；摘要/标题范围如实标注 | 搜索结果不证明全网或 60 天完整，`host_search` 记录固定为非重大发现级证据，不借搜索恢复无权保存的正文；原文须改用原始发布来源另行导入 |

`source_id` 不接受表外自创名字。新增接入应先登记历史、市场、语言、文本范围、延迟、鉴权、预算、内容许可和能力实测时间，然后再进入 request。

## 可选背景数据

[global gateway](global-stock-data.md) 与 [Yahoo 路由](yfinance-data.md) 仅提供证券身份、公司资料、财务、公告及必要行情/宏观背景。Yahoo 不是基础依赖，也不是新闻覆盖的替代物。黄金、能源、汇率和利率的观测须明确现货/期货/基准、币种、单位和日期；不使用黄金 ETF 价格冒充现货金价。分析师观点若用于解释预期，必须有可追溯发布者和时间，不输出供应商数值情绪。

## 覆盖与降级规则

每个来源/实体/主题记录请求窗口、成功与失败区间、分页游标/末页、条数上限、预算、取回/去重数量与最近成功时间。HTTP 200 但无法证明分页/截断穷尽时只记录“已查询但完整性未证明”的区间，不能当作成功覆盖或零新闻。来源不可用时其他已授权来源继续工作；403、429、超时、历史不足和内容缺失分别保存，不转换为“零新闻”。只取得 7 天或 30 天不能把 60 天请求标为完整；不足 30 天须显著提示。

无全文权限时仅保存允许的元数据/摘要及链接，并尝试通过已授权原始公告佐证。标题级材料可初筛，但不能自动进入唯一重大事件或综合主因。许可允许时目标保留 120 天；许可更短时服从并披露旧证据失效。密钥/联系方式只能在支持的环境配置中提供，日志、快照和报告不得泄露。

## 官方能力参考（不是本次实测）

- SEC [数据 API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) 与 [自动访问规则](https://www.sec.gov/about/developer-resources)。
- 美联储 [RSS](https://www.federalreserve.gov/feeds/feeds.htm) 与 [历史发布](https://www.federalreserve.gov/newsevents/pressreleases.htm)。
- FRED [历史版本](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)。
- Finnhub [company-news](https://finnhub.io/docs/api/company-news) 与 [官方客户端](https://github.com/Finnhub-Stock-API/finnhub-python)。
- HKEX [公告查询说明](https://www.hkex.com.hk/global/exchange/faq/getting-started?sc_lang=en) 与 [IIS](https://www.hkex.com.hk/Services/Market-Data-Services/Infrastructure/Issuer-Information-feed-Service-%28IIS%29?sc_lang=en)。
- EIA [开放数据](https://www.eia.gov/opendata/)。

文档链接、离线 schema 或注册表检查不能证明网页此刻可达、账户具备权限或来源没有遗漏。安装/发布报告应单独列出未实测、受限和失败项。更多保护见 [来源限制](source-policies.md)。
