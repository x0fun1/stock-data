**stock-data Quant 深度审计与改进计划**

审计日期：2026-10-11，Asia/Shanghai。基线：Git `5833220`，Skill 2.6.0，research runtime 0.3.0，snapshot schema 1.2。

**判断：工程完整性已经明显改善；真实市场预测有效性尚未建立；算法处于可审计的基础研究阶段。下一阶段应优先建立可信的评价闭环、解决样本预算和概率质量问题，再比较复杂模型。**

本次完成代码、研究规则、上一轮实施记录审查，运行全部 203 个现有测试，并补充三个 horizon 的合成快照复现和三个独立契约探针。203 个测试通过，耗时 27.492 秒；尾部 `FileExistsError` JSON 是现有防覆盖测试的预期输出。合成数据仅用于暴露结构问题，不能证明市场预测水平。本次没有修改生产算法、安装依赖或更新已安装 Skill。

证据文件：[audit_evidence.json](audit_evidence.json)。复现入口：[audit_probes.py](audit_probes.py)。仓库根目录执行：

```text
python audits/quant-2026-10-11/audit_probes.py
python -m unittest discover -s tests
```

**1. 有效性必须分成四件事判断**

| 要判断的能力 | 当前结论 | 证据和边界 |
|---|---|---|
| 可计算、可追踪、可交付 | 有较扎实的工程证据 | 冻结快照、数据 gate、路径身份、成熟标签、fold 内缩放、独立 News、digest 和交付校验已有实现与回归测试 |
| 有优于简单基准的方向预测能力 | 尚未证明 | 未提供足够的真实、独立、可配对市场 OOS；上一轮指标对照主要来自一个可预测的合成波形 |
| 报告的概率具有可靠校准 | 尚未证明 | C 的 Platt 拟合与最终 holdout 已隔离，但拟合完成不等于校准改善；ensemble 无独立校准；没有充分的分阶段校准不确定性与覆盖验证 |
| 扣除交易成本后能够盈利 | 未评估 | close-to-close label 不是成交回报；没有执行模型、完整交易规则、组合回测、有效试验收益矩阵 |

“没有证据证明有效”并不等于“已经证明无效”。当前资料支持第一行，后面三行需要独立研究。CLI success、203 tests passed、PASS/DEGRADED 和 `forecast_eligible` 都不能替代第二至第四行的证据。

上一轮已经修正 A 的成熟标签边界、B 的 OOS IC 推断、C 的最终 holdout 校准隔离，并把成本情景与预测置信度分离。这些是必要修订；[IMPLEMENTATION.md](../../IMPLEMENTATION.md) 本身也明确没有真实独立快照支持准确率改善。此次继续检查这些修订在默认配置下能否形成有效证据。

**2. 新发现与优先级**

此处 P0 表示应在继续宣传验证水平或启用新模型前处理；P1 表示预测质量研究的主线；P2 表示依赖前序数据与验证能力的扩展。

| ID | 优先级 | 发现 | 可核对的代码 |
|---|---|---|---|
| F01 | P0 | B 的三分桶最低样本与默认三年 5D/20D 预算不匹配 | [factor.py](../../stock-data/scripts/quant_research/researchers/factor.py#L201)、[market_data.py](../../stock-data/scripts/quant_research/market_data.py#L42) |
| F02 | P0 | 当前可用共识与 OOS 完整交集的路径集合不一致；弃权 B 阻断 A+C 评价 | [consensus.py](../../stock-data/scripts/quant_research/consensus.py#L175)、[probability_quality.py](../../stock-data/scripts/quant_research/validation/probability_quality.py#L100) |
| F03 | P0 | A 的 OOS 使用至少 20 个匹配，当前预测使用至少 30 个；验证了不同的资格规则 | [quant.py](../../stock-data/scripts/quant_research/researchers/quant.py#L68)、[quant.py](../../stock-data/scripts/quant_research/researchers/quant.py#L93) |
| F04 | P0 | OOS 记录校验不核查训练标签边界、准确的 H-session label、目标真值 | [probability_quality.py](../../stock-data/scripts/quant_research/validation/probability_quality.py#L20)、[audit.py](../../stock-data/scripts/quant_research/audit.py#L262) |
| F05 | P0 | 数值 confidence 不直接反映是否战胜基准；ensemble 未校准也没有相应独立扣分 | [consensus.py](../../stock-data/scripts/quant_research/consensus.py#L228)、[audit.py](../../stock-data/scripts/quant_research/audit.py#L351) |
| F06 | P1 | 5D 默认 OOS 的 IC 推断常态达不到循环移位检验最小样本，20D 更弱 | [factor.py](../../stock-data/scripts/quant_research/researchers/factor.py#L129)、[math_utils.py](../../stock-data/scripts/quant_research/math_utils.py#L148) |
| F07 | P1 | A 无收缩或漂移处理，B 概率映射离散，C 校准器向重拟合模型迁移未经验证 | [quant.py](../../stock-data/scripts/quant_research/researchers/quant.py#L55)、[factor.py](../../stock-data/scripts/quant_research/researchers/factor.py#L270)、[ml.py](../../stock-data/scripts/quant_research/researchers/ml.py#L221) |
| F08 | P1 | 单次 chronological holdout 缺乏跨版本、跨运行的使用治理 | [ml.py](../../stock-data/scripts/quant_research/researchers/ml.py#L307)、[orchestrator.py](../../stock-data/scripts/quant_research/orchestrator.py#L38) |
| F09 | P1 | 证据家族的 Jaccard 不能衡量模型误差独立性，ML 家族标注过粗 | [consensus.py](../../stock-data/scripts/quant_research/consensus.py#L216)、[ml.py](../../stock-data/scripts/quant_research/researchers/ml.py#L328) |
| F10 | P1 | 同一价格序列的原始动量容易捕捉市场漂移，现有基准不足以说明增量信息 | [quant.py](../../stock-data/scripts/quant_research/researchers/quant.py#L87)、[factor.py](../../stock-data/scripts/quant_research/researchers/factor.py#L319) |
| F11 | P1/P2 | `p > 0.5` 就标 bullish，没有单独表达相对基准优势和估计不确定性 | [consensus.py](../../stock-data/scripts/quant_research/consensus.py#L302) |
| F12 | P2 | C 的预期收益近似忽略状态对涨跌幅的影响；简化成本情景不能评价执行 | [ml.py](../../stock-data/scripts/quant_research/researchers/ml.py#L325)、[consensus.py](../../stock-data/scripts/quant_research/consensus.py#L16) |

**F01：默认 horizon 存在结构性样本短缺。**

B 采用前 50% 选择因子、后续 30% 校准、最后 20% holdout。`non_overlapping()` 对闭区间采取 `next_start > previous_start + H`，因此完整日线上的评分节奏是 H+1 个 session。对于连续、没有大量分数并列的 composite，三分桶通常需要总计至少 60 个校准标签才能让每桶达到 20。

756 根合成日线的实际复现：

| horizon | B 校准标签 | low/mid/high 桶 | B 当前概率 | C 的 Platt 训练预测数 | 最终可用路径 | 最后 20% OOS 评分时点 |
|---|---:|---|---|---:|---|---:|
| 1D | 113 | 38 / 37 / 38 | 有 | 121 | A+B+C | 76 |
| 5D | 37 | 13 / 11 / 13 | 无 | 40 | A+C | 25 |
| 20D | 10 | 4 / 2 / 4 | 无 | 12，未校准 | C | 7 |

固定当前切分、非重叠规则、连续不同分数，三个桶都达到 20 所需最少日线约为：1D 399、5D 1199、20D 4199。按每年约 252 个 session 粗算，后两者约 4.8 年与 16.7 年。这是预算估算，不是建议自动扩窗，也不是说任何并列分数情形都绝无一个合格桶。分数集中、并列可以改变桶大小，但不能稳定解决默认配置的预算问题。

改进重点应是样本效率和连续、收缩的概率模型。把窗口改成十几年会引入更多制度和市场状态变化；把最低样本简单降成 5 也没有统计依据。

**F02：部署共识的评价与“三条路径都有预测”的诊断需要分开。**

5D 复现中当前 consensus 明确使用 A+C，但公共 OOS 构造把有 OOS 记录的 B 一并纳入，即使 B 的所有 probability 都是 null。三路径完整样本为 0，A+C 的完整公共样本实际为 25。

这不会直接改变当前等权概率，却让共同样本权重诊断、基准比较及依赖该诊断的共识审计无法说明实际 A+C 组合的质量。即使只保留 A+C，25 个样本仍不足以满足权重估计/评价各 20 的现行要求；修复集合问题不能伪造出可部署权重。

应分别报告全候选交集、每个固定子集和实际在线决策策略的回放。实际策略必须在每个历史决策日重算路径资格和可用权重；不能用今天的可用路径集合挑选过去最好看的 OOS 子集。

**F03：OOS 与运行资格不一致会高估覆盖。**

170 根单调上涨的合成日线、5D：当前 A 有 25 个匹配而拒绝输出概率，OOS 却输出 4 次概率，因为这些历史时点有 20、21、22、23 个匹配。20D 的 756 根复现也出现当前 A 不合格、历史 OOS 仍有数值预测。

同一个资格函数应被当前预测和历史 replay 共用。评价要记录“本来该预测但因样本不足而弃权”的分母；不能用更宽松历史规则验证更严格当前规则。

**F04：新增 OOS 字段目前不是独立证据验证器。**

独立探针向 OOS 质量函数提供一条 5D 记录：decision 和 label_end 同一天、training_label_end 在 decision 之后、target_definition 与 5D 不符。只要两条路径都提交同样字段，质量函数仍返回 available、公共样本 1。

这证明校验边界存在缺口，不证明当前内部 A/B/C 实际生成了这些坏记录，也不代表可绕过所有快照 gate。当前 E 主要审查布尔声明、身份和汇总指标；应补充以冻结行情、session 索引、训练/校准 receipt 重算的证据校验，坏记录不能进入质量评价、权重或置信度依据。

**F05：高 confidence 可以与基准失败同时出现。**

隔离评分探针固定数据质量 100、两条不同家族路径、概率 0.6、OOS 数 100。把两条路径的报告 Brier 从 0.20 改为 0.35、基准保持 0.25：confidence 仍为 high / 100；E 将 reporting 从 pass 改为 degraded。

这属于合同/评分探针，并非真实市场的 100 次预测。它说明数值 score 与基准优势没有直接联系。当前数值分数实际更接近“工程证据支持度”；把它当作预测准确性或概率可靠性的数值尺度会误导。A/B 的经验频率被标为 calibration_available，也不等于已经验证 ensemble 的可靠性。

建议保留现有兼容字段，明确 `confidence_score_kind=heuristic_evidence_support`，另增独立 `predictive_evidence_grade`、baseline skill、校准证据和不确定性。经验证 grade 的升级必须由真实外层 OOS/forward 证据触发。只有非致命证据不足时继续保留原概率并降级，保持现行 Skill 的预测/报告/策略资格分离。

**F06：IC 推断“已实现”与“默认窗口能产生证据”不同。**

5D 复现有 25 个 OOS 观察，block_length=5；允许的循环移位仅 16 个，小于代码要求的 19，因此 p 值和 BY 调整值都是 null。即使删除这一检查，最低可能原始 p 为 1/17≈0.0588，11 个因子都取得这个最低值时最乐观 BY q 仍约 0.1776。不能靠增加 bootstrap 重复次数改变这个移位检验的 p 值分辨率。

20D 只有 7 个 OOS 观察，block_length=20，bootstrap 与移位检验均不足。这里 block 是以“筛选后的观察”为单位，H 是原始 session 数，现实现直接混用两个尺度作为下限；应显式定义单位并在多个 block 规则下做敏感性与覆盖模拟。

BY 本身不能修复原始 p 检验的平稳性假设、低功效或未登记的跨 horizon/跨版本试验。现代码将推断标为 exploratory、平稳性和覆盖未验证是正确做法；下一步应补足证据，而不是删去这些限制。

**3. 算法先进性的评价**

| 模块 | 方法定位 | 值得保留 | 主要提升空间 |
|---|---|---|---|
| A | 固定 20-session 动量条件频率 baseline | 可解释、易复现、成熟标签约束 | 对市场上涨基率的增量信息、收缩、小样本不确定性、状态变化 |
| B | 固定 11 个 OHLCV 因子、相关性剪枝、三分桶经验频率 | 因子选择/校准/holdout 顺序、所有候选披露 | 样本预算、稳定选择、连续映射、跨状态验证 |
| C | 手写 L2 Logistic Regression + Platt | 因果 OOS、fold 内缩放、最终 holdout 不参与校准 | 优化器收敛、正则与窗口选择、校准迁移、非线性交互 |
| D | 单标的时间序列 IC 诊断 | 不冒称横截面组合回测 | 缺同日多资产 panel、PIT universe、组合与交易引擎 |
| 共识/E | 等权平均、家族 Jaccard、声明/指标审计 | 不让 News 改概率、策略审计与概率分开 | 在线 policy replay、误差相关性、受约束组合、ensemble 校准与审计证据重算 |

总体上，工程防泄漏与可追踪设计好于直接把技术指标映射成 LLM 百分比；模型算法本身属于成熟基础方法。A/B/C 共享同一证券、同一价格/量序列和大量动量信息，隔离执行不等于三份统计独立证据。

更先进应体现为：在新时段、新标的、新市场状态上可复现地提高概率质量，同时表达不确定性，并在交易用途下验证成本和风险。复杂模型名称不能替代这些能力。[Gu、Kelly、Xiu 的原始研究](https://www.nber.org/papers/w25398) 支持研究非线性交互的价值，但其资产定价任务与这里单股 1D/5D/20D 涨跌概率不同，不能据此推定树模型或神经网络在本项目一定优于 Logistic。

2025 年的 [Chronos-2 原始论文](https://arxiv.org/abs/2510.15821) 已支持多变量与协变量预测，可作为远期研究候选。引入前仍需核查预训练数据/时间范围、基准污染、收益目标转换、概率校准及成本。通用时间序列 benchmark 的进步不能当作股票 alpha 证据，当前优先级应低于数据、评价和校准。

**4. 建议的算法路线**

**A：保留原条件频率作基线，增加受验证的收缩候选。**

使用同一决策日之前成熟标签的整体上涨基率 p0，构造 Beta 收缩。为保证 Beta prior 有效，tau 必须大于 0；p0 必须位于 (0,1)，可预登记使用成熟整体标签的 Jeffreys 平滑基率 `(wins0 + 0.5) / (n0 + 1)`，并与未平滑基率基准分开披露：

```text
alpha = tau * p0
beta  = tau * (1 - p0)
p_shrunk = (weighted_wins + alpha) / (sum_weights + alpha + beta)
```

tau 和时间半衰期只能在内层历史窗口选择；raw hit rate、p0、prior 参数、权重、模型版本必须保留。等权无先验版本继续作 challenger 的基准。先比较无衰减与少量预登记半衰期，验证是否提升 OOS Brier/Log Loss，再考虑状态条件。不要同时堆叠十几个 regime 和窗口。

无权重 Bernoulli 版本对应通常的 Beta 更新；时间权重版本属于加权/幂似然近似，区间解释依赖额外模型假设，不能直接宣称普通独立 Beta-Binomial 的覆盖保证。

区分实际标签数、Kish 权重有效样本 `(sum w)^2 / sum(w^2)`、时序依赖造成的精度损失。Kish ESS 不是独立样本数；当前 `n_effective=n_raw` 只表达等权定义，不能保证时间独立。Beta 区间是模型假设下的后验可信区间；时间序列 bootstrap 区间也需要覆盖模拟，不能宣称已保证真实条件概率覆盖。

**B：把三档频率改成连续、正则化映射的研究候选。**

先保留固定 11 因子与家族限制，注册全部候选。每个内层 fold 重新做因子符号、缩放、相关性剪枝；评估选择稳定性、OOS IC 和对基准的实际提升。

首选比较“signed composite → 单变量正则 Logistic”与“当前三分桶 + 向成熟基率收缩”。连续映射避免每桶必须积累 20 个标签的结构障碍，但不产生额外独立信息；样本少时必须强收缩、披露不确定性。Isotonic 等高自由度映射仅在数据足够、独立外层评价优于低维模型时纳入。

对 overlapping labels，允许的研究方向是使用全部成熟观察训练、按标签并发度降权，并以按时间分块的 OOS 损失推断；不能把重复标签计作等量独立样本。保留现行严格采样版本作对照，所有规则变化须独立验收。

**C：先改验证和数值实现，再引入一类非线性模型。**

补充 loss、梯度范数、最大系数、迭代数与收敛状态；与成熟库 Logistic 在相同目标、正则约定、同一数据切分下做数值一致性比较。验证时只能使用训练 fold 的缩放与缺失处理。

把“train labels 必须已知”的因果要求与“共享过去价格造成样本相关”的统计问题分开。当前 causal folds/holdout 因 60-session 特征窗口再丢弃一段已经成熟的标签，较保守且损失样本。可以研究成熟标签版 causal splitter，但要保留现有严格 splitter 作对照，证明无未来可用性泄漏，并对相关损失采用稳健推断；不要直接删除 Purge/Embargo。

比较固定 L2 Logistic、有限正则/窗口候选，以及一类浅树 boosting。先选 LightGBM 或 XGBoost 之一；每个 horizon 的总配置预算建议上限 24，包含预处理、因子选择、校准与窗口组合，全部进入 registry。早停、超参、校准器选择只在外层训练区间内完成。当前默认模型继续关闭 booster，直到真实外层 OOS 和性能验收满足标准。

Platt 与 raw、Beta calibration 做配对比较，参数拟合和方法选择均隔离最终测试。[Beta calibration 原始论文](https://proceedings.mlr.press/v54/kull17a.html) 表明校准方法具有模型/分布适用条件；这里 Platt 输入 logits，不能简单套用“所有 Logistic 校准都缺 identity”的论断。

当前从较小训练量 walk-forward 模型获得的校准器，后续被用于 holdout 模型和更大训练量 current 模型。要通过外层 replay 评价整个“训练→校准→重拟合→预测”程序，记录 model/calibrator hash 与训练窗口；不能只因 calibration fitted 就把重新训练后的概率称为验证充分的校准概率。

**共识：始终保留等权基线，按历史可用策略回放。**

新增独立于权重估计的 `deployed_policy_quality`，计算真正执行策略的每日期概率、coverage、Brier、Log Loss、可靠性分箱与基准差。某路径弃权时，按照提前冻结的规则重新归一化，记录 active_path_mask；固定三路径/两路径诊断继续并列展示。

误差多样性按共同 OOS 上 `p_i-y` 的相关、Brier 残差与逐状态一致性衡量，家族 Jaccard 只作语义解释。A/B/C 的不同程序不能自动算三个独立证据。

权重候选可采用非负、和为 1、向等权收缩的受约束 stacking；用内层过去 OOS 估计，下一块评价，不能拿同一 holdout 同时筛权重、拟 ensemble calibrator 和验收。样本不足继续等权。仅在独立测试显著优于等权且稳定时启用；只赢过很弱的路径不够。

**不确定性与方向：同时报告事件概率、相对基准优势和证据等级。**

保留 P(up) 原事件定义和 >0.5 的绝对方向兼容字段，新增 `probability_edge_vs_baseline` 与 `directional_strength`。例如历史基率 60%、模型 58%，可以描述为上涨概率高于 50%，但不能描述为相对基准的看多优势。

在外层时间块上比较成对损失差与校准斜率/截距，先提供“证据不足/有改善证据/跨期稳定”的等级。不要把 confidence_score 或 ECE 翻译成“准确率百分比”。如研究收益预测区间，可参考 [Gibbs、Candès 2024 的在线分布漂移推断](https://www.jmlr.org/papers/v25/22-1218.html)，并只在 H-session 标签成熟后更新；其长期覆盖性质不等于每个状态的条件覆盖，更不自动给出经过校准的 P(up)。

**D 与经济性：先增加相对收益诊断，再建设执行模拟。**

保留原绝对涨跌目标，新增 benchmark/sector excess return 作为独立研究标签，不混进同一概率平均。多资产横截面 IC 需要同日 panel、历史成员/退市数据和可交易掩码；不能从现有单证券 IC 改名得到。

预计收益应研究状态相关的 `E[r | X]` 或 `P(up | X) * E[r | up,X] + P(down | X) * E[r | down,X]`，与 C 现行不依赖 X 的涨跌均值作比较。上涨 60%、平均涨幅 1%、平均跌幅 3% 时，预期收益为 -0.6%；高 P(up) 不保证有经济优势。

执行回测另行定义信号产生时间、下一可成交时点、持有/叠仓、仓位上限、手续费、买卖价差、滑点、冲击、停牌/退市和公司行动。US/HK 及 ETF 的费率、交易单位与成交约束分别取实际证据。当前 5/10/20 bp 只保留为 sensitivity，不冒称已完成成本后回测。PBO 与 DSR 在建立真实策略收益和完整 trial matrix 后计算；[PBO 原论文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) 与 [DSR 原论文](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) 研究的是选择/收益序列问题，不宜作为方向概率的通用 veto。

**新增信息与面板模型：在现有路径验收后扩展。**

先增加决策时已知的市场/行业收益、相对强弱、波动状态和可核对的流动性代理，逐组做消融，检查是否只是重新包装既有动量。之后研究同市场、同资产类型的层级模型：共享部分系数、ticker 截距向组基率收缩，所有资产以当时可用的历史 universe 和统一时间 cutoff 训练。20D 的短样本可以通过受约束跨资产学习增加信息，但同日相关性必须保留，不能把面板行数当作独立样本数。US/HK/杠杆 ETF 不宜直接混成一个统一基率。

盈利/财报、期权隐含信息和历史事件特征只有在可靠 `available_at`、vintage 与覆盖档案齐备后才进入新研究路径。当前最新财务、当前评级、短新闻 feed 不能回填历史。现有 News 仍在 Quant 冻结后独立运行；不得把它的事后判断接回 Quant 概率。新面板或事件模型需要显式的 Skill 版本、输入契约与 provenance 扩展，当前 OHLCV 路径的能力声明保持准确。

**5. 可执行的阶段计划与交付验收**

工期为拥有量化研究与工程能力、且数据可取得时的工作量估算，不是性能承诺；真实 forward 的观测长度由市场时间决定。

| 阶段 | 工作量估算 | 改动范围与交付 | 必须满足的退出条件 |
|---|---|---|---|
| P0-A：评价规则与证据边界 | 3–5 工作日 | 共享 A 资格函数；OOS validator；active-mask policy replay；独立 ensemble metrics；提前 sample-feasibility 预算 | 当前/OOS 用同一资格；坏 label/timing 拒绝；B 全弃权时仍可评价 A+C；单路径、旧字段缺失与 VETO 行为兼容 |
| P0-B：置信度与证据等级 | 2–3 工作日，可接上阶段 | 修改 consensus/audit/report 的证据语义；区分 support score、predictive grade、ensemble calibration | 基准失败不得获得“预测优势已验证”级别；原概率保留或 veto 遵守原规则；News 不改概率；实际待发送文本校验仍通过 |
| P1-A：真实研究数据与实验治理 | 约 1–2 周，受数据授权影响 | source/calendar/adjustment/units pilot；数据/代码版本、trial registry、锁定 holdout、prediction ledger、标签成熟回填 | 有真实可报告预测快照；没有伪造元数据/降低 gate；每个测试预测有产生时 receipt；holdout 二次用于选择自动标记污染 |
| P1-B：A/B 样本效率与概率改进 | 约 1–2 周 | Beta 收缩、少量半衰期候选、B 连续映射；依赖稳健损失/IC 推断与覆盖模拟 | 与旧 A/B、成熟基率、等权共识做同样本外层比较；只按预登记主指标选型；20D 样本不足明确披露 |
| P1-C：C 与共识候选 | 约 1–2 周 | 求解器校验、内层时序选择、一个浅树模型、校准迁移 replay、受约束 stacking | 校准器和超参不接触最终测试；候选在真实 OOS 优于简单模型/等权；无改善保留 baseline；可选模型默认关闭 |
| P2-A：执行与组合研究 | 约 2–4 周，独立于概率资格 | 成交/资金账本、成本压力、benchmark panel、PIT universe、策略 trial matrix | 账本可重算；所有失败交易/弃权保留；费用/延迟/冲击合理；PBO/DSR 输入范围完整；无策略优势时不升级策略资格 |
| P2-B：forward 与受控推广 | 与上述阶段并行，至少 8–12 周初步观察 | 冻结 champion，影子 challenger，按标签成熟回填，漂移告警与回退 | 未成熟标签不计入成绩；真实前瞻改善与可靠性成立才启用候选；20D 不把 8–12 周误称充分独立证据 |

第一版预测改进候选可争取约 6–8 周工程/研究迭代；策略引擎和长期前瞻验证是后续独立里程碑。若数据仍过不了 gate，首要交付是数据资格 pilot 与缺口，而不是宣称算法已验收。

**建议分成八个可独立回退的实施单元。**

1. `researchers/quant.py`：抽出同一资格策略；修复 20/30 不一致；保留成熟标签和未来价格扰动检查。
2. `validation/probability_quality.py`：按 session 重算 target/label_end；核查训练/校准边界；每条 rejection 给结构化原因；不让坏证据进入权重。
3. 新 `validation/policy_replay.py`：按历史决策日重新执行所有路径的冻结算法，记录可用 mask；有意保持与当前 snapshot-only analyze 分离。
4. `consensus.py` / `audit.py` / `report.py`：实际策略质量、固定子集诊断、证据等级、score 语义；不依赖“权重可估计”才能评价等权。
5. 新 `experiments/registry.py` 与预测账本：hash、参数/候选计数、使用过的时段、校准器、持有规则、数据 vintage、失败和负结果。文件 hash 保证一致性；隔离 holdout 的权限/流程才保证使用治理。
6. A/B 候选：prior、time decay、continuous map 作为显式配置，旧算法继续 baseline；模型版本和 prior policy 在结果中可追踪。
7. C 候选：标准求解器、内层选择与可选 booster；保留不加载 XGBoost/LightGBM 的当前默认合同，直到验收记录授权启用。
8. 独立 portfolio/execution 模块：使用明确 trading target；现有 snapshot 1.2 与 Quant→freeze→News→synthesis 保持兼容，必要时只给研究 ledger 新 schema，避免隐式改变主快照目标。

**6. 如何验收真正有效，而非只提高漂亮指标**

**研究数据与切分。**

先做真实小规模资格 pilot：US 股票、HK 股票、ETF 各选代表案例，核对 identity、currency/price 与 volume 单位、公司行动和价格 basis、真实交易日历、最新已闭合 session、source timestamp 的语义。Yahoo 的 bar_label 不能伪装成交易/收盘时间；provider 证据缺失应补证据或改用合格来源。上一轮真实 NVDA 路由最终 veto，因此不得把“取到真实行情”当作“已有合格市场预测”。

研究面板可先用预登记、按历史流动性/上市状态定义的 50–100 个标的，覆盖 US/HK/ETF。避免只选今天仍存续的大盘股或事后表现突出的股票。默认用户分析仍使用三年窗口；批量研究可另建 8–10 年数据档案，在每个 historical as-of 只取当时过去三年作为当前默认算法的输入。更长窗口候选单独编号、单独报告，不能偷偷混入主窗口。

最终测试建议锁定最后 6–12 个月，之前区间做至少四个外层 chronological 时间块；外层训练内部再做特征/超参/校准/权重选择。样本预算不足时减少自由度，报告不确定性，不把更短测试描述为同等证明。校准样本与权重样本可采用内层 cross-fitting，但最终测试不可兼作方法选择。

保留同日跨标的相关性和 H-session 重叠。推断按日历时间分块、保留同日资产联合结构；视研究设计进行 ticker/sector cluster 敏感性。不能把 100 只高度相关股票的预测简单视作 100 倍独立样本。US/HK 的 session 与时区分别处理。

**比较对象。**

- 原 A/B/C 与原实际等权策略，均按同一历史资格规则 replay。
- p=0.5，以及严格只用过去成熟标签估计的 expanding / rolling base rate。
- 简单正则 Logistic 与固定动量条件频率。
- 市场/行业基准条件的简单模型；若研究 excess label，则使用对应 excess 基准，和原 P(up) 分开。
- 当前 raw、Platt、候选 Beta 校准，在相同预测时点配对比较；所有校准方法的选择包含在 trial 计数中。

概率主指标采用 Brier 和 Log Loss，因为它们评价概率而不只评价 0.5 阈值的分类准确率；这一选择有 [proper scoring rules 原始研究](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf) 支持。AUC、accuracy、ECE、IC 作为辅指标，不能替代主指标。

```text
Brier Skill = 1 - BS(candidate) / BS(causal_baseline)
paired_loss_difference[t] = loss(candidate[t], y[t]) - loss(reference[t], y[t])
```

baseline 的预测必须在对应历史决策日即可取得；不以评价窗口最后才知道的整体上涨比例冒充实时预测基准。配对比较固定 coverage 或并列展示 coverage–risk 曲线；只在容易的日期预测不能冒称整体质量提升。

**建议预登记的晋级条件，均为研究验收规则而非当前概率 gate。**

| 类别 | 条件 |
|---|---|
| 完整性 | 无未来特征/训练或校准标签越界；model/data/code receipt 可重算；所有弃权与失败计入 ledger |
| 概率增量 | 预指定主指标的真实最终 OOS 成对损失差优于当前实际策略和因果基率；时间块推断区间支持改善；若试验过多需控制选择偏差 |
| 跨期稳健 | 多个外层时间块和市场状态无系统性退化；US/HK/ETF 分组报告，不能用一个组的大幅改善遮盖另一个组失效 |
| 校准 | raw 与 calibrator 的独立测试配对；可靠性分箱公布计数与区间；校准斜率/截距及 Brier/Log Loss 没有实质退化；ECE 单独降低不够 |
| 覆盖 | 分母由冻结的决策 cohort 定义；完整病例结果与整套 policy 结果并列；样本不足标证据不足，不能仅因输出成功晋级 |
| 组合/策略 | 如提出盈利声明，再要求真实成交假设下净回报、风险、成本压力和 trial-adjusted 审计；不反向取消合法方向概率 |
| 运行性能 | 在相同机器/快照与模型配置下比较 p50/p95 时间、峰值内存、失败率和可重算性；具体预算从当前实测与使用要求设定，不能用未测试库推定性能 |

改善的最小经济/统计幅度由真实开发集的效应、噪声和使用要求预登记；不预设“准确率提高 10%”之类承诺。若无候选通过，结论就是保留 baseline 与当前证据等级。

可用的第一版晋级规则是：把 Brier 设为预登记主指标，最终真实 OOS 的成对 Brier 损失差 95% 区间上界小于 0；同时 Log Loss 不劣于开发阶段预登记的容忍幅度，coverage 下降需单独说明且符合使用要求。时序 block/cluster 推断方案和试验调整在看最终测试前冻结。样本无法给出有意义区间时保留“证据不足”，不机械套用显著性阈值。该规则只决定 challenger 晋级，不改变现行 raw probability 的可报告资格。

**工程验证、统计模拟、市场 OOS、前瞻验证各自留独立记录。**

工程回归增加：A 同一资格、5D/20D 默认样本预算、B 弃权下 A+C 评价、坏 OOS 时序/target、模型与校准 receipt 不一致、未来价格扰动、holdout 标签只改评分不改先前参数、旧可选字段兼容、News 不改概率、交付校验。

统计模拟增加：无可预测信息的随机游走、带基率漂移、波动聚集、结构突变、已知弱条件信号、多资产共同冲击。分 seed 和生成机制统计误报、覆盖与检测功效；Monte Carlo 区间保留。模拟能验证推断方法在假设下的行为，不能替代市场 OOS。

前瞻 ledger 存下 `prediction_time / asof / horizon / decision_session / model_hash / calibrator_hash / active_paths / probability / abstain_reason`，标签成熟后追加真值，不回写历史预测。日常盯 quote 的成功率不是前瞻预测质量。8–12 周可做流程与初步漂移观察；20D 单标的独立标签仍很少，不能用该长度宣称长期可靠。

**7. 第一轮实施顺序**

先完成 F03/F04 的规则和证据一致性，再修 F02 的 policy 评价与 F01 的预算披露，随后处理 F05 的置信度语义；这些工作无需赌新模型能提高收益。下一步做合格真实数据 pilot 与 experiment registry，然后进入 A/B 收缩与连续映射、C 校准/求解器验证，最后比较一个非线性模型和受约束共识。

这条路线的核心交付是：每个升级都能说明“在什么数据和资格规则下，对哪个基准，提高了什么指标，区间有多宽，哪些场景仍失败”。模型名、测试数和一次漂亮的合成回测不能代替这些回答。
