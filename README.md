# Stock Data & News Analysis

`stock-data` 是面向美股、港股及相应 ETF 的可安装 Skill。行情与基本面经已连接的 Finnhub MCP 或本包 global gateway 获取；新闻消息面使用 **SEC EDGAR + 公司 IR RSS + Finnhub 免费档**，核实使用 **TinyFish Search/Fetch**。研究脚本只处理已采集的数据，不联网更新快照。

普通个股/ETF 的“分析、简析、怎么看、综合分析”默认执行 **数据验证 → 新闻采集 → 核实 → 描述性汇总 → 交付校验**。本技能**不提供方向预测或买卖/仓位建议**。旧 `intent=forecast` / `horizon=5D` 请求返回结构化错误 `FORECAST_REMOVED`（退出码非 0）。完整规则以 [SKILL.md](stock-data/SKILL.md) 及其引用文档为准。

## 安装

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支安装 stock-data Skill，仓库内路径为 stock-data。先读取 README.md 和 stock-data/SKILL.md，定位当前 Agent 实际支持的 Skill 安装目录；Codex 使用自带的 skill-installer，目录为 $CODEX_HOME/skills，未设置时为 ~/.codex/skills，其他 Agent 不猜路径。只安装 stock-data/ 整个目录，包含 scripts/、references 和 requirements 文件，不改动其他 Skill、配置、密钥或长期 memory。已有同名 Skill 时不要覆盖，按升级流程处理。检查实际执行脚本的 Python 3.10+ 环境，按权限安装 requirements.txt 中的运行依赖，不自动改装其他全局环境。用已安装脚本的 --help 和 schema --kind request 做离线自检，并检查 reference 链接。报告安装路径、Skill 版本、下载 commit、依赖和自检结果；按 Agent 加载机制刷新，Codex 提示重启。此步骤不配置 Finnhub/TinyFish，也不把离线自检表述为实时来源连通成功。
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
python3 -m venv .venv && .venv/bin/pip install -r stock-data/requirements.txt
```

运行使用标准库、IANA 时区数据库和可选的 `exchange_calendars` / `pandas_market_calendars`。缺少交易日历库时显式输出 `CALENDAR_LIB_MISSING`，价格反应描述窗只作未验证披露，不静默降级。gateway 需要 `requests`；Yahoo 路由依赖由 `requirements-yahoo.txt` 引入并按需加载。`SEC_USER_AGENT`、`FINNHUB_API_KEY`、`TINYFISH_API_KEY` 只经环境变量读取，缺失时对应来源为 `not_configured` / `not_assessed`，不得伪造。

## 升级

复制以下指令给支持本地 Skill 的 Agent：

```text
请从 https://github.com/x0fun1/stock-data 的当前默认分支将已安装的 stock-data Skill 升级到最新版，只替换实际安装的 stock-data/，不在旧目录上叠加复制。先定位当前已安装的 stock-data 目录，将新版下载到 Skill 扫描目录之外的独立暂存目录并记录 commit，比较旧版本及本地改动。检查新版入口、依赖和参考链接，并执行离线自检。将旧 Skill 完整备份到 Skill 扫描目录之外，再替换为新版完整目录，仅迁移兼容的用户自定义配置。在最终安装路径重新自检，失败时恢复备份。升级不修改来源配置、密钥、其他 Skill、用户运行产物或长期 memory；长期约束只需保存“股票/ETF 分析优先使用 stock-data，方法和流程以当前 SKILL.md 为准”，不要在 memory 重复固化算法、版本或路径。报告版本、commit、备份位置、未迁移改动和自检结果，按宿主机制重新加载。
```

## 运行

示例从仓库根目录执行；安装后使用已安装脚本的绝对路径。输入与输出位于用户可写工作目录，不写入 Skill 安装目录。

### 行情采集

```sh
python3 stock-data/scripts/yahoo_collect.py --request request.json --output gateway-responses.json --cache-dir runtime/yfinance-cache
```

`yahoo_collect.py` 仅采行情。Yahoo 历史元数据提供 currency（例如 USD），不提供成交量/交易单位：currency 作为证据保留，`unit` 保持 null 并披露；不再仅因缺 unit 输出 `policy_status=ineligible`。缺失单位只降低披露质量，不等于数据不可用。

### 新闻采集与核实

```sh
python3 stock-data/scripts/news_collect.py --ticker GOOGL --as-of 2026-11-01T21:00:00Z --output news_collected.json
python3 stock-data/scripts/news_verify.py --candidates news_collected.json --output news_verified.json
```

固定窗口为 `(as-of − 60 天, as-of]`；另统计最近 30 天与第 31–60 天。每个来源独立报告状态，IR feed 未覆盖窗口时标 `partial_window`。SEC 仅美股；Form 4/144 只汇总笔数。Yahoo Finance 与 The Economist 仅作静态参考链接，程序不请求。

### 离线研究与交付

```sh
python3 stock-data/scripts/quant_research.py adapt --request request.json --responses gateway-responses.json --output collected.json
python3 stock-data/scripts/quant_research.py freeze --input collected.json --output-root runtime/snapshots
python3 stock-data/scripts/quant_research.py news-candidates --snapshot-dir <snapshot_dir> --collected news_collected.json --verification news_verified.json --output news_candidates.json
# 宿主 AI 逐条填写 news_ai_assessment.json（lean/reason/strength/event_type/evidence_urls）；ID 与 as-of 必须与候选文件一致
python3 stock-data/scripts/quant_research.py analyze --snapshot-dir <snapshot_dir> --news-collected news_collected.json --news-verification news_verified.json --news-ai-assessment news_ai_assessment.json --output-root runtime/research
python3 stock-data/scripts/quant_research.py validate-response --analysis-dir <report_dir> --response-file <待发送的完整回复.md>
python3 stock-data/scripts/quant_research.py cleanup-run --run-dir <run_dir> --analysis-dir <report_dir> --response-file <同一待发送的完整回复.md>
python3 stock-data/scripts/quant_research.py gc --output-root runtime --dry-run
```

`analyze` 生成 `analysis.json`、`report.md`、`agent_summary.json` 和 `final_response.md`。新闻逐条判断由宿主 AI 提供；程序只做域名/日期过滤、`strength_v1` 规则复核与描述性矩阵，不输出概率、综合分数或方向结论。`final_response.md` 必须含固定行并通过 `validate-response`；矩阵条数必须与 `analysis.json` 一致。

`--cleanup-json` 将 pipeline 运行隔离到 `runtime/runs/<run_id>/`；`cleanup-run` 再次校验并仅删除本次登记的 JSON，保留 `report.md`、`final_response.md`、`cleanup_receipt.txt`。`gc` 负责长期保留与 provider 缓存清理。

## 离线自检

以下命令不访问市场、不生成行情或研究报告：

```sh
python3 -B stock-data/scripts/yahoo_collect.py --help
python3 -B stock-data/scripts/news_collect.py --help
python3 -B stock-data/scripts/news_verify.py --help
python3 -B stock-data/scripts/quant_research.py --help
python3 -B stock-data/scripts/quant_research.py schema --kind request
python3 -B stock-data/scripts/selfcheck.py
python3 -m unittest discover -s stock-data/tests -p 'test_*.py' -v
```

`selfcheck.py` 用合成数据运行离线回归校验；`tests/` 使用从官方文档字段格式制作的录制夹具覆盖 SEC/IR/Finnhub 解析、TinyFish 核实、strength 矩阵与六类交付篡改。合成数据只验证流程，不构成真实来源连通或统计有效性证据。

## 代码质量

```sh
python3 -m pip install ruff
ruff check stock-data/scripts
```

`pyproject.toml` 固定 lint 契约：`target-version = "py310"`、`line-length = 120`。刻意保留的写法用带理由的 `# noqa` 就地标注，`ruff check` 应保持零告警。

## 边界与参考

- [分析路径](stock-data/references/analysis-paths.md)：报价、基本面、新闻与描述性统计路由。
- [新闻规则](stock-data/references/news-analysis.md)、[影响分级与 strength_v1](stock-data/references/news-impact-rubric.md)。
- [采集契约](stock-data/references/collection-adapter.md)、[快照契约](stock-data/references/snapshot-schema.md)。
- [来源限制](stock-data/references/source-policies.md)、[来源与许可记录](stock-data/references/upstream-provenance.md)。
- [审计规程](stock-data/references/audit-protocol.md)。

当前 Skill `4.0.0`，research runtime `4.0.0`，snapshot schema `1.3`，analysis schema `analysis-4.0`，新闻规则 `strength_v1`。本版本移除了 52 周/5 日方向预测、概率估计、因子/ML 影子路径和前向台账；保留行情与基本面采集、冻结与摘要校验、新闻采集/核实/描述性汇总、`validate-response` 与 `cleanup-run`。Finnhub 内容仅限个人使用、不得再分发。技能输出不构成交易指令、收益保证或买卖/仓位建议。
