---
name: stock-research
description: Evidence-first US/HK news, macro and company-message research using local Search/Fetch. Use to collect headlines and source evidence, review each item, grade its direction and impact, and synthesize a cited message report. No price or K-line model, no probability forecast, no trading execution or position sizing.
---

# Stock Research Skill

美股/港股消息研究 Skill：采集新闻、宏观消息和公司消息，逐条核对原文证据，给出方向与影响强度的解读，并汇总成一条带引用的报告。

本包不含 K 线/量价模型、多因子、概率模型或任何模型权重，不输出价格预测或上涨概率。

## 安装

需要 Python 3.12 以上。安装器一并安装 `requests`、DDGS 搜索和 Trafilatura 正文抽取依赖；数据导入、压缩 JSON 存储和 SQLite 索引均使用 Python 标准库。从 skill 目录执行：

```bash
python scripts/install_skill.py --destination ~/.claude/skills/stock-research
```

安装器复制 skill 源码、在目标目录内创建 `.venv`、安装本地 Search/Fetch 依赖，并自检“导入审核 → 生成报告”、DDGS 导入及 Trafilatura 正文抽取。输出本地服务启动命令，服务由安装助手或用户启动。目标已有本 skill 时用显式 `--update`；不改全局 Python，不在目标目录留 pip 缓存。首次依赖安装需要联网。

执行或编写安装指令时使用[安装 Prompt](references/install-prompt.md)，一并部署本地 Search/Fetch 并验证 `/health` 与真实公开网页。默认监听 `127.0.0.1:8765`，标题、搜索和抓取并发均为 **32**。本地应用使用原生 Python，无须 Docker 或 TinyFish；SearXNG 仅作可选搜索后端。

安装后所有命令使用 `python scripts/skill.py ...`，它调用本 skill 内的 `.venv`。若只开发源码且已有相容环境，可直接运行 `research.py`。来源账户凭证和运行数据不随包分发。

从旧量化版本迁移时，先用旧环境运行 `python scripts/migrate_runtime.py --root MY-DATA` 转换保留的数据，再安装到新的空目录以得到干净的轻量环境。普通 `--update` 不删除旧权重或旧环境中的额外依赖；不要把更新源码误当成释放这些空间。

## 使用条件

使用本 Skill 当用户要求：

- 解读某只美股或港股近期的新闻、公告和公司消息；
- 判断一条或一批消息对个股是利多、利空还是中性，以及影响大小；
- 结合已公开的宏观读数（利率曲线、通胀、汇率、政策利率）给出消息背景；
- 导入用户已有或已授权的新闻、宏观数据并复现既有报告。

不使用本 Skill 做价格/K 线预测、概率预测、交易执行、仓位配置、图表生成、港股 Yahoo 数据抓取或 HKEXnews 自动采集。

## 核心原则

1. **先证据，后结论。** 每个结论能追溯到原文 URL、证据定位、发布时间和实际核实时间。
2. **标题不是事实。** 只有标题或摘要的记录标为“证据不足”，不升级为方向结论。
3. **采集与分析分开。** 采集由程序完成且不调用大语言模型；大语言模型在证据表完成后逐条分析。
4. **方向与强度分开判断。** 七档方向和影响大小各有理由，不从语气推定重大。
5. **时点严格。** 报告只使用知识截止前已公开、已核实的信息；后来才建立的核实不回填旧报告。
6. **缺口不是中性。** 无法核实、无关联和中性分别记录。
7. **不换算概率。** 新闻贡献是可审计的研究分数，不是上涨概率，也不给买卖建议。
8. **市场独立。** 美股与港股分别处理；港股公告只来自用户提供或许可材料。

## 命令

在仓库根目录或安装后的 skill 目录运行；也可用 `PYTHONPATH=scripts python scripts/research.py ...`。

```bash
# 消息研究报告：宏观背景、逐条新闻解读、综合判断、缺口
python scripts/skill.py research --root MY-DATA --market US --ticker NVDA --news-lookback 30

# 导入用户授权文件（CSV/JSON），不联网
python scripts/skill.py import --root MY-DATA --kind news --file news.json --market US
python scripts/skill.py import --root MY-DATA --kind macro --file macro.csv --market US
python scripts/skill.py import --root MY-DATA --kind securities --file securities.csv --market US

# 直接导入带引用、事实、判断理由和核实时间的新闻分析
python scripts/skill.py import-news-reviews --root MY-DATA --file news-reviews.json

# 逐标题持久队列；采集后逐项判断，循环 apply 直到没有待办
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step prepare
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step next --limit 8
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step apply --file assessments.json
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step finalize

# 本地 Search/Fetch（默认）：安装器已包含依赖，在独立终端启动服务
python scripts/skill.py news-local-serve
# 另一个终端采集；默认 local、32 并发，先 Fetch 已有 URL，失败时 Search
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id COLLECTION-ID

# 可选已连接 TinyFish 插件：导出标题计划，宿主执行一次固定批量驱动，再导入其联网证据
python scripts/skill.py news-plugin-plan --root MY-DATA --workflow-id NEWS-ID --batch-id BATCH-ID
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id COLLECTION-ID --provider tinyfish-plugin --plugin-batch-id BATCH-ID

# 独立 API 方式：鉴权读取本地 TINYFISH_API_KEY，不把凭证写入包或命令参数
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id COLLECTION-ID --provider tinyfish

# 查看数据登记状态（不打印凭证）；复现既有运行
python scripts/skill.py doctor --root MY-DATA
python scripts/skill.py reproduce --root MY-DATA --run-id run-...
```

`--root` 指定运行数据目录；默认使用当前 skill 目录内的 `runtime/`，也可通过 `STOCK_RESEARCH_RUNTIME` 指定。

## 运行流程

### 1. 准备消息

`news` 数据集保存发现记录（标题、来源、URL、时间、证券）。来源可以是用户导入文件，或用包内 `FinnhubClient` 取得的标题。`security_ids` 为空的记录视为全市场消息（宏观、政策），对同一市场的所有证券可见。宏观数值用 `import --kind macro` 导入，或用 `FredClient` 取得带 vintage 的序列。

### 2. 采集原文证据

有新的待核实标题时，用 `news-workflow --step prepare` 冻结任务清单；既支持 Finnhub 标题，也支持用户导入的消息。港股传 `--market HK --ticker 0700`，不自动抓取 HKEXnews 公告。默认采用[本地 Search/Fetch](references/local-search-fetch.md)：先启动 `news-local-serve`，再用 `news-collect` 按默认 32 并发采集；优先 Fetch 已有 URL，失败时 Search 原标题及公司/日期组合，无固定搜索间隔。可用 `--workers`、`--search-workers`、`--fetch-workers` 调低上限。可选 TinyFish 插件用 `news-plugin-plan` 及[固定批量驱动](references/tinyfish-plugin-batch.md)，完成后 `news-collect --provider tinyfish-plugin` 导入；只有独立 TinyFish API 方式才使用 `TINYFISH_API_KEY`。遍历、失败回退和落盘由程序完成，不让大语言模型逐条决定或发起采集调用。`--provider archive` 只重放旧证据，不能说成联网采集。

### 3. 逐条解读

读取证据表后分批分析，重复 `next`/`apply` 直到所有标题进入终态，再 `finalize`。每条给出 `verified`、`unrelated` 或 `unresolved`：

- `verified` 保留事实与待确认条件、七档方向及理由、影响强度及理由、原始信息时间、实际核实时间和事件聚类；
- `unrelated` 用于其他公司的消息或仅在标签/广告中提及；
- `unresolved` 保留失败来源和具体缺口，不进入方向贡献。

内容或日期缺口追加新的采集任务，不把不足内容升级成完整正文。字段格式、衰减与综合规则见[新闻证据流程](references/news-evidence-and-grades.md)。

### 4. 生成报告

`research` 冻结知识截止和数据版本，输出：

1. 宏观背景：截止时已公开的读数；缺少公开时点的观测不计入，缺口写原因。
2. 新闻与事件：综合判断、逐事件贡献表、工作流覆盖率、每条已核实事件的依据与来源、待核实清单。
3. 综合摘要与限制。

报告的 Markdown/JSON 保存到 `runtime/reports/`，可用 `reproduce` 原样重放。结构见[报告结构](references/publication-and-report.md)。

## 数据导入约定

### news.json

必需：`title`、`published_at`、`source_id`。建议：`security_ids`、`body`、`available_at`、`source_kind`、`evidence_scope`、`url`、`evidence_locator`。只有标题或摘要的记录不会被升级为方向结论。

### macro.csv

必需：`series_id`、`observation_date`、`value`。`available_at` 是该数值的公开时点；缺少它的观测不会进入任何报告的宏观背景，因为无法证明当时可知。默认序列：`DGS10`、`DGS2`、`CPIAUCSL`、`DEXUSEU`、`DFEDTARU`，可在 `policies/defaults.json` 的 `macro_series_map` 调整。

### securities.csv

必需：`ticker`、`market`、`valid_from`。缺主表时使用默认证券身份并在报告中说明。

模板见 `examples/`。

## 参考文件按需加载

| 场景 | 文件 |
|---|---|
| 安装或更新 Skill，部署本地应用 | `references/install-prompt.md` |
| 来源、市场与禁止用途 | `references/scope-and-source-policy.md` |
| 新闻证据、去重、七档、强度与衰减 | `references/news-evidence-and-grades.md` |
| 本地 Search/Fetch 部署、并发与接口 | `references/local-search-fetch.md` |
| TinyFish 插件批量采集驱动 | `references/tinyfish-plugin-batch.md` |
| 报告结构 | `references/publication-and-report.md` |
| 时间口径、存储、复现与安全 | `references/operations-and-reproducibility.md` |

## 输出规则

- 新闻等级使用：极大利好、高度利好、中度利好、中性、中度利空、高度利空、极大利空；证据不足独立。
- 综合判断使用：利多、利空、中性/多空抵消；没有已核实评估时写明尚无，不写中性。
- 每条结论附来源链接、发布时间和证据范围。
- 不输出价格预测、上涨概率、买卖指令、目标价、仓位或图表。
- 宏观读数只作背景描述，不推导个股方向。

## 目录

```text
仓库根目录或安装后的 stock-research/
  SKILL.md
  references/
  policies/                  # 默认参数、来源政策
  examples/                  # 请求和导入模板
  scripts/research.py        # CLI
  scripts/skill.py           # 调用包内 .venv 的入口
  scripts/install_skill.py   # 一条命令安装与自检
  scripts/stock_research/    # 实现包
    acquisition/             # Finnhub/FRED 适配器、Search/Fetch 采集器、原文阅读
    context/                 # 新闻分级与去重、影响综合、宏观背景
    control/                 # 导入、逐标题工作流、审核、报告编排
    reporting/               # 报告渲染
    storage/                 # 压缩 JSON 版本库、SQLite 索引、原始载荷
  tests/
```

运行数据留在 `--root` 指定目录，不进入安装包。测试阶段保留源标题、查询、证据元数据和版本，不自动清理；去重只影响贡献计算，不删除源标题。
