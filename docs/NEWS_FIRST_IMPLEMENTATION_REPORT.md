# 新闻消息面整改实施报告

状态：代码迁移与离线验收完成；真实美股／港股公司样例尚未完成；工作区改动未提交。

- 实施日期：2026-11-02（Asia/Shanghai）。
- 整改依据：[NEWS_FIRST_REMEDIATION_PLAN.md](NEWS_FIRST_REMEDIATION_PLAN.md)。
- 审阅基线：`95f1fd3ae0a520672245243d877ac3396cde14dc`。
- 发行版本：`4.0.0`，request/assessment/import/snapshot schema 为 `news-1`。

本报告只记录实际执行的检查。离线合成测试、来源注册表、HTTP 成功和真实来源回执分别陈述，不能互相替代。

## 1. 已完成内容

### 1.1 新入口与模块

```text
stock-data/scripts/news_research.py
stock-data/scripts/news_research/
  contracts.py         request / assessment / import 契约校验
  orchestration.py     collect → prepare → analyze → report → validate-report
  coverage.py          按来源／实体／领域／query_key 汇总回执覆盖
  common/              时间、路径、JSON、脱敏、安全工具
  sources/             SEC submissions/companyfacts、Finnhub、Fed、FRED、registry、传输层
  collection/          分页/分区间采集、宿主导入、预算、增量覆盖
  storage/             SQLite 证据版本库、运行快照、锁、GC
  events/              标准化、PIT 排除、去重、事件版本与更正/撤回
  context/             财务事实、TTM/FCF/PE/PS 口径与目标暴露
  assessment/          证据准入、宿主结果校验、引用/日期/数字核对
  reporting/           报告渲染与实际发送文本校验
```

命令均已可用：`sources`、`schema`、`collect`、`prepare`、`analyze`、`report`、`validate-report`、`gc`。`gc` 默认 dry-run，实际清理必须显式 `--apply`。

### 1.2 数据与判断规则

- 默认 60 个自然日，允许 30～60；`as_of` 必须带时区，窗口为 `(as_of - lookback_days, as_of]`。
- 采集、窗口覆盖和分析充分性分开；覆盖只依据来源回执，不使用文章首末日期。
- 保留发布时间、更新时间、发生/生效/计划时间、取回时间及日期精度；缺失时不补造精确时刻。
- 新闻事件与文章分离，保留来源谱系、实质更新、更正/否认/撤回版本；旧闻重发不重置新鲜度。
- 方向仅 `bullish` / `bearish` / `neutral`；证据不足或未分析时为空，不自动中性。
- 重大事件必须有正文或官方摘录、目标关联和未被推翻的事实；标题/传闻只能待核实。
- 唯一重大事件优先最近 7 天，无合格候选扩至 30 天；31～60 天保留为背景，可返回空值。
- 综合判断、主因、相反证据和改变条件使用同一已准入事件集；宏观/行业影响必须有目标实际暴露。
- 财务事实保留报告期、披露日、币种、单位、会计口径、accession 和来源证据；TTM 只汇总四个连续独立季度，FCF 明确资本开支符号，PE/PS 拒绝亏损或不可比口径。
- 模型只允许引用登记证据 ID；虚构 ID、URL、日期或数字、与其他目标暴露、失效/被替代证据均被拒绝。
- 宿主输出缺失或非法时保留采集证据，方向为空并披露分析未完成，不回退到关键词情绪或规则打分。

### 1.3 来源接入

| 来源 | 本次状态 |
|---|---|
| `sec_submissions` | 已实现；遍历重叠历史 submissions 文件，不截最近 50 条；需用户配置 SEC 身份 |
| `sec_companyfacts` | 已实现核心财务事实采集；保留 taxonomy、tag、form、unit、accession、filed；未做账户实测 |
| `finnhub` | 已实现按日期分段查询；HTTP 200 不证明分页穷尽，覆盖保持 partial；需用户 API key |
| `fed` | 已实现官方年度归档 + FOMC 归档 + RSS 补充；真实公开端点探测见 §3 |
| `fred` | 已实现 vintage dates 与 observations 分页；需用户 API key，系列许可需单独确认 |
| `hkex`、`company_ir`、`china_macro`、`hkma`、`eia`、`host_search` | 仅接受授权宿主导入；不绕过登录、付费墙或权限 |

## 2. 删除与迁移

### 2.1 已从发行目录删除

```text
stock-data/scripts/quant_research/                     整个包（含 researchers/、validation/、news/）
stock-data/scripts/quant_research.py
stock-data/scripts/momentum_ab.py
stock-data/scripts/selection_ab.py
stock-data/scripts/research_b_ab.py
stock-data/scripts/research_candidates.py
stock-data/scripts/yahoo_collect.py
stock-data/references/quant-paths.md
stock-data/references/probability-policy.md
stock-data/references/momentum-model.md
stock-data/references/forward-ledger.md
stock-data/references/consensus-protocol.md
stock-data/references/researcher-contract.md
stock-data/references/finnhub-mcp-1.21.3-schema.md
```

`selfcheck.py` 已替换为 unittest discovery + AST 删除断言 + Markdown 链接检查，不再包含旧算法检查、退役表或量化测试夹具。

### 2.2 Gateway 清理

删除技术指标、期权异动/信号、空头成交排名、市场/框架筛选排名、Yahoo options/screen 及失去调用方的归一化/兼容映射。保留的能力限定为证券身份、公司资料、财务、公告、宏观和必要行情背景；52 周最高/最低仅作为报价字段，不再有 52 周采集门槛或预测用途。

### 2.3 依赖与文档

- `requirements.txt` 移除 `exchange_calendars` 和强制 Yahoo 引用，保留 `requests` 与 `tzdata`；Yahoo 为可选依赖。
- README、SKILL 和活跃 references 已迁移到 `news-1`；新增 `news-first-workflow.md`、`source-matrix.md` 与两个 request 示例。
- 所有本地 Markdown 链接检查通过。

## 3. 实际验收结果

### 3.1 离线测试

```text
python -B stock-data/scripts/selfcheck.py
Ran 121 tests in 4.653s
OK (skipped=1)
```

构成：`test_news_pipeline.py` 72、`test_sources.py` 42、`test_cli_integration.py` 5、发行目录检查 2。

覆盖计划 §12.2 的 N01–N24 离线行为，包括：无 OHLCV、请求 60 天只回 7 天、稀疏成功结果、403/429/超时、转载/双语/修正/撤回、PIT、数量不投票、目标特定宏观暴露、标题/传闻准入、空唯一事件、30 天扩窗、财务口径、虚构引用、缺失分析、外部注入文本、预算截断、缓存输入变化、幂等/中断/GC、排除证据重新准入、部分来源与无报价等。

跳过 1 项：Windows 创建 symlink 返回 WinError 1314（当前账户无 symlink 特权）。另有可移植模拟链接分类测试覆盖 GC 拒绝分支。

### 3.2 静态检查

```text
runtime/lint-tools-20261102/bin/ruff.exe check stock-data/scripts
All checks passed!

Python 3.10 AST 解析：34 个 .py 文件通过
git diff --check：无空白错误
```

Ruff 0.17.3 由用户授权从 PyPI 安装到 `runtime/lint-tools-20261102`，未修改全局 Python 环境。

### 3.3 CLI 全链路

真实执行（合成标识数据，不声称真实新闻）：

- 授权导入一条合成证据与完整回执 → `collect` 生成快照和 run。
- `prepare` 分批 → 宿主 assessment → `analyze` → `report` → `validate-report` 通过。
- 篡改 assessment 写入不支持的数字 → 分析失败，报告降级为“证据不足”，仍可通过交付校验。
- 同一导入重复执行及 `--incremental` → SQLite 记录幂等，两个 run 独立保存。
- `gc` 默认返回 dry-run，不删除共享数据库。
- 真实 network smoke（`0700.HK`，`max_requests=0`）：四个来源均为 `unavailable`，窗口为 `unknown`（没有任何可核实成功区间），`below_minimum_target=true`，报告明确“证据不足”，交付校验通过。
- `stock-data/examples/request-us.json` 与 `request-hk.json` 均通过实际 `validate_request`。

### 3.4 真实来源探测

仅完成美联储公开端点只读探测（未发送身份或凭证）：

- 2026-11-02T14:45:01.102137Z：2025-01-01 至 2025-01-31，6 次请求，3 条官方摘录（01-08、01-14、01-29），`success` / `exhausted=true` / 无 cap，完整成功区间。
- 2026-11-02T14:38:17.272749Z：同窗口一次归档超时，4 次请求，仅保留 01-29 一条摘录；`partial`，成功区间为空，失败原因为 `network_or_timeout`，未被 RSS 或文章跨度掩盖。
- 摘要保存在 [source-probes-20261102.json](source-probes-20261102.json)（本次运行副本在 `runtime/source-smoke-20261102/reported-probes.json`）；未保存原始完整响应，不能当作完整回放证据。

## 4. 未完成与已知缺口

1. **真实美股/港股公司样例未完成。** WebFetch 对 `www.apple.com` 与 `www.tencent.com` 均因域名安全验证不可用而失败；未取得可引用的公司正文，因此不生成样例报告，不伪造新闻或引用。
2. **SEC/Finnhub/FRED 账户未实测。** 未提供 `SEC_USER_AGENT`/`SEC_CONTACT` 或 API key；适配器只通过离线契约和 mock 测试。不能声称权限、分页完整或港股覆盖已验证。
3. **受限来源仍为导入模式。** HKEX、公司 IR、中国宏观、HKMA、EIA 未实现自动化采集；需要用户合法取得的公告/发布材料及查询回执。`host_search` 只作发现，不证明全网完整。
4. **能源、黄金、汇率和利率专题没有专用适配器。** 只能使用已授权的宿主导入或可选 gateway 背景行情；须标明现货/期货/基准、币种、单位和观测期，不得以黄金 ETF 价格冒充现货金价，也不据此声称完整宏观覆盖。
5. **事件归并边界。** 自动归并依赖来源提供的 `event_key`/`origin_id`、相同 URL 或完全相同标题。没有这些标识的改写转载无法自动合并；宿主必须避免把多篇转载当作独立确认。未来可在宿主侧增加语义归并，但不能用数量投票。
6. **模型判断质量未验收。** 回归使用合成 assessment 验证契约、引用和拒绝逻辑，不代表真实宏观传导或重要性排序的分析质量；未调用额外付费模型 API。
7. **Windows symlink 路径测试跳过。** 分类和拒绝逻辑有模拟测试，但未在具备 symlink 权限的账户上重跑。
8. **未做真实进程强杀/多进程竞争压力测试。** 幂等和中断恢复通过 SQLite 事务、manifest 最后写入和锁内异常模拟验证。
9. **`validate-report` 采用精确报告约束。** 实际发送文本必须与程序渲染的报告一致；宿主自行增删内容会被拒绝，需重新生成报告，而不是绕过校验。

## 5. 复现命令

```sh
python -B stock-data/scripts/news_research.py sources
python -B stock-data/scripts/news_research.py schema --kind request
python -B stock-data/scripts/news_research.py schema --kind import
python -B stock-data/scripts/news_research.py schema --kind assessment
python -B stock-data/scripts/selfcheck.py
runtime/lint-tools-20261102/bin/ruff.exe check stock-data/scripts
```

真实执行前需用户提供对应来源的身份/密钥并确认内容保存许可；没有权限时保留 `unavailable`/`partial` 回执，不以空新闻或合成数据冒充验收。

## 6. 变更状态

当前改动**未提交**。工作区中的旧量化文件已删除，但 Git 索引仍会显示为删除，提交后才会从仓库版本中消失；Git 历史未重写。不得在未完成真实来源验收的情况下把本报告表述为“真实美股/港股样例已通过”。
