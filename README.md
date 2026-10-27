# Stock Data & Quant Research

`stock-data` 是面向美股、港股及相应 ETF 的可安装 Skill。行情、基本面、技术指标、专项数据经已连接的 Finnhub MCP 或本包 global gateway 获取；新闻唯一来源为宿主 Finnhub MCP `get-news-pulse`。量化研究脚本只处理已采集的数据，不联网更新快照。

普通个股/ETF 的“分析、简析、怎么看、综合分析”和方向判断默认执行 **数据验证 → Quant → 验证/审计 → News → 综合 → 交付校验**，使用 `intent=forecast`，horizon 固定 `5D`，窗口固定为 `(decision-364 个自然日, decision]`，价格基础为拆股调整、不含分红再投资。只有 A 路径（`quant`）在窗口覆盖完整、日历可验证、目标日可解析、当前/回放可重算且有效回放样本 ≥12（=有效条数/5）时才发布正式概率；B/C 为影子研究，D 为 5D 描述性诊断。简析只缩短篇幅，不省略 Quant。完整规则以 [SKILL.md](stock-data/SKILL.md) 及其引用文档为准。

## 安装

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支安装 stock-data Skill，仓库内路径为 stock-data。先读取 README.md 和 stock-data/SKILL.md，定位当前 Agent 实际支持的 Skill 安装目录；Codex 使用自带的 skill-installer，目录为 $CODEX_HOME/skills，未设置时为 ~/.codex/skills，其他 Agent 不猜路径。只安装 stock-data/ 整个目录，包含 scripts/、references/ 和两份 requirements，不改动其他 Skill、配置、密钥或长期 memory。已有同名 Skill 时不要覆盖，按升级流程处理。检查实际执行脚本的 Python 3.10+ 环境，按权限安装 requirements.txt 中的运行依赖，不自动改装其他全局环境。用已安装脚本的 --list、--help 和 schema --kind request 做离线自检，并检查 reference 链接。报告安装路径、Skill 版本、下载 commit、依赖和自检结果；按 Agent 加载机制刷新，Codex 提示重启。此步骤不配置 Finnhub MCP，也不把离线自检表述为实时数据源连通成功。
```

安装结构：

```text
<Agent 的 Skill 目录>/stock-data/
  SKILL.md
  requirements.txt
  requirements-yahoo.txt
  scripts/
  references/
```

在实际执行 gateway 的 Python 环境中安装依赖：

```sh
python -m pip install -r stock-data/requirements.txt
```

量化运行使用标准库、IANA 时区数据库和 `exchange_calendars`（XNYS/XHKG）验证交易所 session；Windows 缺时区数据库时需要 `tzdata`。未安装 `exchange_calendars` 时只能输出诊断，不能发布正式 5 交易日概率（`CALENDAR_UNVERIFIED`）。gateway 需要 `requests`；Yahoo 路由依赖由 `requirements-yahoo.txt` 引入并按需加载。Finnhub 连接、API key、网络权限与来源授权由宿主提供，见 [source-policies.md](stock-data/references/source-policies.md)。

## 升级

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支将已安装的 stock-data Skill 升级到最新版，只替换实际安装的 stock-data/，不在旧目录上叠加复制。先定位当前已安装的 stock-data 目录，将新版下载到 Skill 扫描目录之外的独立暂存目录并记录 commit，比较旧版本及本地改动。检查新版入口、依赖和参考链接，并执行离线自检。将旧 Skill 完整备份到 Skill 扫描目录之外，再替换为新版完整目录，仅迁移兼容的用户自定义配置。在最终安装路径重新自检，失败时恢复备份。升级不修改 Finnhub/MCP 配置、密钥、其他 Skill、用户运行产物或长期 memory；长期约束只需保存“股票/ETF 分析优先使用 stock-data，方法和流程以当前 SKILL.md 为准”，不要在 memory 重复固化算法、版本或路径。报告版本、commit、备份位置、未迁移改动和自检结果，按宿主机制重新加载。
```

## 运行

示例从仓库根目录执行；安装后使用已安装脚本的绝对路径。输入与输出位于用户可写工作目录，不写入 Skill 安装目录。

### 数据采集

美股优先实际已连接且覆盖字段的 Finnhub；港股及缺项按 Skill 来源规则调用 gateway。选择 Yahoo 时强制优先 `yahoo_*` yfinance 路由，失败后仅在网络授权与请求限额允许时使用语义等价的旧接口，保留实际来源和失败原因；不提供后端切换，不覆盖已有 partial 数据。可用函数和参数以 `--list` 为准，见 [yfinance-data.md](stock-data/references/yfinance-data.md)。

```sh
python stock-data/scripts/global_stock_data.py --function yahoo_history --params-file history-request.json --output history-envelope.json --cache-dir runtime/yfinance-cache
python stock-data/scripts/yahoo_collect.py --request request.json --output gateway-responses.json --cache-dir runtime/yfinance-cache
```

`yahoo_collect.py` 仅采行情。新闻由宿主 Finnhub 映射为 `news.data.articles[]`；未连接时记为 `not_assessed`，不二次联网补新闻。采集字段映射见 [collection-adapter.md](stock-data/references/collection-adapter.md)。取得响应不代表通过预测数据门槛，不能补造收盘、复权、日历或单位证据。

### 离线研究与交付

```sh
python stock-data/scripts/quant_research.py pipeline --request request.json --responses gateway-responses.json --output-root runtime --cleanup-json
python stock-data/scripts/quant_research.py validate-response --analysis-dir <实际返回的_report_dir> --response-file <待发送的完整回复.md>
python stock-data/scripts/quant_research.py cleanup-run --run-dir <实际返回的_run_dir> --analysis-dir <实际返回的_report_dir> --response-file <同一待发送的完整回复.md>
python stock-data/scripts/quant_research.py gc --output-root runtime --dry-run
```

`pipeline` 完成 request → adapt → freeze → analyze。A 是唯一生产路径，B/C 影子研究，D 做 5D 单证券诊断，E 审计；Quant 与 E 冻结后才运行独立 News，再冻结并综合。`pipeline` 默认用内置半衰期运行 News（`--news-policy` 可覆盖），不再默认跳过。News 不修改 Quant 概率。优先读取生成的 `final_response.md`，实际待发送文本必须通过校验；CLI `status: success` 仅表示产物写出，还需检查 `consensus.production_probability_eligible`、`consensus.evidence.directional_edge_state`、`consensus.evidence.directional_edge_independent_supported` 和阶段回执。`--artifact-profile compact` 只把 `analysis.json` 的逐条回放行改为引用 `researchers/<path>.json`，完整行仍保留在研究者产物中。

`news-candidates` 会同时冻结 `news_preview.json`；`analyze` 在 `news_input_digest` 一致时直接复用，避免同一次工作流重复运行确定性 News 管线。`--focus major` 只导出 direct 且 major 的候选并记录排除数，默认 `all` 保持全量覆盖。

`--cleanup-json` 将本次运行隔离到 `runtime/runs/<run_id>/`。宿主读取/复制已校验回复后，`cleanup-run` 再次校验并仅删除本次登记的 JSON，保留 `report.md`、`final_response.md`、`cleanup_receipt.txt`。可先加 `--dry-run` 预览。外部输入、其他运行和 provider 缓存都不在 run 清理范围内；失败/中断运行默认保留。需要复现或审计时使用 `--keep-json`；清理后不能再进行依赖原 JSON 的交付校验。

清理前会先删除两类崩溃残留：原子写临时文件 `.<文件名>.<12位hex>.tmp` 与快照构建临时目录 `.<snapshot_id>.<8位随机>`；其他未知文件仍会阻断删除。宿主拿不到实际发送文本时可用 `cleanup-run --assume-delivered`，回执记 `delivery=unverified`。`gc` 负责长期保留：默认把超过 1 天仍未交付的 `awaiting_cleanup` 运行补清理，把超过 30 天、回执 `complete` 且已无 JSON 的 run 目录整体删除，并按 7 天清理 provider 缓存；`pipeline` 启动前会节流执行同一套清扫（两种布局模式都触发，每 24 小时一次，`--no-gc` 关闭）。链接、未知文件、未完成回执和存活进程一律跳过并给出原因。逐阶段命令与更多参数见 `--help`。

## 离线自检

以下命令不访问市场，不生成行情或研究报告：

```sh
python -B stock-data/scripts/global_stock_data.py --list
python -B stock-data/scripts/yahoo_collect.py --help
python -B stock-data/scripts/quant_research.py --help
python -B stock-data/scripts/quant_research.py gc --help
python -B stock-data/scripts/quant_research.py schema --kind request
python -B stock-data/scripts/selfcheck.py
```

前五步只确认入口、参数和请求 schema 可用，不验证实时来源连通或预测有效性；`selfcheck.py` 额外用合成数据运行离线回归校验，且**登记表、退役表和已定义函数必须互相覆盖**（未登记或未退役的检查会直接失败）。T01–T35 覆盖固定 5 日 horizon、52 周窗口、窗口冲突、越界 K 线、价格基础、目标 session、日历、标签成熟、有效样本、当前概率重算、transform/参数/版本篡改、台账、单生产路径、影子隔离、因子/ML 边界、新闻三态与注入防护；T36–T45 覆盖覆盖重算与伪造申报、抽样三态、veto 全字段清除、前向录入时限与模型版本隔离、拆股与价格序列对账、partial AI 新闻交付、警告分级、登记完整性、版本一致性与 compact 产物；F01/F02/F39–F42/F46 覆盖 ML 前缀不变性、台账语义、影子校准禁用、实时=回放与 v2 收缩公式。仓库内历史检查若测试已被移除的功能，列在 `RETIRED_CHECKS` 并写明原因，不执行也不隐藏。合成数据只验证流程，不构成效能证据；效能结论必须来自真实数据并附有效样本量与区间。

## 代码质量

```sh
python -m pip install ruff
ruff check stock-data/scripts
```

`pyproject.toml` 固定 lint 契约，不是可选的风格偏好：`target-version = "py310"` 与运行环境下限一致（因此不自动套用 3.11 才有的 `datetime.UTC`、原生 `Z` 解析等写法）；`line-length = 120`；忽略项都带理由——单行数据契约字面量（E501）、CLI 统一用 `ValueError` 传递机器可读文案（TRY003/TRY301）、中文全角标点（RUF001-003）。刻意保留的写法用带理由的 `# noqa` 就地标注，`ruff check` 应保持零告警。

JSON 序列化集中在 `quant_research/jsonio.py`：快照 manifest、冻结阶段结果和清理回执都对 JSON 字节取哈希，键序、分隔符、NaN 策略与结尾换行属于磁盘契约，只允许在一个地方定义。

## 边界与参考

- [专项分析路径](stock-data/references/analysis-paths.md)：事实、财务、技术、新闻、ETF 和多标的路由。
- [量化路径](stock-data/references/quant-paths.md)、[数据门槛](stock-data/references/reliability-gates.md)、[概率规则](stock-data/references/probability-policy.md)：当前实现、证据限制及降级规则。
- [审计规程](stock-data/references/audit-protocol.md)：可复现性与有效性的正交评分、独立第二实现、变异测试、有效样本量与门槛功效披露。
- [News 契约](stock-data/references/news-analysis.md)、[影响分级](stock-data/references/news-impact-rubric.md)：确定性事件筛查与关联，不产生融合概率，也不证明因果关系。
- [快照契约](stock-data/references/snapshot-schema.md)：冻结输入、完整性和输出路径边界。
- [来源限制](stock-data/references/source-policies.md)、[来源与许可记录](stock-data/references/upstream-provenance.md)：网络授权、来源声明及集成边界。

当前 Skill `3.3.1`，research runtime `0.23.0`，snapshot schema `1.3`，analysis schema `analysis-3.0`，forward ledger schema `ledger-3.1`。生产估计器为 `quant_drift_shrinkage_v2`（因果基率 + 波动率标定漂移先验收缩，`k=50`、`μ=6%/年`）；ROC20 仅作披露状态。52 周覆盖由冻结时的交易日历逐日重算，申报值只作对照；方向优势区分“窗口内回放”与“独立前向证据”，回放区间下界大于 0 不再等同于已支持。审计规则见 [audit-protocol.md](stock-data/references/audit-protocol.md)。没有实现正式 CPCV/PBO/DSR、多资产组合执行回测、XGBoost/LightGBM 或 ETF 穿透模型；缺失能力须记为未评估。合成数据和离线验收不构成真实市场预测改善证据。概率是有不确定性的研究结果，不是交易指令或收益保证。
