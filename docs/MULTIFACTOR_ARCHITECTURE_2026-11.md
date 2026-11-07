# 多因子架构调研与本次实现 · 2026-11-07

本次选择 **时点数据底座 → 有限因子库 → 受控合成模型 → 独立风险解释 → 封存历史检验 → 统一报告** 的架构。多因子实现与Kronos-small源码一起位于skill包内，由本地Python执行；当前报告已接入多因子画像，历史研究入口支持家族等权、Ridge和LightGBM。Qlib、MCP和在线模型服务不参与实际运行。

代码已具备研究流水线，不意味着已取得经过验证的预测优势。当前最完整登记库只有NVDA一家公司和SPY基准；本次NVDA描述能够计算33项候选因子中的29项，尚不能形成有效的公司截面排名、行业/市值中性化或已校准多因子上涨概率。其余4项宏观因子的缺口包含可靠公开时刻/历史版本不足、库存没有汇率序列，以及`FX/RATE`占位映射尚未配置；原始宏观载荷仍可作为带版本的背景。报告逐项显示所需系列与具体缺口，不将映射问题误称为没有来源。

本文件区分调研结果、实际实现和后续扩展。运行细节以[原生多因子工作流](../stock-data/references/multifactor-workflow.md)及源码为准；库存和渠道证据见[独立渠道复核](MULTIFACTOR_DATA_CHANNEL_AUDIT_2026-11.md)与[原始数据缺口报告](MULTIFACTOR_DATA_GAPS_2026-11.md)。原[量化设计](QUANT_ANALYSIS_DESIGN_2026-11.md)与[整体架构](QUANT_SKILL_ARCHITECTURE_2026-11.md)仍是建设规格，不能将其中所有设想算作已实现能力。

## 1. 先进体系如何运作

先进多因子体系通常同时维护三个对象：解释风险的公共因子及暴露、用于预测的股票特征、将特征合成信号的统计模型。它们的用途与验收标准不同。Fama–French因子收益和Barra式风险暴露不是现成的个股涨跌预测权重；LightGBM等算法也需要在目标股票池、期限和数据口径上训练与验证。

截至本次调研，主要可借鉴路线如下。较新的论文列为研发候选，论文中的结果不代表本项目复现结果。

| 路线与来源 | 运作方式 | 本次采用的位置 | 状态与适用边界 |
|---|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib)，当前官方研究平台 | 数据处理、模型训练、信号诊断、风险分析和回测按工件连接；比较线性、树和深度模型 | 采用模块分层、有限候选比较和实验工件管理的设计 | 本项目为原生实现，没有安装或复制整个Qlib；[官方模型基准](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)主要使用中国股票市场，不能转写为美股效果 |
| [MSCI当前权益因子模型](https://www.msci.com/data-and-analytics/factor-investing/equity-factor-models) | 用风格、行业和宏观等暴露解释风险，监测集中、拥挤与环境变化 | 风险解释独立于收益预测，保留覆盖与数据末日 | 当前只实现FF3＋Momentum研究归因，未获得商业Barra模型、完整行业风险或拥挤数据 |
| [Gu–Kelly–Xiu，2020](https://dachxiu.chicagobooth.edu/download/ML.pdf) | 比较正则化、树和神经网络，检验非线性特征交互的预测价值 | 透明Ridge基准与LightGBM候选；按日期等权拟合、比较控制模型 | 采用算法与比较方法，未移植论文股票池、训练权重或收益结果 |
| [IPCA，2019](https://doi.org/10.1016/j.jfineco.2019.05.001)及[条件自编码器，2021](https://doi.org/10.1016/j.jeconom.2020.07.009) | 股票特征决定变化的风险载荷，进一步学习非线性条件因子结构 | 作为更广且合格面板后的候选路线 | 尚未实现；七姐妹的狭窄截面不足以支持完整市场潜因子结论 |
| [AlphaForge，AAAI 2025](https://arxiv.org/abs/2406.18394) | 生成公式因子，再依据过去表现选择并动态组合 | 采用因子目录、相关性检查、分家族组合和增量消融原则 | 未实现自动公式生成网络或动态权重模型；当前报告不按近期行情临时改权重 |
| [RD-Agent-Quant，NeurIPS 2025](https://arxiv.org/abs/2505.15155) | 假说、代码实现、实验执行与反馈迭代，联合优化因子和模型 | 采用离线登记、保留全部尝试、封存评估的研发闭环 | 未接入RD-Agent自动研发；新增候选仍需控制尝试预算和封存测试使用 |
| [不确定性调整排序，2026-01-02预印本](https://arxiv.org/abs/2601.00593) | 将个股预测不确定区间用于排序，而非仅依赖点预测 | 列为后续不确定性层的比较候选 | 尚未实现该排序算法；当前日期块区间是评估误差诊断，不能冒充每股未来收益区间 |
| [残差深层增强，2026-10-07预印本](https://arxiv.org/abs/2610.09613) | 保留浅层基准，用残差网络学习增补部分 | 支持先冻结强基准、再检验模型增强的研发顺序 | 尚未实现或在项目验证；不能推出模型越大或因子越多就越准确 |

这次引入的是有出处、能审计且可继续扩展的研究架构。现阶段优先解决可知时间、有效股票面板和公平评价，再比较更复杂的模型。用户提出的60%方向准确率保留为验收目标；任何上述架构都不能保证达到。

## 2. 本地实现的层次与连接

| 层 | 已实现职责 | 包内主要位置 | 当前边界 |
|---|---|---|---|
| 数据与时点 | 按登记版本读取价格、行动、财务、宏观、新闻、证券元数据和因子收益；冻结报告输入 | `storage/`、`control/orchestration.py`、`features/metadata.py` | 完整股票池、长期行动、历史行业与全部公开时刻仍需补齐；未将当前快照升级为严格历史PIT证据 |
| 原始因子 | 14项价格/风险/流动性、15项财务/规模、4项宏观；保留原值、单位、依赖与缺失原因 | `features/registry.py`、`price.py`、`financial.py`、`macro.py` | RSI等4项显示指标另列，不作为33项候选的重复投票；财务同比须匹配同一财年期间 |
| 当前画像 | 个股因子表、过去价格因子分位、经营公司截面状态、宏观版本背景 | `control/multifactor.py` | 过去分位仅为描述位置，既非上涨概率，也非全市场排名 |
| 截面处理 | 日期内MAD处理与缩放；在PIT行业/股本控制充分时中性化；分别保留原始与残差分数 | `multifactor/preprocessing.py` | 默认至少20家公司；还检查每因子覆盖、行业组、设计矩阵秩和残差自由度 |
| 信号合成 | 预登记方向的家族等权控制、日期等权Ridge、CPU LightGBM；训练内拟合变换、相关簇与权重 | `multifactor/catalog.py`、`study.py`、`diagnostics.py` | 模型源码可用；当前报告不会自动训练，也没有现成、已验证的多因子预测权重 |
| 独立风险 | 解析本地公共因子档案、估计历史暴露和特异风险、构造风险协方差 | `multifactor/factor_returns.py`、`risk.py` | 研究归因实现；无完整行业/拥挤层，特异协方差采用对角矩阵 |
| 历史检验 | 共同时间块、实际标签成熟检查、有限选参、家族消融、封存预测、独立测试评估 | `multifactor/study.py`、`datasets/` | 当前多因子校准块只做诊断，不拟合概率校准器，不激活概率注册表 |
| 报告与复现 | 当前研究默认附多因子画像、独立风险、库存缺口与版本哈希 | `reporting/report.py`、`scripts/research.py` | 无数据或失败仅限制相应能力，不删除可用价格、财务或事件描述 |

实现沿用项目现有的Parquet数据和SQLite登记。原规划中的DuckDB查询层尚未在本次迁移；本文不将规划中的存储选择称为已实现依赖。

### 当前29/33项意味着什么

29项可计算表示NVDA在本次输入版本下具有价格和财务/规模的描述数值，不能说明这些因子已经完成预测检验。市值使用已披露时点股数重建，仍需股本、拆股和可知时间的独立核验；它不等于完整自由流通市值数据库。

当前经营公司截面是1家。SPY用于市场比较，因ETF身份被排除在公司因子截面之外。因此当前正确产物是NVDA的因子画像、已披露报表描述和自身历史位置。家族等权综合分数、中性化和同业名次保持未形成。

自身历史分位使用截止前最多126个交易日的价格因子观测，排除当前观察，至少20个有效观测才展示；相同数值按半个平局计数。财务历史分位尚未建立。这个百分比不能与预测准确率或上涨概率混读。

本文件不引用旧财务快照中的增长率数值。季度、TTM和同比需按财年对应期重建，旧输出应以修复后重新运行的报告为准。

## 3. 风险模型与alpha分开

**收益信号层**研究未来约定区间的绝对收益或相对SPY的收益，输出经过历史拟合的研究分数。家族等权首先在族内平均已有因子，再对预登记家族等权；默认要求所有声明家族有覆盖，并展示逐股因子/家族缺失，不将缺少财务的技术子集重新包装为完整多因子结果。经济方向仍是假说，必须经过历史检验。

**风险层**估计已实现收益对公共因子的暴露：

```text
r_i − RF = a_i + B_i f + e_i
Σ_daily = B F_daily Bᵀ + D_daily
Σ_annualized = 252 × Σ_daily
```

当前公共因子是市场MKT、规模SMB、价值HML和动量MOM。默认最多252日共同因子窗口、至少120日有效重叠，将因子协方差按10%收缩到其对角线，再加特异方差对角矩阵。窗口和收缩比例均为工程参数，不能作为预测有效性的证明。回归截距是历史描述，不是发布的预期alpha。

已保存Ken French档案截至2026-09-30；无论当前报告截止多新，风险输出都须标明实际公共因子末日。`import-factors`只导入已授权本地CSV/ZIP，识别百分数单位与缺失码，保存原始档案哈希。档案不随模型安装包分发，也不自动联网更新。

未知逐期公开时刻的最新版本因子收益，只用于事后风险归因。`strict_pit=True`拒绝未知`available_at`，未来待预测区间的已经实现因子收益禁止成为预测输入。美股公共因子不会自动用于港股风险模型。

行业/市值中性化须服务于明确目标。规模因子自身保留规模方向，仅对行业做相应处理；不能机械消掉准备研究的规模效应。当前实现要求至少两个行业、每行业至少3家公司、设计矩阵满秩以及至少10个残差自由度。样本数量门槛不是“统计上必然充分”的保证，七姐妹也不足以代表完整市场的行业与规模溢价。

## 4. 历史遮蔽、验证与Kronos增强

日频主标签沿用原规划：截止日收盘后产生特征，从下一交易日开盘开始，以第H个交易日收盘结束，按行动台账重建区间收益；超额目标使用同市场、同币种、同收益口径的基准。完整当日日线不能用于模拟当日开盘已经发出的预测。相关边界也得到[2026年决策时点泄漏诊断研究](https://arxiv.org/abs/2605.23959)的支持，但项目验收仍以自己的输入、标签与测试为准。

现已实现的多因子实验顺序是：

1. 登记所有股票共同的训练、开发、校准、测试日期块及有限候选预算。核对证券日期唯一、市场/期限/目标一致、标签成熟，以及特征可得时刻。
2. 按真实标签终点与成熟时刻剔除跨入后续块的样本，保留计划分母和排除原因。仅训练段拟合相关簇、代表因子、缺失处理、缩放和模型权重。
3. 在开发段选择家族等权、Ridge或LightGBM的有限参数；保留成功、失败和缺依赖尝试。默认选择日期等权MSE，只有足够公司截面时才使用Rank IC。
4. 家族消融只用开发段。选定参数后用训练＋开发重拟合；校准段用于独立诊断，不参与选参，也不为这套研究分数拟合Platt等概率校准器。
5. 保存JSON及原生LightGBM文本模型，先封存不含测试真实结果的预测文件和哈希，再保存真实标签和评估。模型可用`load_candidate`本地重载，无需pickle或服务。
6. 分开报告方向准确率、计划/可用分母、覆盖、日期等权误差、Rank IC/ICIR及日期块不确定区间。排序篮子差价、换手和明示假设成本只作研究诊断；重叠多日篮子不能累计为已验证可执行策略收益。

本研究目录的`.holdouts`拒绝同一市场/目标/期限/日期/证券测试范围的重复选择和覆盖，允许读取既有封存工件。它不能证明其他目录中从未看过同一历史；此前反复观察的NVDA样本仍属于开发证据。移动文件或更换目录不会产生新的盲测。

### Kronos在其中的位置

Kronos-small继续保留为冻结的量价基准。多因子模型不默认将16条生成路径票数当作校准方向概率，也不把当次Kronos预测临时加到基本面分数里。

已实现的`kronos_*`特征准入检查要求封存、逐折、冻结模型的证据清单，并要求逐行输入截止、封存时刻和模型训练截止不晚于对应特征截止。当前报告没有自动生成完整历史Kronos特征面板。今天新生成的历史遮蔽回放，不能伪造为当年已经封存的预测；后续回放适配须明确真实生成时间与模拟预测截止，并继续保证权重和输入未使用目标标签。

后续应在同一分区和数据版本上比较传统因子模型、传统因子＋Kronos，以及移除Kronos后的结果。只有在相同留出范围上提供稳定增量，才保留增强；高度相关或无增益时保留其独立基准角色。Kronos微调、不同历史训练数据量的学习曲线和跨股票留出另按[训练方案](../stock-data/references/kronos-training-plan.md)实施，本次不自动开始昂贵训练。

小时方向与日频基本面收益保持独立标签和评价。把一份财报复制到每小时，不会产生额外独立财务信息；宏观和新闻只有在具备历史版本、可知时刻并被登记为该模型输入后，才参与预测。

## 5. 信息渠道优先级

当前缺口既有尚未接成可用数据集的现有材料，也有缺少完整历史权益或渠道的材料。应先补已有来源的工程和覆盖，再决定新增来源。

| 优先级 | 信息 | 已有基础 | 下一步处理 | 渠道性质 |
|---|---|---|---|---|
| P0 | 七姐妹及更广公司的PIT财务 | SEC已有NVDA完整facts及申报材料 | 扩展公司；绑定accession与受理时刻、修订链；正确拆季度、同财年TTM和同比 | 主要是现有SEC工程与覆盖 |
| P0 | 长期原始价与公司行动 | 已有AAPL/NVDA长原始日线，Tiingo有短窗口行动载荷 | 核日历、缺口、拆股/派息/权益单位、长期行动及公开时点 | 优先扩展已选渠道，原始长日线不直接等同训练就绪 |
| P0 | 历史股票池、上市/退市及稳定身份 | 当前筛选、SEC身份入口与零散历史样本 | 保存有效期范围、代码变更和退市权益，避免使用今天名单回测全历史 | 完整历史证据尚缺，可能需授权档案或用户文件 |
| P0 | 历史股数、自由流通股、行业 | 已披露NVDA股数、当前证券主表和SIC入口 | 建公开时点股本及有效期行业；自由流通股与报告股数分开 | 混合工程和渠道缺口；不将GICS作为唯一选择 |
| P1 | 宏观vintage与精确发布时间 | FRED/ALFRED已有数值与指定vintage能力 | 逐批发布重建as-of视图，按市场时钟连接 | 主要是现有版本库和发布时间工程 |
| P1 | 一致预期与历史修正 | 现有Finnhub账户入口 | 核EPS/营收历史端点、额度、预测财年和修订时间 | 先核现有权益，不足再补授权历史；当前评级不替代修正史 |
| P1 | 新闻/公告历史正文与首次可得时间 | Finnhub、GDELT、RSS、SEC/IR当前入口 | 补范围许可、正文、实体去重、附件、首次时间与修订存档 | 当前背景可用；完整长期正文覆盖尚无证据 |
| P1 | 空头余额、借券成本与期权历史 | FINRA文件渠道及当前期权样本 | 区分短卖成交量与余额，按发布时间生效；核个股历史链与借券字段 | FINRA部分属工程；借券与完整期权链需要另核授权渠道 |
| P1 | 公共因子收益及风险暴露 | 已保存FF3和Momentum档案，本次有原生解析/归因模块 | 在正确运行目录登记、保存版本、标明实际数据末日 | 可先用透明研究风险层，无需先购买商业模型 |
| P2 | 持仓/资金流、报价/逐笔与成本 | SEC申报目录、供应商样本、少量分钟/报价资料 | 正文和披露滞后解析，报价单位、价差、冲击和容量核验 | 完整高频与真实交易成本历史尚缺；小时线不能替代 |

港股另有长期公司行动、PIT财报、退市股票池和公告时点缺口。保持既有港股来源政策：不采用Yahoo路径，不自动抓取HKEXnews，也不以美股模型代替港股证据。

细到文件路径、行数、列名、日期和哈希的结果见[机器可读渠道审计](../runtime/multifactor-integration/data-channel-audit.json)。该库存审计的“存在”只说明本地保存过材料；本次新模块接线和后续登记不会自动补齐其历史可知、行动或许可证据。

## 6. 运行入口与本次完成范围

从安装后的skill目录运行。首先选择真实有库存的运行根：主`runtime/catalog.sqlite`、`runtime/nvda-preview`和`runtime/nvda-full-run`不是同一个库。选错根会造成已抓取材料无法进入报告。

```powershell
python scripts/skill.py doctor --root MY-DATA
python scripts/skill.py research --root MY-DATA --market US --ticker NVDA --benchmark SPY
```

多因子默认随`research`执行；`--no-multifactor`显式跳过，`--universe`指定已登记的经营公司比较范围。指定七个ticker不会自动生成缺失的历史行情或财报。

```powershell
python scripts/skill.py import-factors --root MY-DATA --ff3 FF3_daily.zip --momentum Momentum_daily.zip
python scripts/skill.py import --root MY-DATA --kind security_metadata --file security-metadata.csv --market US
```

历史合成采用已发生数据，先构建与协议匹配的`features_labels`版本，再进入`multifactor-study`：

```powershell
python scripts/skill.py build-dataset --root MY-DATA --market US --tickers NVDA,AAPL,MSFT,AMZN,GOOGL,META,TSLA --benchmark SPY --start 2015-01-02 --end 2025-12-31 --horizons 20 --targets excess --factors mom_60,rv_20,ep_ttm,roa_ttm,size_log --protocol multifactor-protocol.json
python scripts/skill.py multifactor-study --root MY-DATA --market US --horizon 20 --target excess --factors mom_60,rv_20,ep_ttm,roa_ttm,size_log --protocol multifactor-protocol.json
```

日期仅为历史范围示例，不表示已取得全部股票数据。协议须携带实际PIT数据验收证据；证据不足时只能显式使用`--development-only`进行未验证历史研发。`--models`可指定`equal_weight,ridge,lightgbm`的有限子集。历史入口默认不要求中性化；显式`--neutralize`开启后，缺合格行业/股本控制会留下缺失，不自动降为同名已中性化模型。

本次完成原生架构、当前画像与风险接线、历史合成算法和工件/验证边界，并上报现有数据与渠道缺口。全股票历史库建设、宏观/新闻入模、自动Kronos历史特征生成、深度条件因子、动态因子挖掘与已校准多因子预测仍是后续工作。当前报告和历史研究均不得据此声称已经实现60%准确率或可执行投资收益。

## 7. 最终安装与真实运行验收

最终包版本为`0.3.0`。完整安装已在Windows x64、Python 3.13.7、CPU上实测，先创建独立`.venv`完成首次安装，再用正常`--update --offline`安装最终修复。最低Python版本为3.12；`--offline`限制权重下载，首次Python依赖安装仍需联网。Kronos模型与tokenizer的固定代码/权重、本地LightGBM和多因子引擎均随包安装，不依赖旧实验目录或MCP。已将依赖旧`deps/mini`目录的`rolling_baseline.py`留作仓库档案并排除安装/ZIP。

| 验收 | 最终结果 | 证据 |
|---|---|---|
| 源代码完整行为测试 | 166项通过，无失败/跳过 | [source XML](../runtime/multifactor-v1-integration/source-tests-v030-final.xml) |
| 独立安装目录完整测试 | 166项通过，无失败/跳过；使用目标私有Python | [installed XML](../runtime/multifactor-v1-integration/installed-tests-v030-final.xml) |
| 安装器实际模型自检 | 固定权重核验、真实Kronos推理、LightGBM 4.7.0实际拟合及24公司合成截面处理通过 | [最终更新回执](../runtime/multifactor-v1-integration/updated-install-receipt.json)；[初次完整安装](../runtime/multifactor-v1-integration/fresh-install-receipt.json) |
| 真实已保存NVDA数据完整报告 | 从无关工作目录执行安装后的launcher、HF离线；82项检查通过；报告逐字复现 | [真实报告](../runtime/multifactor-v1-integration/NVDA-Kronos-multifactor-installed.md)、[JSON](../runtime/multifactor-v1-integration/NVDA-Kronos-multifactor-installed.json)、[运行验收](../runtime/multifactor-v1-integration/installed-forward-validation.json) |
| 财务组件独立对账 | 67项通过，累计链、四季差额、币种、期间、公开时刻与债务scope核对 | [财务复核](../runtime/multifactor-v1-integration/independent-review/financial-cashflow-final-review.json) |
| 便携ZIP独立核验 | 219项文件的CRC、清单SHA/字节与source/installed逐文件一致；官方92文件完整；20项检查通过 | [安装包](../runtime/multifactor-v1-integration/stock-research-kronos-multifactor-v0.3.0.zip)、[独立发布复核](../runtime/multifactor-v1-integration/independent-review/release-final-package-review.json) |

ZIP为116,971,183字节，SHA256为`b5a149244629f7794b9c8d5a2af06e6d1a041fe1f0f091b87ae205972840b4c4`。包保留固定上游公开示例，不包含本机采集研究数据或账户凭证。数据来源和覆盖仍由安装者配置或导入。

真实报告的截止为2026-11-05 21:00 UTC，实际生成于2026-11-07；这是历史遮蔽工程验收，不能冒充当年已经封存的实时预测。它使用463日日线，Kronos生成16条下一日路径，其中11条上涨，并标记1条OHLC排序异常；方向票数未经概率校准，异常路径不作为价格极值/风险预测。多因子层给出29/33项数值，NVDA中期动量弱、波动高，已披露增长强；仅1家经营公司，综合截面评分保持未形成。FF3＋Momentum归因使用252日共同样本，实际因子末日为2026-09-30。

概率表里的“未执行”指没有已激活、通过验证的**概率模型**，不等于没有大语言模型，也不等于Kronos与多因子分析未运行。当前`baseline_forecasts.status=ok`、`multifactor_snapshot.status=descriptive`；下一步需要补齐历史面板并对Ridge/LightGBM等做封存样本外检验，而非把当次生成路径票数或因子历史分位改称上涨概率。上述验收证明代码、口径与安装复现功能，不是投资预测准确率测试；本次没有启动昂贵神经模型训练。
