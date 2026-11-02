# Stock Data — News Research

`stock-data` 是面向美股、港股及相应 ETF 的新闻消息面研究 Skill。普通“分析、简析、怎么看”默认执行 **来源规划 → 历史采集/授权导入 → 冻结证据 → 宿主离线分析 → 报告与交付校验**。

当前版本为 **4.0.0 / news-1**。默认最近 **60 个自然日**，支持显式 30～60 天；没有 OHLCV 但有足够新闻证据也可完成研究。分析宏观、公司事件和财务背景，形成利多/利空/中性或证据不足的判断，选择唯一合格重大近期事件或明确返回空值。结论针对已知消息影响，不提供价格预测、数值情绪、技术指标、排名筛选或交易策略入口。完整流程见 [SKILL.md](stock-data/SKILL.md) 和 [新闻工作流](stock-data/references/news-first-workflow.md)。

## 安装

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支安装 stock-data Skill，仓库内路径为 stock-data。先读取 README.md 和 stock-data/SKILL.md，定位当前 Agent 实际支持的 Skill 安装目录；Codex 使用自带的 skill-installer，目录为 $CODEX_HOME/skills，未设置时为 ~/.codex/skills，其他 Agent 不猜路径。只安装 stock-data/ 整个目录，包含 scripts/、references/、examples/ 和两份 requirements，不改动其他 Skill、配置、密钥或长期 memory。已有同名 Skill 时不要覆盖，按升级流程处理。检查实际执行脚本的 Python 3.10+ 环境，按权限安装 requirements.txt 中的运行依赖，不自动改装其他全局环境。用已安装的 news_research.py --help、sources、schema --kind request、schema --kind assessment、schema --kind import 及 global_stock_data.py --list 做离线自检，并检查 reference 链接。报告安装路径、Skill 版本、下载 commit、依赖和自检结果；按 Agent 加载机制刷新，Codex 提示重启。此步骤不配置 Finnhub MCP，也不把离线自检表述为实时数据源连通成功。
```

安装结构：

```text
<Agent 的 Skill 目录>/stock-data/
  SKILL.md
  requirements.txt
  requirements-yahoo.txt
  scripts/
  references/
  examples/
```

在实际执行 gateway 的 Python 环境中安装依赖：

```sh
python -m pip install -r stock-data/requirements.txt
```

核心运行使用 Python 3.10+、HTTP 依赖与 IANA 时区数据库（包括 Windows 的 `tzdata`）。**Yahoo 是可选背景来源**；只有需要且获得安装/访问授权时再运行 `python -m pip install -r stock-data/requirements-yahoo.txt`，不自动改动其他 Python 环境。新闻来源由 request 显式规划，允许已授权历史适配器和宿主证据导入；账户、网络权限及内容保存许可分别核对，见 [来源矩阵](stock-data/references/source-matrix.md) 与 [source-policies.md](stock-data/references/source-policies.md)。

下载或解压的仓库放在 Skill 扫描目录之外的新空暂存目录；核对文件与 commit 后再执行受信任脚本，不从未经检查的下载目录启动解释器或构建工具。

## 升级

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支将已安装的 stock-data Skill 升级到最新版，只替换实际安装的 stock-data/，不在旧目录上叠加复制。先定位当前已安装的 stock-data 目录，将新版下载到 Skill 扫描目录之外的独立暂存目录并记录 commit，比较旧版本及本地改动。检查新版入口、依赖和参考链接，并执行离线自检。将旧 Skill 完整备份到 Skill 扫描目录之外，再替换为新版完整目录，仅迁移兼容的用户自定义配置。在最终安装路径重新自检，失败时恢复备份。升级不修改 Finnhub/MCP 配置、密钥、其他 Skill、用户运行产物或长期 memory；长期约束只需保存“股票/ETF 分析优先使用 stock-data，方法和流程以当前 SKILL.md 为准”，不要在 memory 重复固化算法、版本或路径。报告版本、commit、备份位置、未迁移改动和自检结果，按宿主机制重新加载。
```

## 运行

示例从仓库根目录执行；安装后使用已安装脚本的绝对路径。输入与输出位于用户可写工作目录，不写入 Skill 安装目录。

### 新闻采集、离线分析与交付

先读取 `sources` 与三份 schema，按 [新闻工作流](stock-data/references/news-first-workflow.md) 创建 request 和授权导入材料。可复制 [美股请求](stock-data/examples/request-us.json) 或 [港股请求](stock-data/examples/request-hk.json)，并更新截止时间与实际来源配置；这些是配置示例，不是已验收的真实市场报告。`RUN` 为 collect 回执返回的实际目录。

```sh
python stock-data/scripts/news_research.py sources
python stock-data/scripts/news_research.py schema --kind request
python stock-data/scripts/news_research.py schema --kind import
python stock-data/scripts/news_research.py schema --kind assessment
python stock-data/scripts/news_research.py collect --request request.json --imports evidence.json --output-root runtime
python stock-data/scripts/news_research.py prepare --run-dir RUN
# 宿主离线阅读冻结证据，按 schema 生成 host-assessment.json
python stock-data/scripts/news_research.py analyze --run-dir RUN --assessment host-assessment.json
python stock-data/scripts/news_research.py report --run-dir RUN
python stock-data/scripts/news_research.py validate-report --run-dir RUN --response-file reply.md
python stock-data/scripts/news_research.py gc --output-root runtime --dry-run --retention-days 120
```

适配器采集可省略 `--imports`；后续更新可加 `collect --incremental`。宿主只能引用登记 ID，不自行写来源标题、URL、日期或数字。分析阶段不联网替换冻结输入；模型输出缺失或非法时报告分析未完成，不自动生成中性结论。

报告披露请求区间、实际来源/正文覆盖、候选与已分析/遗漏数量。60 天只取回 7 天仍为部分覆盖；来源注册和 HTTP 成功不证明全网完整。近期唯一重大事件优先最近 7 天，无合格候选扩至 30 天并披露，再无候选则为空。

共享证据库与单次运行分开；许可允许时默认保留至少 120 天所需证据。不要在交付后清空 JSON。`gc` 默认 dry-run；先审阅 `gc --dry-run`，实际清理必须经用户授权并显式添加 `--apply`，且受路径、引用、已登记产物与许可约束，不清理外部输入、未知文件或共享数据库。

### 可选事实背景

仅在解释新闻需要或用户明确查询时调用 [global gateway](stock-data/references/global-stock-data.md) 的证券身份、报价、财务、公告、宏观等接口。Yahoo 路由按需安装，保留实际来源、失败和 partial 数据；日期、币种、单位、发布与观测期不可猜测。行情或财务背景不是新闻完整覆盖的证明。

## 离线自检

以下命令不访问市场，不生成行情或研究报告：

```sh
python -B stock-data/scripts/global_stock_data.py --list
python -B stock-data/scripts/news_research.py --help
python -B stock-data/scripts/news_research.py sources
python -B stock-data/scripts/news_research.py schema --kind request
python -B stock-data/scripts/news_research.py schema --kind assessment
python -B stock-data/scripts/news_research.py schema --kind import
python -B stock-data/scripts/news_research.py gc --help
python -B stock-data/scripts/selfcheck.py
```

入口/schema 检查只确认离线契约可用；新闻回归应覆盖无 OHLCV、US/HK/ETF、30～60 天窗口、部分来源、历史分页、PIT、去重/更新、证据准入、非法引用、宿主注入和安全清理。以本次真实执行结果为准，不将合成数据或自检成功称为实时数据源连通、账户权限已确认或真实样例已验收。

## 代码质量

```sh
python -m pip install ruff
ruff check stock-data/scripts
```

`pyproject.toml` 固定 lint 契约，不是可选的风格偏好：`target-version = "py310"` 与运行环境下限一致（因此不自动套用 3.11 才有的 `datetime.UTC`、原生 `Z` 解析等写法）；`line-length = 120`；忽略项都带理由——单行数据契约字面量（E501）、CLI 统一用 `ValueError` 传递机器可读文案（TRY003/TRY301）、中文全角标点（RUF001-003）。刻意保留的写法用带理由的 `# noqa` 就地标注，`ruff check` 应保持零告警。

冻结输入与报告的摘要、路径边界和 JSON 写入由 `news_research/common/` 及新闻存储模块统一处理；不要由宿主重写证据 ID、摘要或来源元数据。

## 边界与参考

- [新闻工作流](stock-data/references/news-first-workflow.md)：request、CLI、宿主离线分析、交付和保留策略。
- [来源矩阵](stock-data/references/source-matrix.md)、[来源限制](stock-data/references/source-policies.md)：历史能力、权限和本次覆盖缺口。
- [分析路径](stock-data/references/analysis-paths.md)：新闻、最小事实查询与 ETF 暴露。
- [新闻证据](stock-data/references/news-analysis.md)、[重大影响规则](stock-data/references/news-impact-rubric.md)：宏观传导、准入、唯一重大事件和综合判断。
- [采集映射](stock-data/references/collection-adapter.md)、[快照契约](stock-data/references/snapshot-schema.md)、[时点政策](stock-data/references/point-in-time-policy.md)：来源、日期、数字和冻结证据边界。
- [验收规程](stock-data/references/audit-protocol.md)、[证据门槛](stock-data/references/reliability-gates.md)、[上游记录](stock-data/references/upstream-provenance.md)：校验与能力声明限制。

版本 `4.0.0 / news-1` 是不兼容入口迁移；旧运行产物不直接作为新证据导入。证据不足时明确保留缺口，不以模型推断补造事实。本说明不代表已完成真实联网验收；研究不是交易指令或收益保证。
