---
name: stock-research
description: Evidence-first US/HK stock research with bundled Kronos-small and native multifactor analysis. Use for daily factor/fundamental/risk research, hourly OHLCV prediction, historical masked research, validation and validated probabilities from authorized data. No trading execution or position sizing.
---

# Stock Research Skill

统一的美股/港股量化、多因子与新闻研究 Skill。量化结论、新闻结论和综合摘要进入同一条消息。

## 完整安装与模型

本包包含固定版本的Kronos模型与微调源码（含LICENSE）、small/tokenizer固定权重清单、skill分析代码，以及独立Python环境安装器。GitHub源码不上传大体积权重，安装时按固定官方revision下载并校验SHA256；便携包可包含已核验权重。旧横向模型比较、全部本地生成数据与报告、预测图片和Git本地配置不随发布或安装；本地数据保留。模型由本包代码直接调用，不依赖MCP、在线模型服务或外部试验目录。

多因子源码同样位于包内：日期截面处理、条件行业/市值中性化、家族等权、Ridge、LightGBM 4.7.0、因子诊断、封存历史实验与FF3＋Momentum研究风险模型。安装器安装固定本地依赖，同时实际执行Kronos推理及LightGBM训练/因子处理自检。

用户只需向agent提出“安装这个stock-research skill”。需要Python 3.12以上（完整固定依赖已在Windows x64/Python 3.13.7/CPU环境实测）。从解压后的skill目录执行一条命令：

```powershell
python scripts/install_skill.py --destination "$env:USERPROFILE\.codex\skills\stock-research"
```

安装器复制完整包、核验固定权重、创建目标目录内的`.venv`、安装CPU依赖并实际调用模型自检。目标已有skill时用显式`--update`；不覆盖其他目录，不改全局Python。已有权重可离线复制，缺权重只从清单固定的官方revision取得；首次依赖安装需要联网。其他平台同样传入自己的skill目标路径。

安装后所有分析使用`python scripts/skill.py ...`，它调用本skill内的`.venv`。若仅开发源码且已有相容环境，可直接运行`research.py`。来源账户凭证和本机抓取的运行/训练行情不随模型包分发；固定官方源树中的上游示例文件保留原样。

## 使用条件

使用本 Skill 当用户要求：

- 对美股或港股证券做当前研究、历史因子研究或概率研究；
- 导入用户已有或已授权的价格、公司行动、财务、宏观或新闻数据；
- 构建 point-in-time 数据集、训练候选模型、检查校准/样本外证据；
- 复现既有研究报告。

不使用本 Skill 做交易执行、仓位配置、图表生成、港股 Yahoo 数据抓取或 HKEXnews 自动采集。

## 核心原则

1. **先证据，后计算。** 每个数值必须能追溯到数据版本、时点口径和算法版本。
2. **三层产物分开。** 描述统计、历史研究、已验证预测不得互相冒充。
3. **概率只能来自通过登记验证的模型。** 没有验证证据时输出“不发布”，不填 50%。
4. **原始价格 + 独立行动台账。** 不用厂商复权序列作为训练真值。
5. **能力局部降级。** 缺财务只关闭财务历史能力，不删除价格描述、风险或事件。
6. **新闻默认是上下文。** 未进入该模型版本的新闻不调整已计算概率；七档结论不换算成上涨概率。
7. **市场独立。** 美股与港股分别建日历、数据集、模型、校准器和发布资格；港股缺口不切换到美股模型或 Yahoo。
8. **固定Kronos基准。** 美股当前研究默认执行Kronos-small下一交易日量价推理，小时研究通过独立UTC入口执行下一完整交易小时。基准方向及16条路径票数单列；未通过验证时不写成校准概率，也不替代1/5/20/60日和跑赢基准的发布检查。
9. **多因子研究默认执行。** 原始因子、自身历史分位、截面分数、风险暴露与预测概率分别标明。先确认选定运行目录确有库存；用`doctor --root ...`检查。少量证券不构造全市场排名，ETF仅作基准。历史公开时点未核验的财务/宏观/事件保留描述或背景并记录缺口。

## 命令

在仓库根目录或安装后的 skill 目录运行；也可用 `PYTHONPATH=scripts python scripts/research.py ...`。

```bash
# 当前研究（缺已验证概率模型时仍输出描述、风险、新闻和缺口）
python scripts/skill.py research --market US --ticker AAPL --benchmark SPY --horizons 1,5,20,60

# 多因子自动随当前报告执行；显式传入已登记数据根和同业股票池
python scripts/skill.py research --root MY-DATA --market US --ticker NVDA --benchmark SPY --universe NVDA,AAPL,MSFT,AMZN,GOOGL,META,TSLA

# 已授权本地日因子文件进入独立风险归因，原始文件不随模型包分发
python scripts/skill.py import-factors --root MY-DATA --ff3 FF3_daily.zip --momentum Momentum_daily.zip

# 本包原生模型：日线或小时OHLCV文件；不读取目标小时/日真实行情
python scripts/skill.py quant-baseline --file NVDA-hourly.csv --ticker NVDA --frequency 1h --cutoff 2026-11-05T20:00:00Z
python scripts/skill.py quant-baseline --file NVDA-daily.csv --ticker NVDA --frequency 1d --cutoff 2026-11-05T21:00:00Z

# 只导入用户授权文件（CSV/JSON），不联网
python scripts/skill.py import --kind prices --file prices.csv --market US --ticker AAPL
python scripts/skill.py import --kind actions --file actions.csv --market US
python scripts/skill.py import --kind news --file news.json --market HK

# 构建 point-in-time 数据集（先登记协议）
python scripts/skill.py build-dataset --market US --tickers AAPL,MSFT --benchmark SPY \
  --start 2023-01-03 --end 2025-12-31 --factors mom_60,rv_20,ep_ttm --protocol protocol.json

# 训练候选模型；只有证据 verified 且显式 --activate 才进入默认报告
python scripts/skill.py study --market US --horizon 5 --target absolute \
  --factors mom_60,rv_20,ep_ttm --protocol protocol.json --activate

# 查看数据/模型注册状态（不打印凭证）
python scripts/skill.py doctor

# Agent搜索原文后导入带引用、事实、判断理由和核实时间的新闻分析
python scripts/skill.py import-news-reviews --root MY-DATA --file news-reviews.json

# 逐标题持久队列；宿主agent检索、阅读和逐项判断后循环apply
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step prepare
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step next --limit 8
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step apply --file assessments.json
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step finalize

# 数百个独立预测日期；选择、校准、封存测试依时间分开，可恢复
python scripts/skill.py kronos-study --root MY-DATA --ticker NVDA --samples 1000 --dev-samples 200 --cal-samples 500 --study-id STUDY-ID
# 默认校准关闭；platt-diagnostic仅作单列诊断，主方向仍取原始票数
python scripts/skill.py kronos-study --root MY-DATA --ticker NVDA --study-id NEW-STUDY-ID --reuse-study-id STUDY-ID --calibration-mode off
python scripts/skill.py kronos-bank --root MY-DATA --study-id NEW-STUDY-ID

# 原始票数/校准后方向退化诊断；不重拟合、不改封存预测
python scripts/skill.py kronos-diagnostics --root MY-DATA --study-id STUDY-ID --out direction-audit.md

# 一次命令自动遍历原标题、Search/Fetch并输出分析前证据Markdown
# TinyFish API鉴权读取本地TINYFISH_API_KEY；不把凭证写入包或命令参数
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id COLLECTION-ID --provider tinyfish

# 已连接TinyFish插件：导出标题计划，宿主执行一次固定批量驱动，再导入其联网证据
python scripts/skill.py news-plugin-plan --root MY-DATA --workflow-id NEWS-ID --batch-id BATCH-ID
# 执行references/tinyfish-plugin-batch.md的程序驱动；不需要另配API Key
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id COLLECTION-ID --provider tinyfish-plugin --plugin-batch-id BATCH-ID

# 复现既有运行
python scripts/skill.py reproduce --run-id run-...
```

`--root` 指定运行数据目录；默认使用当前 skill 目录内的 `runtime/`，也可通过 `STOCK_RESEARCH_RUNTIME` 指定已有数据目录。

## 运行流程

### 当前研究

1. 读取请求，解析证券身份与市场。
2. 读取 `securities`、`prices`、`actions`、`action_coverage`、`financial_facts`、`macro`、`news` 的最新已登记版本。
3. 冻结截止时刻和数据快照；不使用截止后才到达的记录。
4. 计算特征、历史路径风险和当前上下文。
5. 读取该市场/期限/目标的活动模型；逐目标检查数据、证据与校准。
6. 按固定结构输出；缺失值写原因，不补中性值。
7. 保存运行清单和 Markdown/JSON 到 `runtime/reports/`。

当前报告同时读取同目录已登记候选模型：已计算但验证未通过的候选以研究分数/收益估算单列，不误称未执行，不授予活动预测资格。1/5/20/60日需要分别训练和验证；当前报告不临时重训。

新闻不能停在标题分类。当前研究有新Finnhub标题时，用`news-workflow`建立队列。已连接TinyFish时优先`news-plugin-plan`及[固定批量驱动](references/tinyfish-plugin-batch.md)，用插件已有鉴权逐标题Search/Fetch，不再索要独立API Key；完成后`news-collect --provider tinyfish-plugin`导入并输出分析前Markdown。只有独立Python/API方式才使用`TINYFISH_API_KEY`。遍历、失败回退和落盘由程序完成，不让大语言模型逐条决定或发起采集调用；大语言模型在证据表完成后分批分析。`--provider archive`只重放旧证据，不能说成联网采集。分析后重复`next/apply`直到所有标题进入终态，再`finalize`生成报告；内容或日期缺口追加确定性采集任务。每条保留事实与条件、方向符号、强度及理由、原始信息时间、实际核实时间与事件聚类。无法核实和无关联分别记录，不算中性。执行格式及衰减见[新闻证据流程](references/news-evidence-and-grades.md)。

Kronos同时保存原始解码和受约束派生K线：最高价至少为开收盘较大者，最低价不高于较小者；不交换开盘/收盘，不改变方向票数。结构约束解决无效K线，价格预测准确性仍须独立验证。

Kronos基准默认取当前已完成日线截止，输入全部此前有效OHLCV；小时CSV必须保留`bar_start_utc/bar_end_utc`，不用会丢失小时精度的日线导入契约。过滤未结束、截止后才可得、非交易时段和零量质量缺口，预测前保存输入，预测后封存输出。16条路径不因方向错或OHLC排序异常而事后删除；异常路径不用于价格极值或风险推断。超过512根公开训练上下文参考时保留全部历史并标记压力测试。宏观、新闻与财务仍由相应研究模块解释，未被编码训练时不声称Kronos已经使用它们。

原始冻结基准永不被候选训练覆盖；之后训练版本独立保存。历史遮蔽、多股票训练和数据量学习曲线按[训练方案](references/kronos-training-plan.md)实施。基准推理与多因子研究已接入；Kronos神经微调仍按方案另行实施，不把增加上下文误称神经训练。

多因子默认同时给出因子画像与独立风险解释，细节按[多因子工作流](references/multifactor-workflow.md)。有历史同行业股本控制且截面足够时做标准化/中性化；默认20家为可配置工程门槛，实际还检查行业组、矩阵秩和残差自由度。七姐妹先用于限定股票池研究。Kronos信号加入合成模型前须有封存的逐折历史输出；当前路径票数不直接混入综合分数。

### 历史研究

1. 先登记 `ExperimentProtocol`（训练/开发/校准/测试块、候选预算、基线、验收政策）。
2. 构建带 `label_status`、`matured_at`、`partition` 的数据集。
3. 训练段拟合变换和模型；开发段选有限超参数；测试段只评估。`study`的方向概率在校准段单独拟合Platt；`multifactor-study`的收益评分在校准段只做诊断，当前不拟合概率校准器。
4. 保存所有候选及失败记录，不覆盖历史样本外预测。
5. 只有 `ValidationEvidence.status = verified` 才能激活模型。

`kronos-study`比较预登记的全历史、512根原始价格和512根仅截至当时拆股换算上下文；开发段选择，诊断段检查原始分数可靠性，测试封存后揭示标签。默认`calibration-mode off`，不拟合或变换原始票数；`platt-diagnostic`仅作研究对照，始终不替代主方向。旧校准器与失败结果保留。输入K线数、路径数、验证日期数分别报告。历史覆盖起点不是供应商首次发布时点，缺档案仍可研究并标边界。详见[研究与验证](references/research-and-validation.md)。

优化效率先读[Kronos优化方案](references/kronos-optimization.md)。`kronos-bank`把已封存预测导出为无量纲特征，不调用神经推理；它不等于隐藏表征缓存或已训练方向头。研究元模型或新权重时可复用同输入/权重/生成配置的预测，不能把旧留出重新宣称盲测。优先多股票共同时间训练与股票留出，不以单股调参制造普适优势。

方向验证必须同时展示预测上涨占比、混淆矩阵、上涨/非上涨识别率、平衡准确率及AUC；原始票数与校准输出分别列示。单一预测类别、某类识别率为零或分数近常数会阻止方向资格。仅提高总准确率或反转AUC不能证明校准成功。融合量价、财务、宏观、新闻时先读[融合候选设计](references/multimodal-fusion.md)：未训练的融合不能写成已实现，新闻研究分数不能直接与未验证概率按主观比例混合。

测试阶段保留原始载荷、数据版本、输入、预测、校准器和报告，不自动清理或删除。安装包与运行数据分离；数据留在`--root`指定目录，SQLite管理索引，表格/工件单独存储。去重只影响新闻贡献计算，不删除源标题。

## 数据导入约定

### prices.csv

必需：`date`、`close`，以及 `security_id` 或 `ticker`。可选：`open/high/low/volume/amount/currency/available_at`。同一证券同一日期不得重复。

### actions.csv

必需：`security_id`、`ex_date`、`action_type`。可选：`q`（拆股换股数）、`cash_old`（每旧股现金权益）、`announced_at`、`available_at`、`accession`、`confirmed`、`supported`。复杂行动（rights/spin_off/merger）标为 `supported=false`，相关收益明确缺失。

### financial_facts.csv

必需：`security_id`、`concept`、`value`、`period_end`、`available_at`。`period_start` 为空表示时点余额；有值表示累计期间。缺少 `available_at` 的文件拒绝导入，因为它无法支持历史可知查询。

### news.json

建议字段：`security_ids`、`title`、`body`、`published_at`、`available_at`、`source_id`、`source_kind`、`evidence_scope`、`url`、`evidence_locator`。只有标题或摘要的记录不会被升级为强结论。

### action_coverage.csv

必需：`security_id`、`start`、`end`、`complete`。没有覆盖记录不等于“没有公司行动”，而表示覆盖未验证。

## 参考文件按需加载

| 场景 | 文件 |
|---|---|
| 来源、市场与禁止用途 | `references/scope-and-source-policy.md` |
| 时间、复权、公司行动与标签 | `references/time-return-and-action-policy.md` |
| 契约、数据准入与能力状态 | `references/contracts-and-capabilities.md` |
| 因子定义与分组 | `references/factor-catalog.md` |
| 多因子画像、风险、非线性合成与渠道缺口 | `references/multifactor-workflow.md` |
| 数据集、分区、验证与发布 | `references/research-and-validation.md` |
| 新闻证据、去重与七档 | `references/news-evidence-and-grades.md` |
| 最终消息结构 | `references/publication-and-report.md` |
| 存储、缓存、复现与安全 | `references/operations-and-reproducibility.md` |

## 输出规则

- 表中概率数值只在 `verified` 时显示。已计算候选标“待验证”并解释失败项，研究分数/收益估算另列；缺模型与缺数据分别说明，不混称未执行。
- 基准率单独说明“不是预测”。
- 新闻等级使用：极大利好、高度利好、中度利好、中性、中度利空、高度利空、极大利空；证据不足独立。
- 不输出买卖指令、目标价、仓位或图表。
- 综合摘要不得覆盖“不发布”决定，不得追加主观概率。

## 目录

```text
仓库根目录或安装后的 stock-research/
  SKILL.md
  references/
  policies/                  # 默认参数、来源政策、日历策略
  examples/                  # 请求、协议和导入模板
  scripts/research.py        # CLI
  scripts/skill.py           # 调用包内.venv的入口
  scripts/install_skill.py   # 一条命令完整安装与模型自检
  vendor/kronos/            # 固定官方完整模型/微调源码与LICENSE
  model-assets/kronos-small/ # 冻结small、tokenizer与相对哈希清单
  scripts/stock_research/    # 实现包
    multifactor/            # 包内原生处理、研究模型、诊断、风险与工件加载
  tests/
```

完整运行规则按上述 `references/` 按需加载。规划、调研及数据渠道审计保留在本地 `docs/`，不提交到 GitHub，也不进入安装包；执行分析不依赖该目录。
