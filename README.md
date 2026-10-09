# Stock Data & Quant Research

`stock-data` 是面向美股、港股及相应 ETF 的可安装 Skill，统一行情、基本面、技术指标、新闻情绪、专项事件与量化方向研究。市场数据只经现有 Finnhub MCP / 本包 global-stock-data gateway 获取，研究脚本仅处理已采集的数据。

普通个股/ETF 的“分析、简析、怎么看、综合分析”以及方向判断遵循 **数据验证 → Quant → 验证/审计 → News/Sentiment → 交叉验证与风险 → Final Synthesis**：三条独立预测路径、一条因子诊断路径、共识后的偏差审计、独立消息面分析，再做保留可报告 Quant 概率的分类证据综合。简析只控制篇幅，默认 `intent=forecast` / `5D`，不能变成省略 Quant 的行情与新闻摘要。用户明确的事实查询、纯历史及专项描述才只取必要数据。

## 发给 Agent 的一键安装指令

复制以下整段给支持本地 Skill 的 Agent。它是一条自然语言执行指令，无需手工拆分操作；安装目标是仓库中的 `stock-data/`，不是整个仓库。

```text
请从 https://github.com/x0fun1/stock-data 安装 stock-data Skill（仓库内路径：stock-data），使用仓库当前默认分支。先读取仓库 README.md 和 stock-data/SKILL.md，并检查当前 Agent 的实际 Skill 安装目录。Codex 优先使用自带的 skill-installer；安装目录使用 $CODEX_HOME/skills，未设置 CODEX_HOME 时用 ~/.codex/skills。其他 Agent 使用其自身支持的 Skill 目录，不猜路径。将 stock-data/ 整个目录连同 scripts/ 和 references/ 完整安装，保留其他 Skill、配置和已有长期 memory。若 stock-data 已存在，不覆盖，改按 README 的升级流程处理。检查 Python 3.10+；在实际执行 gateway 的 Python 环境中检查并按权限补齐 requests（global gateway 需要）。同时在用户授权且隔离的 Python 环境中安装 Yahoo 可选依赖：先阅读 stock-data/requirements-yahoo.txt 和 README 中的 Yahoo 安全/来源说明，再执行 `python -m pip install -r stock-data/requirements-yahoo.txt`（使用与 gateway 相同的实际 Python 解释器；不得跟随未固定的上游分支、不得自动改装其他全局环境）。如用户环境不需要 Yahoo、无安装权限、网络不可用或该固定依赖安装失败，保留 Skill 安装并如实报告 Yahoo 依赖未就绪，不要改写 requirements 或用其他传输后端替代。量化脚本本身只用标准库。用已安装脚本的 --list、--help 和 schema --kind request 做离线验收，检查 SKILL.md 及 reference 链接可读；Yahoo 依赖安装成功时另做 Yahoo provider 的离线验收，不把离线通过表述为实时连通成功。报告安装路径、Skill 版本、下载的 commit、依赖安装及验收结果；按当前 Agent 的加载机制刷新，Codex 安装后提示重启以加载 Skill。此步骤不配置 Finnhub 密钥，不修改长期 memory；长期约束另用 README 的 memory 指令保存。
```

安装目录应为：

```text
<Agent 的 Skill 目录>/stock-data/
  SKILL.md
  scripts/
  references/
```

Skill 不会自动提供 Finnhub MCP 连接或 API key。已连接的 Finnhub 工具按当前会话 schema 使用；未连接或缺能力时，仅走 gateway 已支持且可用的回退。网络权限、来源授权和限流边界见 [来源限制](stock-data/references/source-policies.md)。

### Codex 终端安装命令（可选）

如当前 Codex 安装含系统 `skill-installer`，也可执行以下命令。它们仅用于首次安装；该安装器遇到已存在的目标目录会拒绝覆盖，没有 `--upgrade` 或 `--force` 参数。`requests` 需在实际执行 gateway 的 Python 环境可用。

PowerShell：

```powershell
$stockCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
python (Join-Path $stockCodexRoot 'skills/.system/skill-installer/scripts/install-skill-from-github.py') --repo x0fun1/stock-data --path stock-data
```

macOS / Linux（sh/bash）：

```sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" --repo x0fun1/stock-data --path stock-data
```

安装器默认 ref 为 `main`；固定版本时可加 `--ref <tag 或 branch>`。若默认分支已变更，使用实际分支名。系统安装器不在上述位置时，使用前面的 Agent 指令，由 Agent 定位可用安装能力。安装完成后重启 Codex 以加载 Skill。

## 发给 Agent 的一键升级指令

```text
请将已安装的 stock-data Skill 升级到 https://github.com/x0fun1/stock-data 当前默认分支的最新版。先定位当前 Agent 实际加载的 stock-data 安装目录，读取本地 SKILL.md 版本并记录旧文件摘要。将上游下载到独立暂存目录，记录新 commit，阅读新版 README.md、SKILL.md 和相关 references，对比文件及本地改动；先验证新版结构、reference 链接、Python 依赖以及 global_stock_data.py --list、quant_research.py --help 和 schema --kind request。验收通过后，把旧 Skill 完整备份到 Skill 扫描目录之外并报告备份路径，再用新版完整目录替换安装目录（不要在旧目录上叠加复制，以免遗留已移除文件）。备份所有本地改动，明确报告哪些改动没有迁入新版；仅迁移与新版契约兼容的用户自定义配置，不保留旧算法或旧路由覆盖新版。不要改动其他 Skill、Finnhub/MCP 配置、密钥、用户 runtime 产物或长期 memory。再次在最终安装路径验收；替换或验收失败时恢复旧版。若内容没有变化则报告已是最新版。报告旧/新 Skill 版本、commit、主要变化、备份与验收结果，按当前 Agent 机制刷新；Codex 提示重启加载。仅安装 stock-data/，不要把 source-material/、tests/ 或仓库根 README 装成额外 Skill。
```

长期 memory 只保存下面的路由约束。升级通常不需要重写 memory：方法、算法、契约和降级规则随当前 Skill 更新。

## 发给 Agent 的长期 memory 指令

复制以下整段。Agent 应保存到自身支持的长期记忆位置；只有该环境实际使用 `memory.md` 时才写入该文件。仓库内不附带替用户生效的全局 memory。

```text
请将以下“股票 / ETF 分析约束”合并保存到你实际支持的长期 memory 中（若该环境使用 memory.md，就更新该文件）。保留其他记忆；如已有同主题约束，将其更新为这一版，避免重复或冲突。不要把 stock-data 的算法、数据源清单、路径参数或版本号抄入 memory。保存后报告实际位置与生效范围；若环境不支持长期记忆，如实说明，不声称已跨会话生效。

股票 / ETF 分析约束

- 凡涉及股票或 ETF 的价格趋势、涨跌方向、未来走势、收益概率、技术/量化分析、消息面、新闻情绪、事件影响或综合投资研究，必须优先使用 stock-data Skill，并遵循其当前 SKILL.md 与 references/ 定义的方法和流程。
- 不自行另建股票趋势、量化预测、新闻情绪或综合判断方法；stock-data Skill 是此类任务的统一研究框架和事实来源路由。
- 普通个股/ETF 的分析、简析、怎么看、综合分析，以及方向判断，必须按 Skill 完成 Quant → 验证/审计 → News/Sentiment → Final Synthesis；简析只缩短篇幅，不能跳过 Quant。最终展示实际 Quant 上涨概率，缺失/无效/否决时明确“上涨概率：不可用”并说明原因，不自行补算或由新闻修改概率。消息数据不可用时，按 Skill 记录未评估并完成降级综合，不把消息缺失当作中性或已验证。
- ETF 按与股票相同的分析原则处理；根据需要结合 ETF 自身价格数据、相关新闻及其指数/行业/主要成分股背景，但分析方法仍服从 stock-data Skill。背景资料不能替代 ETF 自身行情或凭空补齐基金字段。
- 简单事实查询（如当前价格、历史价格、基本资料）及纯历史描述只调用 Skill 所需的数据能力，不强制运行完整方向研究；单独查询新闻、评级或日历时按 Skill 专项路由处理，一旦涉及未来方向就转入完整流程。
- Skill 的具体算法、数据源、输出契约和降级规则以当前版本为准；长期 memory 不重复保存这些实现细节。
- Skill 未安装、无法读取或所需能力不可用时，明确报告缺口并按 Skill 的可用能力降级；无法读取 Skill 时不自行重建替代研究方法或编造概率。
```

## 能力与分析路径

| 任务 | 当前路径 / 边界 |
|---|---|
| 行情、历史价格、资料 | 最小必要查询，保留来源、时间、单位与缺口 |
| 基本面、财报、估值 | 资料与财务证据、披露时序、口径核对和按需同行比较 |
| 历史走势、技术指标 | 原始 OHLCV 与本地指标；历史描述不自动变成预测 |
| 个股/ETF 分析、简析、怎么看 | 默认完整 Quant → News → 综合；输出实际概率或明确不可用，交付前校验待发送文本 |
| 新闻、情绪、评级、日历、内部人 | 事件事实、观点、发布时间和行情反应分开处理 |
| 期权、资金流、空头成交量、SEC、宏观、筛选 | 使用已支持数据能力，明确代理值、分页、授权与覆盖限制 |
| ETF、多标的、综合/深度研究 | 按需补充证据；通用分析及方向比较按 ticker 分别运行完整流程 |

上述专项的具体入口见 [analysis-paths.md](stock-data/references/analysis-paths.md)。涉及未来方向或上涨概率时，统一执行：

| 量化路径 / 阶段 | 实际实现 | 是否进入概率共识 |
|---|---|---|
| A — Quant Research | 固定 20-session 动量假设、非重叠历史条件频率与时间外验证 | 是，有效预测才纳入 |
| B — Factor Research | OHLCV 因子验证、去冗余、后段频率映射与 holdout | 是，有效预测才纳入 |
| C — ML Research | L2 Logistic Regression、Purged K-Fold、Embargo、causal walk-forward、可选 Platt 校准与 holdout | 是；当前 baseline 为 partial，披露校准状态 |
| D — Factor Diagnostic | 单证券时间序列 IC 与 1D/5D/20D forward-return 诊断 | 否，不输出预测概率 |
| 共识 | 有效 A/B/C 等权概率平均、分歧与证据多样性 | 仅聚合已有概率 |
| E — Bias / Adversarial Audit | 时序、泄漏、OOS、Brier 基准、多重检验范围及未评估偏差 | 否，可否决方向 |
| News / Sentiment | Quant 冻结后独立做 PIT 筛选、去重、事件/观点分离、衰减与行情反应 | 否 |
| Final Synthesis | 验证两份冻结结果后做分类证据综合，保留原 Quant P(up) | 不生成 News 或融合概率 |

代码、门槛、结果字段和交付检查见 [quant-paths.md](stock-data/references/quant-paths.md)。A–E 是流程角色，本地运行器已经执行这些阶段，无需另建五个 Agent。

## 通用分析与方向研究的运行方式

先按 Skill 完成一次数据采集，将实际 gateway 响应按 [collection-adapter.md](stock-data/references/collection-adapter.md) 映射为规范化输入。推荐组合入口：

```text
python stock-data/scripts/quant_research.py pipeline --request request.json --responses gateway-responses.json --output-root runtime
```

`pipeline` 顺序完成请求规范化、adapt、freeze、analyze；它只消费已采集文件，不自动拉取最新数据。Agent 仍负责实际工具采集、来源字段映射和授权边界。逐阶段命令也可用。示例在仓库根目录执行；安装后改用已安装脚本的实际绝对路径，JSON 输入和 `runtime/` 放在用户可写工作目录。

```text
python stock-data/scripts/quant_research.py request --input request.json --output normalized-request.json
python stock-data/scripts/quant_research.py adapt --request normalized-request.json --responses gateway-responses.json --output collected.json
python stock-data/scripts/quant_research.py freeze --input collected.json --output-root runtime/snapshots
python stock-data/scripts/quant_research.py analyze --snapshot-dir runtime/snapshots/<snapshot_id> --output-root runtime/research
```

`<snapshot_id>` 使用 `freeze` 实际返回的目录。可选 `analyze --news-policy <policy.json>` 配置事件衰减，契约见 [news-analysis.md](stock-data/references/news-analysis.md)。

默认主研究窗口为最新已确认收盘交易日前推三个日历年，horizon 支持 `1D/5D/20D` 有效交易 session，默认 `5D`；用户明确指定的区间优先。更长原始响应保留在 raw envelope；样本不足的路径披露缺口，不静默扩窗。短新闻窗口单独报告实际覆盖，不能伪装成三年档案。ETF 使用自身 OHLCV，背景资料不替换标的行情。

`analyze` 先检查数据资格；通过后完成四路径、共识、E、News 与综合，未通过则跳过研究器并输出弃权报告。生成：

```text
runtime/snapshots/<snapshot_id>/          manifest、规范化各域、raw envelopes
runtime/research/<analysis_id>/
  researchers/{quant,factor,ml,factor_backtest}.json
  quant_result.json + quant_result.freeze.json
  news_result.json + news_result.freeze.json
  report.json + report.md
  agent_summary.json
  final_response.md                     三部分简版，保留八项证据及概率/不可用字段
```

CLI 的 `status: success` 仅表示产物写出；另检查 `analysis_status` 和 `may_report_direction`。优先读取程序生成的 `final_response.md` 与有界 `agent_summary.json`，完整八项证据在 `report.md`，不把原始行情/新闻/矩阵塞入 LLM。简析以 `final_response.md` 为基础，强制字段原行保留，可补充来源明确的解释。交付前检查数据门槛、路径状态、样本、概率来源、审计 veto、News 覆盖和最终综合；不能把 `partial/insufficient_data/not_assessed` 当成完整验证。审计否决时不使用 `pre_veto_prob_up` 绕过结论抑制。

将实际准备发送的完整回复保存为 UTF-8 Markdown，再校验：

```text
python stock-data/scripts/quant_research.py validate-response --analysis-dir <实际返回的 report_dir> --response-file <待发送的完整回复.md>
```

该检查验证 Quant/News 冻结 digest、报告来源绑定、完成阶段和强制原文行；遗漏/篡改概率、原生字段、标的/窗口/时间或未保留不可用原因时返回错误。通过后发送同一文本，不能再次删减强制字段。它不能拦截宿主 Agent 未调用脚本而直接发送的消息，也不核验全部自然语言主张。前置采集/校验失败到无法产生报告时，直接报告“上涨概率：不可用”、失败阶段与实际缺口，明确研究未完成。

## Yahoo 可选 yfinance 接入（2.4.0）

选 Yahoo 时结构化数据默认 `yahoo_*`；美股仍优先实际已连接且覆盖字段的 Finnhub。P0/P1 覆盖历史/批量行情、报价/资料、三表、搜索/新闻、统计/分析师/持仓、期权/财报日程、基金/筛选；详见 [yfinance-data.md](stock-data/references/yfinance-data.md)。旧 Yahoo 保留原契约，仅显式兼容。Quant、中央 gate、News 时序和 schema 1.2 不变。

Yahoo 依赖仅调用时加载。离线发现与其他来源不要求安装 Yahoo：

```text
python stock-data/scripts/global_stock_data.py --list
python stock-data/scripts/yahoo_collect.py --help
# 在授权的隔离环境核对来源和约束后，再安装可选依赖：
python -m pip install -r stock-data/requirements-yahoo.txt
```

适配基线为 spec 的 yfinance 1.7.0 / 固定 commit。仓库本次已在 Python 3.14.6 隔离环境解析并安装 `requirements-yahoo.txt`，验证 yfinance 1.7.0 使用 curl_cffi 0.15.0 后端；这不代表用户实际 gateway 解释器已配置，也不保证 Yahoo 持续可访问。生产 provider 严格要求 curl_cffi 后端，不提供 requests 替代。使用实际执行解释器、固定发布版本或明确源码 commit，记录来源/库版本；不跟随 main、不自动 pip install。安装后使用实际脚本路径，cache-dir 与输出指向用户可写目录，cookie 缓存不进快照/仓库；不自动修改已安装 Skill、用户 runtime/bench 或全局配置。

Yahoo gateway 执行必须传 `--cache-dir` 或设置 `STOCK_DATA_YAHOO_CACHE_DIR`；collector 必须传 `--cache-dir`。以下是调用设计，不是实时验收结果：

```text
python stock-data/scripts/global_stock_data.py --function yahoo_history --params-file history-request.json --output history-envelope.json --cache-dir runtime/yfinance-cache
```

日线 source timestamp 保留 `bar_label_not_trade_time`，不是收盘/报价时间，可能触发现有 24h/source_before_close gate。来源未确认 unit 留 null；不得补造时间或单位绕过检查。生成 response map 不代表预测有效，须完成实际 adapter/freeze/研究审计；详见 Yahoo 参考的当前限制。

`yahoo_collect.py --request request.json --domains market news --output gateway-responses.json --cache-dir runtime/yfinance-cache` 可生成既有 pipeline 的 `--responses` 文件，只采所需缺域，不重复已成功 Finnhub。离线 fixture、transport、adapt/freeze/analyze 验收与有限联网检查分别报告；注册能力不是连通性成功，不宣称已取得有效研究数据。实际参数以 `--list`/`--help` 为准。

## 实现边界与验证

Skill `2.2.2` / research runtime `0.2.2` / snapshot schema `1.1` 收紧普通简析/分析的默认路由，新增程序生成的最终简版和交付校验；保留 normalized DATA/raw 隔离及行情身份、日线频率、实际窗口、时区时间、交易日轴、最新收盘和公司行动证据门槛。缺少来源日历或复权证据时弃权；不要制造字段通过检查。Yahoo 的 `include_metadata=true` 保留来源 meta/events/adjclose，但不等于完整交易日历或已验证复权。输入契约见 [reliability-gates.md](stock-data/references/reliability-gates.md)。

最终用户回复必须展示可报告的 Quant `prob_up/direction/confidence/agreement` 和存在的 `diversity`。综合结果及有界摘要保留原字段，News 不改变概率；缺失时明确“上涨概率：不可用”。confidence/agreement 的现有分类标签不伪造为百分比，数值来自当前标的的实际运行，规则见 [probability-policy.md](stock-data/references/probability-policy.md)。

旧 1.0 快照可读，但不能自动通过新的预测门槛；应重新采集并生成带实际证据的快照。1.1 manifest 绑定判定元数据，加载时重算门槛。严格模式额外要求 A/B/C 的时间外样本和 C 校准；标准模式披露未校准模型值。新闻相关性、时效、来源与冲突在进入方向前检查，供应商情绪不能覆盖 mixed 事件。hash 只证明完整性，不认证供应商真伪。

保留已有路径保护：输出必须直接位于解析后的 `output_root`，拒绝路径穿越、外部 symlink/junction 和覆盖。具体契约及并发边界见 [Snapshot Schema](stock-data/references/snapshot-schema.md#artifact-path-boundary)。外部文本仅作 DATA；网络限定既有来源 HTTPS 主机、同主机重定向和大小边界，密钥/动作字段在落盘前脱敏移除。

量化运行使用 Python 3.10+ 标准库，并需可用的 IANA 时区数据库（Windows 缺数据库时可使用 `tzdata`）；global gateway 另需 `requests` 和访问来源所需的网络/授权。`adapt/freeze/analyze/pipeline` 不调用网络。新闻筛查与情绪为未校准的确定性启发式；日线行情反应仅是时间关联，不证明新闻导致涨跌。消息面不得修改 Quant 概率。

当前未实现多资产横截面组合回测、成本后执行/市场冲击、正式 CPCV/PBO/DSR 计算、PIT 多资产幸存者审计、XGBoost/LightGBM、bootstrap 不确定性、长期路径可靠性、受治理的 holdout/forward 凭据存储或专用 ETF 穿透模型。输入缺失和未实现计算须明确标为未评估；未附带或伪造真实行情研究结果。

仓库开发验证（离线）：

```text
python stock-data/scripts/global_stock_data.py --list
python stock-data/scripts/quant_research.py --help
python stock-data/scripts/quant_research.py schema --kind request
python -m unittest discover -s tests
```

前三条可在仅安装 `stock-data/` 后运行；测试依赖仓库根的 `tests/`。这些验收不访问市场，也不保证网络数据源当时可用。

## 目录与参考

```text
stock-data/
  SKILL.md                            Agent 的统一入口
  scripts/global_stock_data.py        数据 gateway 的本地回退与指标函数
  scripts/quant_research.py           请求、适配、冻结与分析 CLI
  scripts/quant_research/             研究器、验证、审计、News 与报告
  references/                        专项路由、数据口径与研究契约
source-material/                     上游迁移档案，不覆盖当前运行规则
tests/                               离线数据完整性、News 契约与路径安全回归测试
UPSTREAM_REVIEW.md                    上游调研与许可决策
```

执行以当前 [SKILL.md](stock-data/SKILL.md) 及其 references 为准。来源授权、许可和限制见 [source-policies.md](stock-data/references/source-policies.md)、[UPSTREAM_REVIEW.md](UPSTREAM_REVIEW.md)；预测概率是研究结果，不是交易指令或收益保证。
