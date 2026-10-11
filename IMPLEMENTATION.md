# stock-data yfinance 验收记录（Skill 2.5.0 历史记录）

工作区：`/data/stock-data-skill`。该记录针对 Skill 2.5.0，验收日期：2026-10-10（Asia/Shanghai；2026-10-09 UTC）；首段实时联网记录为升级前 2.4.0 主路由验收。当前仓库 Quant 修订版的最终版本和 Phase 6 结果见本文末尾。

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

## Quant 改进计划实施与 Phase 6 验收（2026-10-10）

本次把修订计划落地为 Skill **2.6.0**、research runtime **0.3.0**；冻结 Snapshot schema 仍为 **1.2**。未删除已有 JSON 字段或改变 reason/status 枚举；新增分析结果字段均为可选，旧结果缺字段时降级为 `not_assessed` 并保持等权。没有修改 Quant → 冻结 → News 单向流或 `NewsInput`。

### 五项量化修订

| 项目 | 实施结果 |
|---|---|
| 经济性与信心 | 5/10/20 bp 假设成本写入独立 `economic_viability`，不修改概率或 confidence；不是实际费用、滑点、冲击或净收益回测。 |
| A 路径时序 | OOS 与历史匹配样本标签必须在决策日成熟；保留原有 30 个样本报告基线。Bayesian prior、`alpha=20` 与 252-session 衰减未实现/未验证，现行估计仍为未收缩、无衰减频率。 |
| C 路径防泄漏 | Platt 只拟合 holdout 前 causal walk-forward OOS 分数，并逐行保留训练标签边界；最终 holdout 独立评价。仓库没有 `_xgb_estimator.py` 或嵌套早停实现，XGBoost/LightGBM 明确为未实现、未探测、未加载；原生 L2 Logistic Regression 仍是唯一 ML 路径。 |
| B 路径统计推断 | 对全部候选做 OOS 时间序列 IC 诊断、300 次循环 block bootstrap、circular-shift 检验和 BY-FDR；选择与评估窗口分开。平稳性与区间覆盖率未验证，DSR 保持 `not_assessed`。 |
| 共识权重 | 在共同 OOS 日期和标签上估计较早窗口、评价较晚窗口；有效样本不足、任何路径弃权或无法时间隔离时回退等权。当前预测始终使用等权，Skill 权重没有启用。极端分歧单独报告，并只在独立公共 OOS 证据足够且表现失败时交给现有 veto。 |

### 固定合成快照的 OOS 对照

比较基线为提交 `b3f529cec08074a5577b01e952e79b2406263a16`（Skill 2.5.0 / runtime 0.2.2）；新版本在同一 `tests/research_fixture.py` 756 根合成 OHLCV 快照上运行。数据明确是合成波形，不是实际交易所日历或市场行情。旧版只落汇总 OOS 指标，因此临时基线副本仅额外暴露原预测日期/标签供配对；临时观测字段不改变模型公式、参数或概率。

| 路径 | 基线原生 OOS | 新版原生 OOS | 同日期/标签配对结果（基线 → 新版） |
|---|---:|---:|---|
| Quant | N=147；Brier 0.252944；Log Loss 0.699039；ECE 0.066837 | N=25；Brier 0.254566；Log Loss 0.702284；ECE 0.109359 | N=25；Brier 0.254566 → 0.254566，Log Loss 0.702284 → 0.702284，ECE 0.109359 → 0.109359。新版按 5-session 间隔使用非重叠标签；25 是评分时点数，不代表弃权。 |
| Factor | N=0，有效校准桶不足 | N=0，有效校准桶不足 | 共同标签日期 25，完整预测 0；两版均弃权，误差指标不可计算。 |
| ML | N=25；Brier 0.002485；Log Loss 0.022327；ECE 0.020947 | N=25；相同指标 | N=25；配对指标一致。此人工波形令预测几乎完美，不能外推到市场数据。 |
| 两路径等权（Quant + ML；Factor 弃权） | N=25；Brier 0.069527；Log Loss 0.305246；ECE 0.262686 | N=25；相同指标 | 决策日 2023-08-17 至 2024-03-06；配对 Brier 差为 0。按 5 个 session 的 circular moving-block bootstrap、500 次重采样，得到 `[0, 0]`；该区间只反映本合成样本，未验证覆盖率。 |

三路径完整预测样本为 0，所以权重估计和后续评价均为 `not_assessed`，`skill_weight_validation.status=fallback_equal_weight`。覆盖率按各自预定义评分 cohort 计算：Quant 25/25、ML 25/25、Factor 0/25；新版 Quant 改用非重叠决策节奏，不能把基线每日 147 个重叠标签直接当作同一评价基数。没有足够的真实共同 OOS 样本支持准确率改善结论。

### 运行性能

使用相同 Python 环境、固定快照和 `tracemalloc` 跟踪，端到端时间包含研究、Quant/News 冻结、综合、报告和交付校验。测量是各一轮，包含运行噪声；新版时间还包含新增日期化 OOS 结果与报告字段。

| 指标 | v2.5.0 / runtime 0.2.2 | v2.6.0 / runtime 0.3.0 | 变化 |
|---|---:|---:|---:|
| Quant + News 全流程 | 31.95 s | 34.02 s | +2.07 s（+6.5%） |
| 峰值 traced 内存 | 2,196,096 B（2.09 MiB） | 2,974,556 B（2.84 MiB） | +778,460 B（+35.5%） |
| Snapshot 读取 | 1 次 | 1 次 | 无变化；4 条 Research path 复用已加载数据 |
| ML 训练 telemetry | 基线没有拟合计数/耗时字段 | 11/11 次成功；3,622 行训练输入；拟合合计约 2.55 s（不启用 tracemalloc） | 新增记录；基线没有同口径 counter，XGBoost/LightGBM 加载次数为 0 |

单路径耗时（同一 traced pipeline 中）：Quant 0.257→0.028 s、Factor 0.343→1.119 s、ML 31.012→32.444 s、factor diagnostic 0.120→0.130 s。因子推断新增约 0.78 s，端到端增加约 2.07 s；峰值总量仍低于 3 MB。运行报告现在记录各路径、Quant/News 墙钟时间、快照读取次数、ML 拟合次数，以及常规运行未开启内存跟踪这一事实。没有 XGBoost 相关安装、导入或失败重试。该测试规模不足以证明更长历史或其他主机的内存预算。

### Schema、News 与最终交付

- 当前 Snapshot schema 保持 1.2；测试确认新 1.2 可读取、旧 1.0/1.1 可读取但不升级为预测合格。
- 缺少新增 OOS/经济性字段的旧分析结果可被新 consensus/synthesis 消费：默认等权、经济性 `not_assessed`；现有 `validate-response` 必填字段保持不变。
- Quant 预测、概率质量和成本字段不进入 `NewsInput`；集成测试覆盖 Quant 与 News 同向、背向、Quant 弃权、News 缺失，以及新增字段不污染 Quant 概率。
- `report.json`/`agent_summary.json` 记录执行性能；`report.md` 显示耗时、快照加载和 ML 训练状态。常规运行不使用 `tracemalloc`，因此 `peak_memory_bytes=null`；峰值数据只在本节的外部验收基准记录。

### Phase commits

| Phase | Commit |
|---|---|
| 1 — 成本与预测置信度分离 | `7fa1744` |
| 2 — Quant 标签成熟时间边界 | `66656dd` |
| 3 — 因子 OOS IC 推断 | `9e7571e` |
| 4 — 共识公共 OOS 与时间隔离权重 | `3dec51c` |
| 5 — ML Platt 校准与 Holdout 隔离 | `0b6d86f` |
| 6 — Schema、性能及端到端验收 | Phase 6 交付提交（本报告所在提交） |

### Phase 6 最终验收结果

- `python -m unittest discover -s tests -v`：203 tests passed，28.389 s，退出码 0。尾部 JSON `FileExistsError` 是既有 CLI no-overwrite 用例的预期输出，不是 suite failure。
- `global_stock_data.py --list`、`quant_research.py --help`、`schema --kind request`、`schema --kind final` 均返回成功；两份 schema 声明 1.2。
- 57 个 Python 文件 AST 解析通过；README、IMPLEMENTATION、Skill 入口和 references 共 19 份文档的相对链接无缺失；`git diff --check` 通过。
- 主要代码：`consensus.py`、`audit.py`、`report.py`、`synthesis.py`、`orchestrator.py`、A/B/C researchers、`math_utils.py`、`validation/probability_quality.py`。新增兼容/防泄漏测试：`test_ml_temporal_integrity.py`、`test_schema_compatibility.py`；既有 pipeline、OOS、审计和 News 隔离测试扩展。
- 文档：更新仓库 `README.md`、`stock-data/SKILL.md`、`probability-policy.md`、`quant-paths.md`、`snapshot-schema.md` 和本实施报告。没有删除文件；没有安装、发布或修改用户目录中的 Skill。

`tracemalloc` 性能数字来自单轮固定合成快照对照；常规 runtime 不开内存跟踪。所有 OOS 数值和时间性能都仅是工程验收证据，不是市场预测结论。没有真实独立历史快照，因此 accuracy improvement、概率校准覆盖率和可部署 Skill 权重均未获验证。

## Quant 审计计划 P0 执行批次（2026-10-11）

依据 [量化深度审计与改进计划](audits/quant-2026-10-11/QUANT_AUDIT_PLAN.zh-CN.md)，Skill 更新为 **2.7.0**、research runtime 更新为 **0.4.0**；Snapshot schema 保持 **1.2**，Quant → 冻结 → News → 综合顺序和 NewsInput 未变。

- A 的当前概率和历史 OOS 预测共用 30 个非重叠成熟匹配样本门槛。
- 每条带日期的 OOS 记录由冻结 OHLCV 核查 ticker/horizon/snapshot 身份、decision 与 label session、准确 H-session 间隔、收盘价重算的目标、as-of 成熟状态、训练标签结束边界及重复决策日。拒绝项按原因计数，不进入路径质量、等权权重实验或历史策略回放；现有原始概率是否可报告仍遵守原 prediction-integrity gate。
- 当前合格路径公共诊断与全候选诊断分开，单路径集合也报告自身指标。历史策略回放逐日期平均当天有有效预测的路径，与该日之前成熟、非重叠标签形成的 Jeffreys 平滑扩展基率在相同预测日期比较，保存 `active_path_mask` 并报告覆盖、Brier、Log Loss 和点估计 Brier Skill；不估计技能不确定性，不启用新的共识权重。
- Factor 报告固定三段式切分与 H+1 非重叠规则下的校准容量，并估算三个均衡桶各需 20 个标签时的总历史长度。估算仅是样本预算，不扩窗、不降低最低样本要求。
- `confidence_score_kind=heuristic_evidence_support`，UI 明示该 0–100 分是启发式研究支持度而非准确率；新增 `predictive_evidence_grade`，正向点估计仍明确标记为探索性或不确定性未估计。没有独立 ensemble calibrator 时加入置信度扣分与解释。
- `agent_summary.json`、`report.md`、`final_response.md` 与 `validate-response` 同步新增证据等级和评分语义；相关 skill references 与最终 schema 投影同步更新。新增 OOS/样本预算仅用于审计与披露，不作为市场有效性的证明。

初次 P0 实施时未运行测试；随后代码质量复核于 2026-10-11 运行 `python -m unittest discover -s tests`，**207 个测试通过**，`compileall` 与 `git diff --check` 通过。复核修正了历史 replay 误把路径名当日期集合、泄漏路径仍可能进入 replay、当前合格路径缺 OOS 行时被公共样本静默剔除、交付新增字段的冲突文本未被识别，以及因子样本预算旧字段兼容问题；新增相应回归测试并更新输出契约断言。尾部 `FileExistsError` JSON 是既有防覆盖测试的预期输出。replay 已明示其分母仅为各路径提交的有效日期并集；若所有路径都漏记同一决策日，现有结果无法统计该日的弃权。真实独立 OOS、逐日冻结的预测账本和前瞻验证仍待后续 P1 研究数据与 ledger 建立后完成。

## 预测输出后 JSON 清理（2026-10-11）

依据 [预测 JSON 清理实施方案](PREDICTION_JSON_CLEANUP_PLAN.zh-CN.md)，更新为 Skill **2.8.0** / research runtime **0.5.0**；Snapshot schema 仍为 **1.2**，不改预测算法、数据 gate 或 Quant → 冻结 → News → 综合顺序。

- 新增 `pipeline --cleanup-json` 私有运行布局：`runtime/runs/<run_id>/` 内登记 collection、独占 snapshot 和 analysis；创建快照/分析目录时立即更新运行清单。未指定此模式的旧 CLI 共享输出行为不变，`--keep-json` 显式标记研究保留模式。
- 新增 `cleanup-run --run-dir` 与 `cleanup-run --analysis-dir`。完整 run 清理会对实际待发送文本再次调用 `validate_final_response`，核对 analysis/snapshot 身份、登记目标的 SHA-256 和大小，拒绝未知文件、链接/junction、路径穿越或哈希变化；不触碰外部输入、旧共享快照、其他 run、用户文件及缓存。
- 清理保留 `report.md`、`final_response.md` 和最小 `cleanup_receipt.txt`。回执只含 ID、回复/报告哈希、状态、JSON 文件/字节数和相对目标名。操作系统文件锁保证并发互斥并复用失效锁文件；内容不符的既有锁文件会被拒绝而不覆盖。失效 pipeline 标记只在运行归属与清单校验后移除。dry-run、原子回执写入、partial 状态与幂等重试均由清理模块处理；analysis-only 模式不清理源快照。
- Skill 常规预测顺序改为创建私有 run、生成并校验待发送文本、宿主读取/复制后清理、发送同一文本；研究/审计使用 `--keep-json`。CLI 不判断消息是否实际送达。
- 新增 `tests/test_json_cleanup.py` 生命周期验收。代码质量复核后 `python -m unittest discover -s tests`：**220 项运行，219 通过、1 项跳过**；跳过项是本机策略拒绝创建 symlink 的隔离路径用例。实际 CLI 合成 abstain 流程、report/freeze 篡改阻断、未知文件、外部输入和共享快照保护、active pipeline 锁、并发清理锁、异常锁文件保护、失效标记归属保护、失败强制清理、dry-run、部分失败重试与幂等均通过。额外 `compileall`、六个 Python 文件 AST 解析、`git diff --check` 和 13 条 Skill 本地链接检查通过。Skill Creator `quick_validate.py` 因系统 Python 与 bundled Python 均无 PyYAML，无法启动；没有为此安装依赖。

## 移除 Yahoo 新闻能力并引入 News 影响分级（2026-10-11）

Skill 更新为 **2.9.0**、research runtime 更新为 **0.6.0**；Snapshot schema 仍为 **1.2**。Quant → 冻结 → News → 综合顺序、数据 gate、概率契约和 NewsInput 均未改变。

- **移除全部 Yahoo 新闻能力**：`yahoo_news`、旧 `stock_news`、`normalize_news`、provider 的 `_news` 与注册项、legacy fallback 的 `stock_news` 路由，以及 collector 的 news 域、`--news-count`、`--news-tab`。`yahoo_collect.py` 现在只采 market 域。实测依据：上游 ticker 新闻端点 `/xhr/ncp` 返回 404，NVDA/INTC/SPCX/AAPL 全部 0 篇；Finnhub MCP `get-news-pulse` 对同批标的分别返回 248/213/246 篇且全部带 `datetime_utc`。
- **新闻唯一来源改为宿主 Finnhub MCP**：`get-news-pulse` 基线固定 7 天，只返回 headline/source/datetime_utc，无正文；`summary` 留空并已在 collection-adapter 披露，正文依赖的观点词典因此减弱。宿主未连接 Finnhub MCP 时 News 记 `not_assessed`，不做二次联网。**代价**：不再有包内脚本级新闻降级路径，未配 MCP 的宿主消息面直接归零。
- **新增确定性影响分级** `news/impact.py` 与 `references/news-impact-rubric.md`，改写自 MIT 许可的 `tradermonty/claude-trading-skills`（Copyright (c) 2026 TraderMonty）`market-news-analyst` 的分类法：影响幅度分级、相对自身 20 日波动率的模式比对（amplified/consistent/dampened/inverse）、按事件类型声明的广度范围、簇内最弱信源分层、固定归因警示。
- **明确不并入上游内容**：复合 Impact Score（未校准的伪精确值，字段恒为 `not_implemented`）、跨资产反应分析（News 层只读单标的冻结 K 线）、WebSearch/WebFetch 采集、英文输出与长篇报告。
- 修正两处实现问题：信源分层原按最高层级匹配且与 docstring 的"最弱"承诺矛盾，已改为逐个判定后取最弱；模式比对新增 `1e-4` 日波动率下限，否则近常数收益序列会把所有事件误判为 `amplified`。
- 广度一律为**声明范围**而非观测值（`observed_breadth` 恒为 `not_assessed`）；影响分级不产生概率、不进入 A/B/C/D、不改动已冻结的 Quant 结果。
- 本轮完整回归：`python -m unittest discover -s tests` **237 项运行，236 通过、1 项跳过**，耗时 32.343 秒；跳过仍为本机不允许创建 symlink 的用例。删除 4 项 Yahoo 新闻专用用例，新增 12 项影响分级用例（含端到端 pipeline 校验 impact 落盘且不含概率字段），并重写 adapter 新闻 PIT 用例以改用 Finnhub 文章形状。`global_stock_data.py --list` 与 `yahoo_collect.py --help` 确认不再暴露任何 Yahoo 新闻函数。
- 没有移除历史 JSON 字段或改变 reason/status 枚举；旧分析结果缺少 `impact` 时按未评估处理，不影响既有字段读取。

## Quant 接口加固与证据口径复核（2026-10-11）

Skill **2.8.1** / research runtime **0.5.1**；Snapshot schema 保持 **1.2**。

- 缺少 as-of 的现有代码因 `str()` 转换不会出现所述字符串/None 比较 `TypeError`，但显式 `None` 会变成 `"None"`，可能错误放行结构性 OOS 日期。改为解析有效 ISO session 日期，缺失或类型错误时拒收记录，并在比较前显式检查类型。
- 共识和概率质量诊断排除重复 researcher ID 的所有副本，避免双重投票或选择第一/最后一条造成偏差；缺失 ID/评估映射写入 `path_input_rejections`，不再抛 KeyError。共识还复核数值有限性，显式 assessment 不能抹除路径已声明的泄漏。
- 共识将 `assessments` 显式传入 OOS 质量和实验权重诊断。缺失评估的路径不参与当前预测集合；历史 replay 继续独立于今天的可用集合，同时保留显式和原路径的泄漏排除。旧版独立调用仍支持路径内注解，但通过 `assessment_source` 披露该模式。
- 修正 C 的多家族 OHLCV 输入说明，以及 B 的收盘区间位置/隔夜收益家族。C 的数值预测不因标签修改，B 的同家族上限可改变保留因子，共识多样性评分也可能变化；家族标签不证明统计独立或预测改善。
- 文档明确 B 的训练 `abs(IC) >= 0.02`、相关性 `0.85`、家族/总数量上限是启发式筛选；最终 OOS 的 BY 调整不是训练选择的 FDR 保证。共识缺 ensemble 校准及技能区间的两项固定扣分，使当前最高支持度评分为 80（起点 85 时至多 65）。维持一条拒收记录触发 20 分扣减的保守政策，并披露 `rejected_sample_rate`；未设未经验证的拒绝率阈值。
- 本轮完整回归：`python -m unittest discover -s tests` **228 项运行，227 通过、1 项跳过**，耗时 31.093 秒；跳过仍为本机不允许创建 symlink 的用例。新增边缘日期、评估映射、重复身份、非有限概率、泄漏不可覆盖及最高评分回归，ML 既有完整时序测试同时验证输入家族。`compileall` 和 `git diff --check` 通过。没有新增独立市场 OOS 或技能不确定性区间，结果不能证明真实市场预测改善。
