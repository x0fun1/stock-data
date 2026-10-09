# stock-data yfinance 验收记录

工作区：`/data/stock-data-skill`。验收日期：2026-10-10（Asia/Shanghai；2026-10-09 UTC）。当前 Skill 版本：2.5.0；下方首段实时联网记录为升级前 2.4.0 主路由验收。

## 验收结果

在 Python 3.14.6 的 `/tmp` 隔离虚拟环境中安装并验证了仓库 Yahoo 依赖及 gateway 通用依赖：requests 2.34.2、yfinance 1.7.0、curl_cffi 0.15.0、pandas 3.0.6、SciPy 1.18.1。yfinance 报告生产 curl_cffi backend 已启用。

完整测试通过：186 passed，119 subtests passed。覆盖既有 Quant、News、审计、交付流程，以及 Yahoo provider、真实 pandas 结果映射、collector 到 adapt → freeze → analyze 的离线 fixture。`global_stock_data.py --list` 返回 58 项能力，其中包含 14 个新 yfinance 路由和原有 Yahoo 兼容能力；gateway/collector `--help`、`schema --kind request`、18 个 Markdown 文件的本地链接、49 个 Python 文件的 AST 解析和 `git diff --check` 均通过。

真实 Yahoo 验收使用 NVDA：collector 取得 2023-10-09 至 2026-10-09 共 754 根日线；直接 `global_stock_data.py yahoo_history` 路由取得近期 28 根日线，stdout 回执的 `record_count` 与 bars 数一致。provider 与版本元数据保留，日线时间标记为 `bar_label_not_trade_time`。新闻请求返回 0 篇，状态为 unavailable。行情标记 partial，因为来源未确认价格单位、当前环境也没有覆盖完整窗口的交易日历证据；这些缺口没有通过推测补齐。

真实响应完整经过 Quant → 审计 → News → 综合，研究 CLI 成功生成报告。数据 gate 因单位来源缺失而 veto，最终方向概率为不可用；News 阶段为 `not_assessed`，没有把无新闻解释成中性。生成的 `final_response.md` 通过 `validate-response` 交付校验。这次是正确的降级/弃权结果，不是可报告的预测结果。

## 适用范围与限制

- 依赖只安装在临时隔离环境；没有改装用户实际 gateway Python、已安装 Skill、长期 memory、runtime/bench 数据或全局配置。
- 联网验收覆盖一个美股普通股的历史行情、Yahoo 新闻请求、collector、gateway CLI 和完整分析交付路径；没有对港股、ETF 或每个 P0/P1 信息函数逐项做实时调用。相应映射及 HK session 行为由离线 fixture 覆盖。
- Yahoo 本次未返回新闻，因此实时 article 正向映射未被验证；离线测试验证了 ticker stream/Search 归一化、发布时间和 ticker tag 规则。Yahoo 短 feed 也不提供历史 PIT 新闻完整性保证。
- 真实行情未满足当前预测准入条件。单位/币种、闭合 session、完整公司行动覆盖和源身份均按实际证据处理；Skill 保留既有 gate，不以 `fetched_at`、ticker 推断或当前交易时间替换缺失证据。
- 结果代表 2026-10-09 UTC 的一次有限联网检查，不保证后续 Yahoo 可用性、完整数据覆盖或策略表现。

接口说明与运行边界见 [yfinance-data.md](stock-data/references/yfinance-data.md) 和 [README.md](README.md)。

## yfinance 强制优先与兼容回退验收（2026-10-09 UTC）

Yahoo 路由改为强制 yfinance 优先。Yahoo 主依赖合并到 `stock-data/requirements.txt`；yfinance 请求错误、空结果、缺依赖或缺能力时会自动调用登记的旧兼容接口，不提供用户切换项。已返回的 yfinance `partial` 数据保留。collector 与 adapter 保留 `fallback_attempted`、`fallback_used`、`fallback_route` 和 `fallback_reason`。没有语义等价入口时返回 `NoLegacyRoute`；包括结构化筛选和带类型约束的搜索。普通搜索回退到 `stock_search`，并报告实际来源 Eastmoney search。

验证结果：完整 pytest 为 195 passed、119 subtests passed；改动相关 provider、fallback、collector 和 adapter 用例为 45 passed。Python 3.14.6 隔离环境中 requests 2.34.2、yfinance 1.7.0、curl_cffi 0.15.0、pandas 3.0.6、SciPy 1.18.1 均可导入，`pip check` 无依赖冲突。`global_stock_data.py --list`、collector `--help`、`quant_research.py schema --kind request`、51 个 Python 文件 AST 解析、57 个 Markdown 本地链接及 `git diff --check` 均通过。自动回退、各路由映射、失败 provenance 以及 collector → adapter 的来源信息以离线 fixture 验证；另在真实联网集成中注入受控 yfinance `NetworkError`，由旧 `stock_kline_yahoo` 成功取得 NVDA 754 根日线（2023-10-09 至 2026-10-09），回退 provenance 经 collector 和 adapter 保留，`quant_research.py pipeline` 返回 success。分析因单位/收盘证据不足而 abstain，`may_report_direction=false`；这是预期的安全降级，不构成可报告预测。该验证覆盖真实旧接口回退路径，但 yfinance 失败由测试注入，不是上游实际故障。
