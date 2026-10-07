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

固定检验“20 个 session 动量符号与未来 H-session 收益”的假设，用同符号、非重叠历史标签计算上涨频率、均值收益和 Wilson 区间；后段时间序列采用扩展窗口预测并报告 Brier / 基准。

查看 `probability_source`、匹配样本数和 `validation.oos_predictions`。当前至少 21 根收盘才能形成信号，至少 30 个非重叠匹配才报告条件概率；无可用时间外预测时数值结果仅为 `partial`。这是固定动量假设 baseline，不是任意策略发现或成本后收益预测。

## B — 因子验证、去冗余与频率映射

路径自行构造动量、反转、趋势、波动和量价因子，记录公式、IC、分位收益差、覆盖、信号换组和 1D/5D/20D 衰减。前段样本选择因子、确定符号和标准化参数，相关性筛选与同家族限制减少重复证据；后续样本进行三档因子分数的经验频率映射，最后的时间段报告 holdout。

核对 `validation.selected_factors`、`factor_decay`、`calibration_observations`、`calibration_bucket_counts`、`holdout_brier` 和基准。当前概率分桶至少需要 20 个校准观察，holdout 预测不足 10 个时数值结果为 `partial`。单 ticker 的 Rank/Spearman IC 是时间序列统计；代码的 `top_quantile_turnover` 是信号换组诊断，不能称为组合实际换手或成本后业绩。

B 的选择/衰减诊断要求 `index + horizon < diagnostics_label_end_exclusive`，不读取 holdout 标签；1D/5D/20D forward 数组各算一次。当前证据极性来自符号校正后的 `current_contribution`，训练 IC 方向另列，不能用训练正 IC 替代当前信号。`sample_feasibility` 披露校准/holdout 样本预算，不为满足阈值扩窗。

## C — 逻辑回归与防泄漏验证

使用路径内独立 OHLCV 特征和确定性 L2 Logistic Regression。验证包含闭区间 Purged K-Fold、Embargo、仅用过去数据的 causal walk-forward、fold 内标准化和最后的 chronological holdout。当前预测可在验证固定后用已成熟标签重拟合，但不使用 holdout 指标调整特征或参数。

核对 `validation.purged_kfold`、`causal_walk_forward`、`probability_calibration`、`final_holdout` 和两类 leakage 声明。当前前 holdout 训练至少需 160 个成熟样本且包含两类标签；Platt 校准至少需 40 个有效 walk-forward 预测且有两类标签。没有校准时保留 raw logistic probability 并标 `partial`，不能称为校准概率；残留 train/test 区间重叠时为 `invalid`，概率置空。

当前不含 XGBoost/LightGBM 比较，因此即使本地计算与校准完成，C 仍标为 `partial`。不得把基线称为完整 boosted-model 验证，也不得把交叉验证 AUC 单独作为方向结论。

`mode=standard/strict` 不改变预测资格；A/B/C 在各自预定义样本/计算条件满足时生成概率。严格模式不会因 OOS 数不足或未校准而排除已成功生成的路径，缺口降低 confidence 并使报告 `DEGRADED`；显式时间泄漏、无效目标或预测失败仍不可报告。`forecast_eligible` 是路径预测资格，`reporting_status` 说明最终展示状态。

## D — 时间序列因子 IC / forward-return 诊断

D 自行构造固定因子，按时间 70/30 划分训练和 holdout，分位切点只用训练数据确定，评估 1D/5D/20D 非重叠 forward-return。查看 `validation.factor_metrics` 中各 horizon 的训练/holdout 样本和 IC；当前可用 holdout IC 诊断门槛是 20 个观察，不足的数值仅作描述并披露状态。

`result_role: diagnostic`，`prob_up` 和 `expected_return` 不作为预测输出。单证券输出不能称为横截面 Rank IC、长短组合、基准相对 NAV 或真实交易回测。完整横截面路径需多 ticker 因子面板、PIT universe、交易日历、复权/可交易价格、安全掩码和基准；当前未实现该引擎，即使用户提供这些输入也不能宣称本地代码已完成完整回测。

## 共识与 E — 偏差审计

共识只纳入通过 `forecast_assessment()`、身份匹配、有定量来源且状态为 `success/partial` 的 A/B/C 数值预测，当前采用等权概率平均，报告 range、dispersion、证据家族重叠和分类 confidence。等权均值不等于经过独立校准的 ensemble 概率。单路径称为 `single_path`；两路径结果披露缺失路径。详见 [consensus-protocol.md](consensus-protocol.md) 与 [probability-policy.md](probability-policy.md)。

E 检查快照身份/digest、路径快照一致性、概率来源和范围、invalid 状态、残留区间重叠、结构化特征/标签时间声明、chronological OOS、Brier 基准与已观察的候选因子数量。声明检查不等于对每个特征实现的形式证明。

PBO、DSR、交易成本/冲击、幸存者偏差缺乏完整试验收益矩阵、试验数量、执行假设、PIT universe 或退市覆盖时必须标为未评估；已有数值也不能自动当作已审计。当前没有正式 CPCV/PBO/DSR 计算、成本后执行模拟、长期可靠性权重或有治理凭据的 holdout/forward store。

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
