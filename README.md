# Stock Data & Quant Research

`stock-data` 是面向美股、港股及相应 ETF 的可安装 Skill。行情、基本面、技术指标、专项数据经已连接的 Finnhub MCP 或本包 global gateway 获取；新闻唯一来源为宿主 Finnhub MCP `get-news-pulse`。量化研究脚本只处理已采集的数据，不联网更新快照。

普通个股/ETF 的“分析、简析、怎么看、综合分析”和方向判断默认执行 **数据验证 → Quant → 验证/审计 → News → 综合 → 交付校验**，使用 `intent=forecast`，horizon 固定 `5D`，窗口固定为 `(decision-364 个自然日, decision]`，价格基础为拆股调整、不含分红再投资。只有 A 路径（`quant`）在窗口覆盖完整、日历可验证、目标日可解析、当前/回放可重算且有效回放样本 ≥12（=有效条数/5）时才发布正式概率；B/C 为影子研究，D 为 5D 描述性诊断。简析只缩短篇幅，不省略 Quant。完整规则以 [SKILL.md](stock-data/SKILL.md) 及其引用文档为准。

## 安装

本 Skill 的主形态是**可挂载的源码目录**：`stock-data/` 内含 SKILL.md、references、Rust 工作区
（`scripts/rust/`：Cargo.toml/Cargo.lock/rust-toolchain.toml/crates/tests）、日历资源和唯一启动器
`scripts/run.sh`。运行不需要 Python；Python 兼容实现只保留在仓库的 `legacy/python/` 供差分开发。

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支安装 stock-data Skill，仓库内路径为 stock-data。
先读取 README.md 和 stock-data/SKILL.md，定位当前 Agent 实际支持的 Skill 安装目录（Codex 为
$CODEX_HOME/skills，未设置时 ~/.codex/skills；其他 Agent 不猜路径）。只安装 stock-data/ 整个目录：
它包含 SKILL.md、references/、scripts/（setup.sh、run.sh、rust/ 源码工作区）和 assets/calendars/。
不要只复制部分子目录，也不要从别处再克隆一份 Rust 源码。已有同名 Skill 时先备份再替换，不覆盖其他
Skill、配置、密钥或长期 memory。

安装后在该服务器上执行一次 stock-data/scripts/setup.sh：要求 Rust >= 1.99（工具链按
rust-toolchain.toml 固定 1.99.1）、Cargo 与 C 链接器；setup 只在此步骤联网 cargo fetch --locked
并预编译，之后常规运行离线。验证：scripts/run.sh --version、gateway --list、
selfcheck --offline（真实 executed 数必须为 68 且 failed 为空）。Skill 目录可只读；构建缓存、
provider 缓存和运行产物写在用户可写目录（默认 $XDG_CACHE_HOME/stock-data/target 与调用者 cwd）。
报告安装路径、Skill/runtime 版本、源码 commit 与 dirty 状态、rustc/cargo 版本和自检结果；按 Agent
加载机制刷新。此步骤不配置 Finnhub MCP，也不把离线自检表述为实时数据源连通成功。
```

安装结构：

```text
<Agent 的 Skill 目录>/stock-data/
  SKILL.md
  references/
  scripts/
    setup.sh            开发/安装准备：校验工具链、cargo fetch --locked、预编译
    run.sh              唯一常规入口，原样透传 argv
    rust/
      Cargo.toml
      Cargo.lock
      rust-toolchain.toml
      crates/           8 个 Rust crate（唯一权威工作区）
      tests/fixtures/   cargo 开发回归输入（离线自检也使用受控资源）
  assets/calendars/     冻结的 US/HK 交易所日历资源与 manifest
```

Rust 编译结果是 Skill 的内部执行缓存，不需要手动分发 exe。首次 setup 后断开依赖网络仍可运行；
源码修改由 `run.sh` 在下次调用时增量编译（release profile 已显式开启 incremental）。

`assets/calendars/` 是一并挂载的冻结日历资源（manifest + 逐文件摘要）。`collect yahoo` 与
`research freeze` 默认自动加载；需要显式覆盖时传 `--calendar-dir <dir>` 或设置
`STOCK_DATA_CALENDAR_DIR`。资源缺失时链路保持 `CALENDAR_UNVERIFIED`（不会用观察 bar 伪造日历），
摘要损坏/覆盖不足则直接拒绝；旧快照重载只读其已冻结的日历证据。

## 升级

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支将已安装的 stock-data Skill 升级到最新版。
只替换实际安装的 stock-data/ 整个目录，不在旧目录上叠加复制，也不单独替换 bin 或 Rust 源码子集。
先把新版下载到 Skill 扫描目录之外的暂存目录并记录 commit，比较旧版本与本地改动；校验新包
MANIFEST/SOURCE-STATE；把旧 Skill 完整备份到扫描目录之外，再原子替换。替换后在最终安装路径执行
stock-data/scripts/setup.sh（工具链/依赖变化时）与 scripts/run.sh selfcheck --offline，失败则恢复
旧包。升级不修改 Finnhub/MCP 配置、密钥、其他 Skill、用户运行产物或长期 memory；长期约束只需保存
“股票/ETF 分析优先使用 stock-data，方法和流程以当前 SKILL.md 为准”，不要在 memory 重复固化算法、
版本或路径。报告版本、commit、备份位置、未迁移改动和自检结果，按宿主机制重新加载。
```

## 运行

`$SKILL_DIR` 指该 Skill 的实际挂载路径（由宿主解析）。常规入口是 Skill 自带启动器，它从自身位置
解析 Skill root、保持调用者 cwd、原样透传参数，并把构建诊断留在 stderr。输入与输出位于用户可写
工作目录，不写入 Skill 安装目录。

### 数据采集

美股优先实际已连接且覆盖字段的 Finnhub；港股及缺项按 Skill 来源规则调用原生 gateway。Yahoo 结构化
数据由本 Skill 的原生 Rust runtime 直接请求并按登记的 legacy 路由回退，保留实际来源、fallback 状态和
partial 数据；没有等价路由时明确失败。可用函数和参数以 `gateway --list` 为准，见
[yfinance-data.md](stock-data/references/yfinance-data.md)。

```sh
"$SKILL_DIR/scripts/run.sh" gateway --function yahoo_history --params-file history-request.json --output history-envelope.json --cache-dir runtime/yfinance-cache
"$SKILL_DIR/scripts/run.sh" collect yahoo --request request.json --output gateway-responses.json --cache-dir runtime/yfinance-cache
```

`collect yahoo` 仅采行情。新闻由宿主 Finnhub 映射到 `news.data.articles[]`；未连接时记为
`not_assessed`，不二次联网补新闻。采集字段映射见 [collection-adapter.md](stock-data/references/collection-adapter.md)。
取得响应不代表通过预测数据门槛，不能补造收盘、复权、日历或单位证据。

### 离线研究与交付

```sh
"$SKILL_DIR/scripts/run.sh" research pipeline --request request.json --responses gateway-responses.json --output-root runtime --cleanup-json
"$SKILL_DIR/scripts/run.sh" research validate-response --analysis-dir <实际返回的_report_dir> --response-file <待发送的完整回复.md>
"$SKILL_DIR/scripts/run.sh" research cleanup-run --run-dir <实际返回的_run_dir> --analysis-dir <实际返回的_report_dir> --response-file <同一待发送的完整回复.md>
"$SKILL_DIR/scripts/run.sh" research gc --output-root runtime --dry-run
```

`pipeline` 完成 request → adapt → freeze → analyze。A 是唯一生产路径，B/C 影子研究，D 做 5D 单证券
诊断，E 审计；Quant 与 E 冻结后才运行独立 News，再冻结并综合。`pipeline` 默认用内置半衰期运行
News（`--news-policy` 可覆盖），不再默认跳过。News 不修改 Quant 概率。优先读取生成的
`final_response.md`，实际待发送文本必须通过校验；CLI `status: success` 仅表示产物写出，还需检查
`consensus.production_probability_eligible`、`consensus.evidence.directional_edge_state`、
`consensus.evidence.directional_edge_independent_supported` 和阶段回执。
`--artifact-profile compact` 只把 `analysis.json` 的逐条回放行改为引用
`researchers/<path>.json`，完整行仍保留在研究者产物中。

`news-candidates` 会同时冻结 `news_preview.json`；`analyze` 在 `news_input_digest` 一致时直接复用，
避免同一次工作流重复运行确定性 News 管线。`--focus major` 只导出 direct 且 major 的候选并记录
排除数，默认 `all` 保持全量覆盖。

`--cleanup-json` 将本次运行隔离到 `runtime/runs/<run_id>/`。宿主读取/复制已校验回复后，
`cleanup-run` 再次校验并仅删除本次登记的 JSON，保留 `report.md`、`final_response.md`、
`cleanup_receipt.txt`。可先加 `--dry-run` 预览。外部输入、其他运行和 provider 缓存都不在 run
清理范围内；失败/中断运行默认保留。需要复现或审计时使用 `--keep-json`；清理后不能再进行依赖原
JSON 的交付校验。

## 离线自检

```sh
"$SKILL_DIR/scripts/run.sh" gateway --list
"$SKILL_DIR/scripts/run.sh" collect yahoo --help
"$SKILL_DIR/scripts/run.sh" research --help
"$SKILL_DIR/scripts/run.sh" experiments --help
"$SKILL_DIR/scripts/run.sh" research schema --kind request
"$SKILL_DIR/scripts/run.sh" selfcheck --offline
```

自检只使用 Skill 内置/受控资源，不读取构建机绝对路径，也不需要 Python。未知的 `--check` ID 必须报错，
指定检查后 `executed` 数必须与选择一致；`executed=0` 不能算通过。自检输出会记录真实执行/失败/
未实现数量，不能以清单或帮助输出代替。

## 代码质量

Rust 工作区（`stock-data/scripts/rust`）的格式、静态检查和全部测试：

```sh
cd stock-data/scripts/rust
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
```

仓库 `legacy/python/` 中的 Python 只用于开发期差分对照（fixtures 生成、历史行为比较），不是 Skill
运行时依赖，也不需要装进用户环境。差分夹具重生成见 `tools/refactor/fixtures.py`。

## 边界与参考

- [专项分析路径](stock-data/references/analysis-paths.md)：事实、财务、技术、新闻、ETF 和多标的路由。
- [量化路径](stock-data/references/quant-paths.md)、[数据门槛](stock-data/references/reliability-gates.md)、[概率规则](stock-data/references/probability-policy.md)：当前实现、证据限制及降级规则。
- [审计规程](stock-data/references/audit-protocol.md)：可复现性与有效性的正交评分、独立第二实现、变异测试、有效样本量与门槛功效披露。
- [News 契约](stock-data/references/news-analysis.md)、[影响分级](stock-data/references/news-impact-rubric.md)：确定性事件筛查与关联，不产生融合概率，也不证明因果关系。
- [快照契约](stock-data/references/snapshot-schema.md)：冻结输入、完整性和输出路径边界。
- [来源限制](stock-data/references/source-policies.md)、[来源与许可记录](stock-data/references/upstream-provenance.md)：网络授权、来源声明及集成边界。

当前 Skill `3.4.0`，research runtime `0.24.0`，snapshot schema `1.3`，analysis schema `analysis-3.0`，forward ledger schema `ledger-3.1`。生产估计器为 `quant_drift_shrinkage_v2`（因果基率 + 波动率标定漂移先验收缩，`k=50`、`μ=6%/年`）；ROC20 仅作披露状态。52 周覆盖由冻结时的交易日历逐日重算，申报值只作对照；方向优势区分“窗口内回放”与“独立前向证据”，回放区间下界大于 0 不再等同于已支持。审计规则见 [audit-protocol.md](stock-data/references/audit-protocol.md)。没有实现正式 CPCV/PBO/DSR、多资产组合执行回测、XGBoost/LightGBM 或 ETF 穿透模型；缺失能力须记为未评估。合成数据和离线验收不构成真实市场预测改善证据。概率是有不确定性的研究结果，不是交易指令或收益保证。
