# 量化研究路径与运行产物

本文将当前本地代码映射到 A/B/C/D、共识、E 审计和 News/Sentiment；名称借鉴上游设计，不表示自动调用上游 Skill、安装上游库或创建多个 Agent。上游来源与许可见 [upstream-provenance.md](upstream-provenance.md)。

## 执行映射

`scripts/quant_research.py analyze` 调用 `quant_research/orchestrator.py`，先重验冻结 Snapshot 的来源与数据门槛，再尝试四个隔离研究器、共识和审计。只有 prediction eligibility 存在致命阻断时跳过预测路径；日历回退、复权未知、弱 OOS、缺校准或策略审计不足不会提前取消 Quant。D/E 不产生预测概率。所有路径只读取同一冻结 Snapshot，当前特征只来自 OHLCV。

| 路径 / 阶段 | 当前代码（位于 `scripts/quant_research/`） | 运行输出 / 角色 |
|---|---|---|
| A — Quant Research | `researchers/quant.py` | `researchers/quant.json`，forecast |
| B — Factor Research | `researchers/factor.py` | `researchers/factor.json`，forecast |
| C — ML Research | `researchers/ml.py`、`validation/purged_cv.py` | `researchers/ml.json`，forecast |
| D — Factor Backtest Diagnostic | `researchers/factor_backtest.py` | `researchers/factor_backtest.json`，diagnostic |
| 概率共识 | `consensus.py` | `quant_result.json.consensus`，只接受 A/B/C 的有效预测 |
| E — Adversarial / Backtesting Bias Audit | `audit.py` | `quant_result.json.adversarial_audit`，验证/否决，不预测 |
| News / Event / Opinion Sentiment | `news/` | 独立 `news_result.json`，不产生概率 |
| Final Synthesis | `synthesis.py`、`report.py`、`delivery.py` | `report.json`、`report.md`、有界 `agent_summary.json`、`final_response.md` 与待发送文本校验，分类证据综合 |

E 没有单独 `researcher_id` 或 `researchers/audit.json`；在共识之后执行，覆盖四条研究路径及共识的证据。D 和 E 均不进入预测概率平均。四个研究器的详细结果必须保留，不以报告中的单个方向摘要替代。

## A — 假设与历史条件频率

固定检验“20 个 session 动量符号与未来 H-session 收益”的假设，用同符号、非重叠历史标签计算上涨频率和均值收益；扩展窗口在每个决策日只使用标签结束日期不晚于该决策日的历史样本。历史账本为全历史逐日 causal walk-forward：每个成熟决策日记录当时会给出的预测（逐条保留训练标签边界）；相邻 H-session 标签重叠，因此同时披露 `prediction_days`、保守的 `non_overlapping_prediction_count` 与 `effective_sample_size_estimate`，阈值使用非重叠计数，重复标签不按等量独立样本计数。输出决策日、标签结束日、训练标签边界及 Brier / 基准。

查看 `probability_source`、`n_raw`、`n_effective`、`n_oos`、历史跨度及 `validation.oos_forecasts`。当前至少 21 根收盘才能形成信号，至少 12 个非重叠成熟匹配才报告条件概率（52 周默认窗口下校准的门槛）；历史 OOS 预测调用相同的 `MINIMUM_MATCHING_SAMPLES`，避免用较宽松规则评价较严格的当前规则。当前不使用 Bayesian prior 或时间衰减，故等权时 `n_effective = n_raw`。Wilson 值仅为 IID 近似区间，时间相关下的覆盖率尚未验证。无可用时间外预测时数值结果仅为 `partial`。这是固定动量假设 baseline，不是任意策略发现或成本后收益预测。

## B — 因子验证、去冗余与频率映射

路径自行构造动量、反转、趋势、波动和量价因子，记录公式、IC、分位收益差、覆盖、信号换组和 1D/5D/20D 衰减。前段 50% 样本选择因子、确定符号和标准化参数，相关性筛选与同家族限制减少重复证据；此后按时间滚动（rolling origin）使用当日已成熟的非重叠标签做三档经验频率映射，当前概率只使用决策日之前已成熟的标签，绝不使用未来标签。

核对 `validation.selected_factors`、`factor_decay`、`calibration_observations`、`calibration_bucket_counts`、`calibration_bucket_raw_probabilities`、`probability_shrinkage`、`sample_feasibility`、`holdout_brier` 和基准。52 周标准下概率分桶至少需要每桶 6 个成熟校准观察（三桶共 18 个），因子选择段至少需要 10 个非重叠标签。诊断按固定 50% 选择段、60-session 特征预热和 H+1 标签间距估算容量：5D 约需 240 根日线（48 周），1D 约 160 根，20D 约 540 根（超过 52 周，故 20D 因子路径在 52 周窗口下不可用）。该估算假设桶能均衡分配，不保证因子分数完整、无并列或跨状态稳定；不自动扩窗、不降低 6 样本门槛。桶样本偏薄时概率按 12 个观察的先验权重向已成熟基准频率收缩，并披露 `bucket_weight`。rolling-origin 评估预测不足 10 个时数值结果为 `partial`。单 ticker 的 Rank/Spearman IC 是时间序列统计；代码的 `top_quantile_turnover` 是信号换组诊断，不能称为组合实际换手或成本后业绩。

B 的选择/衰减诊断要求 `index + horizon < diagnostics_label_end_exclusive`，不读取 holdout 标签；1D/5D/20D forward 数组各算一次。当前证据极性来自符号校正后的 `current_contribution`，训练 IC 方向另列，不能用训练正 IC 替代当前信号。`sample_feasibility` 披露校准/holdout 样本预算，不为满足阈值扩窗。

B 在前段训练样本按绝对 Rank IC 排序，`abs(IC) >= 0.02` 是预设的启发式筛选线，不是显著性检验。随后去除与已保留因子绝对相关系数 `>= 0.85` 的候选，每家族最多两项、总计最多五项。`close_location` 按价格区间位置归为 trend，`overnight_gap` 按隔夜价格收益归为 momentum；家族修正会影响同家族数量限制。该选择过程没有用 FDR 控制训练搜索的错误发现率，不能声称选中的因子具有统计显著性。

新增 `validation.oos_factor_inference` 在选段之后的 rolling-origin evaluation 窗口逐项报告所有尝试因子的单标的时间序列 Spearman IC、分半方向一致性、循环 moving-block bootstrap 近似区间和 circular-shift 零假设诊断，并以 Benjamini–Yekutieli 对全部候选检验做保守 FDR 调整。方法、每项样本量、时间区间、`trials`、可计算 p 值数和调整方法都写入审计。该窗口只用于报告 OOS 结果，不参与因子筛选、方向或概率调整。Circular-shift 的平稳性假设及 bootstrap 覆盖率未验证；样本不足时 p 值/调整值为空，结果仍是探索性 IC 诊断。DSR 继续 `not_assessed`，因为没有策略收益序列与有效交易规则。

OOS 的 BY 调整以全部尝试候选作为检验家族，并计入无法计算 p 值的候选数量。调整值是最终评价窗口的诊断输出；当前没有用 `p_adjusted < 0.05` 等阈值再选因子，也不能把它追溯为训练选择阈值 `0.02` 的 FDR 保证。

## C — 逻辑回归与防泄漏验证

使用路径内独立 OHLCV 特征和确定性 L2 Logistic Regression。验证包含闭区间 Purged K-Fold、Embargo、仅用过去数据的全历史逐日 causal walk-forward 账本、fold 内标准化和最后的 chronological holdout。当前预测可在验证固定后用已成熟标签重拟合，但不使用 holdout 指标调整特征或参数。

核对 `validation.purged_kfold`、`causal_walk_forward`、`probability_calibration`、`calibration_training`、`calibration_transfer`、`final_holdout`、`historical_asof_ledger` 和两类 leakage 声明。当前前 holdout 训练至少需 60 个成熟样本且包含两类标签；账本 walk-forward 训练下限为 30 个成熟前置样本（均按 52 周默认窗口校准）；Platt 校准至少需 40 个有效 walk-forward 预测且有两类标签。校准记录逐条保留预测时训练标签边界与校准标签边界，两者都必须早于对应决策日；冻结映射的拟合范围限定在预留 holdout 之前的非重叠子集，`final_holdout_used` 必须为 false。账本每一行按自身 origin 逐条重拟合 Platt：只使用标签在该行决策日之前已成熟的 walk-forward OOS 对数几率（成熟观测不足 40 或单一类别时保留 raw 输出并标 `uncalibrated`），并逐条记录校准标签边界与校准系数（供审计逐条复核概率值）；更晚拟合的映射绝不应用到更早的行；冻结映射只用于最终 holdout 转移检查与当前预测；逐日账本的相邻标签重叠，行级记录披露 `prediction_days`、`non_overlapping_prediction_count` 与 `effective_sample_size_estimate`。最终 holdout 只评价冻结训练/校准方案，不用于校准或选择参数。没有校准时保留 raw logistic probability 并明确标 `uncalibrated`、`partial`，不能称为校准概率；残留 train/test 区间重叠时为 `invalid`，概率置空。迁移后的 Platt 映射还会应用到在全部成熟标签上重拟合的当前模型，这一应用无法再用 holdout 复验；`calibration_transfer` 用未触碰的 chronological holdout 给出同一映射的 raw 与 calibrated Brier 对照，作为该迁移的唯一直观指标，不能当作迁移已校准证明。

当前仓库没有 `_xgb_estimator.py`，也没有嵌套时序早停实现；Research C 只运行标准库确定性 L2 Logistic Regression。`model_selection` 将 XGBoost/LightGBM 标为 `not_implemented_not_loaded`，不会探测、导入、安装或重试这些可选依赖。即使本地计算与 Platt 校准完成，C 仍标为 `partial`。不得把基线称为完整 boosted-model 验证，也不得把交叉验证 AUC 单独作为方向结论。要引入 boosted 模型，必须先实现嵌套训练/早停、逐折 Purge/Embargo、隔离最终 holdout，并在独立 OOS 与性能验收通过前保持关闭。

`mode=standard/strict` 不改变预测资格；A/B/C 在各自预定义样本/计算条件满足时生成概率。严格模式不会因 OOS 数不足或未校准而排除已成功生成的路径，缺口降低 confidence 并使报告 `DEGRADED`；显式时间泄漏、无效目标或预测失败仍不可报告。`forecast_eligible` 是路径预测资格，`reporting_status` 说明最终展示状态。

## D — 时间序列因子 IC / forward-return 诊断

D 自行构造固定因子，按时间 70/30 划分训练和 holdout，分位切点只用训练数据确定，评估 1D/5D/20D 非重叠 forward-return。查看 `validation.factor_metrics` 中各 horizon 的训练/holdout 样本和 IC；当前可用 holdout IC 诊断门槛是 20 个观察，不足的数值仅作描述并披露状态。

`result_role: diagnostic`，`prob_up` 和 `expected_return` 不作为预测输出。单证券输出不能称为横截面 Rank IC、长短组合、基准相对 NAV 或真实交易回测。完整横截面路径需多 ticker 因子面板、PIT universe、交易日历、复权/可交易价格、安全掩码和基准；当前未实现该引擎，即使用户提供这些输入也不能宣称本地代码已完成完整回测。

## 共识与 E — 偏差审计

共识只纳入通过 `forecast_assessment()`、身份匹配、有定量来源且状态为 `success/partial` 的 A/B/C 数值预测，当前采用等权概率平均，报告 range、dispersion、证据家族重叠和分类 confidence。新增 `oos_probability_quality` 报告每路径证据审计、当前合格路径交集、全候选路径交集和共同完整预测样本的 Brier、Log Loss、可靠性分箱、ECE、覆盖率及弃权率；逐日账本的每路径记录同时披露 `prediction_days`、保守的 `non_overlapping_prediction_count` 与 `effective_sample_size_estimate`，阈值使用非重叠计数。冻结行情验证决策 session、精确 H-session 标签、目标真值、目标定义、标签成熟度和预测时训练标签边界；无效样本不进入质量或权重指标。`algorithm_policy_replay` 按每个历史日期实际有有效预测的路径做等权均值，并与相同日期上的因果扩展基率比较，披露 coverage、`active_path_mask`、Brier Skill、保守的非重叠预测日计数及 moving-block bootstrap 百分位区间（阈值使用非重叠计数；独立块少于 4 时区间标记为不可估计、不据此判分）。它是历史策略诊断，不因今天的活跃路径集合筛选过去日期，也不构成真实独立市场 OOS 验收。`ensemble_calibration` 在预测日前 60% 上拟合确定性 Platt、在净化后的后段做回顾性检查；检查段与各路径既有评估窗口（含 Research C 的最终 holdout）存在重叠，故该检查是描述性、非独立确认；仅当检查块 calibrated Brier 严格低于 raw 等权均值时，校准值才成为 reportable probability（`raw_probability` 保留等权均值），并更新 `calibration_status`。新增 `skill_weight_validation` 仅做严格先早后晚（评价段从最后估计标签成熟后开始）的实验性 Brier Skill 权重估计/评价；当前 `weighting` 仍为 equal，不能把实验权重用于当前预测。等权均值不等于经过独立校准的 ensemble 概率——只有上述回顾性检查通过后才称校准 ensemble（描述性检查，非独立确认）。`confidence_score` 是启发式研究支持度，不是预测准确率；`predictive_evidence_grade` 单独呈现基准技能点估计和证据限制。单路径称为 `single_path`；两路径结果披露缺失路径。详见 [consensus-protocol.md](consensus-protocol.md) 与 [probability-policy.md](probability-policy.md)。

E 检查快照身份/digest、路径快照一致性、概率来源和范围、invalid 状态、残留区间重叠、结构化特征/标签时间声明、chronological OOS、Brier 基准与已观察的候选因子数量。声明检查不等于对每个特征实现的形式证明。

PBO、DSR、实际交易成本/冲击、幸存者偏差缺乏完整试验收益矩阵、试验数量、执行假设、PIT universe 或退市覆盖时必须标为未评估；已有数值也不能自动当作已审计。报告可给出 5/10/20 bp 假设往返成本敏感性，但这不构成实际费用、滑点、市场冲击或策略净收益回测。经济性字段与预测置信度、概率及 News 输入隔离。当前没有正式 CPCV/PBO/DSR 计算、成本后执行模拟、长期可靠性权重或有治理凭据的 holdout/forward store。

若 `adversarial_audit.may_report_direction` 为 false，运行器将共识设为 `research_invalid` 并清空可报告概率。`pre_veto_prob_up` 仅用于追踪被否决结果，不能拿它交付正常方向结论。

## News/Sentiment → Final Synthesis

完整 Quant（含共识和 E）先写入 `quant_result.json` 并生成 `.freeze.json` SHA-256 凭据。随后 snapshot-only loader 构造受限 NewsInput，分析器只接收该内存契约和可选衰减配置，看不到 Quant。News 依次执行 PIT 筛选、canonical event 去重、事实/观点分离、事件筛查、独立观点情绪、session 衰减和冻结行情反应验证。详细方法以 [news-analysis.md](news-analysis.md) 为准。

News 结果独立冻结后，Final Synthesis 验证两份 digest，再报告对齐/分歧、风险和分类置信度；原 Quant 概率保持不变。没有可用新闻时仍生成 `not_assessed` 结果并完成综合，不能跳过 News 后把 Quant 称为已完成消息面交叉验证。

## 交付前检查

1. 通用分析/简析默认完成完整流程。先读取 summary `final_response.md` / `agent_summary.json`；完整验证与 IC/bias 在 `report.md`，源、reason codes、manifest、逐路径输出在 JSON/debug。CLI 成功不代表研究通过；检查 `analysis_status`、`prediction_eligibility`、`reporting_status`、`may_report_direction` 和路径排除原因，不把完整矩阵加载给 LLM。
2. 检查共识可用路径、原始 P(up)、分歧、多样性、E 的 veto/warnings 和未评估项，使用实际报告状态，不自行补概率或修改数值。
3. 检查独立 `quant_result.json`、`news_result.json` 与两份 `.freeze.json`；缺失/摘要不符时不声称最终综合已验证。
4. 检查 `news_result.status`、时间覆盖、事件/观点分离、行情反应缺口和 `report.json.synthesis`；缺失字段保留为空或未评估。
5. Summary 聊天回复包含概率/方向/confidence/报告状态、证据、限制、News/关系及风险；标准 `report.md` 保留 source/time、Quant、IC/bias 实际范围、消息、关系和综合；debug 留在 JSON。
6. 简析使用程序生成的 `final_response.md`，保留标的/窗口/as-of、Quant 五字段（diversity 存在时）、概率说明、校准/不可用原因及消息关系原行。保存实际待发送文本并运行 `validate-response --analysis-dir <report_dir> --response-file <完整回复.md>`；失败则按同一实际产物修复后重验。通过后发送同一文本，不能再将概率摘要掉。此检查验证字段与产物绑定，不代表所有自然语言推断均已验证。
