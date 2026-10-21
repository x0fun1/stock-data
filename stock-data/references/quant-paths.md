# 量化研究路径与运行产物

本文将当前本地代码映射到 A/B/C/D、共识、E 审计和 News/Sentiment；名称借鉴上游设计，不表示自动调用上游 Skill、安装上游库或创建多个 Agent。上游来源与许可见 [upstream-provenance.md](upstream-provenance.md)。

## 执行映射

`scripts/quant_research.py analyze` 调用 `quant_research/orchestrator.py`，先重验冻结 Snapshot 的来源与数据门槛，再尝试四个隔离研究器、共识和审计。只有 prediction eligibility 存在致命阻断时跳过预测路径；日历回退、复权未知、弱 OOS、缺校准或策略审计不足不会提前取消 Quant。D/E 不产生预测概率。所有路径只读取同一冻结 Snapshot，当前特征只来自 OHLCV。

| 路径 / 阶段 | 当前代码（位于 `scripts/quant_research/`） | 运行输出 / 角色 |
|---|---|---|
| A — Quant Research | `researchers/quant.py` | `researchers/quant.json`，forecast |
| B — Factor Research | `researchers/factor.py` | `researchers/factor.json`，forecast |
| C — ML Research | `researchers/ml.py`、`validation/probability_quality.py`、`ledger.py` | `researchers/ml.json`，forecast |
| D — Factor Backtest Diagnostic | `researchers/factor_backtest.py` | `researchers/factor_backtest.json`，diagnostic |
| 概率共识 | `consensus.py` | `quant_result.json.consensus`，只接受 A/B/C 的有效预测 |
| E — Adversarial / Backtesting Bias Audit | `audit.py` | `quant_result.json.adversarial_audit`，验证/否决，不预测 |
| News / Event / Opinion Sentiment | `news/` | 独立 `news_result.json`，不产生概率 |
| Final Synthesis | `synthesis.py`、`report.py`、`delivery.py` | `report.json`、`report.md`、有界 `agent_summary.json`、`final_response.md` 与待发送文本校验，分类证据综合 |

E 没有单独 `researcher_id` 或 `researchers/audit.json`；在共识之后执行，覆盖四条研究路径及共识的证据。D 和 E 均不进入预测概率平均。四个研究器的详细结果必须保留，不以报告中的单个方向摘要替代。

## A — 假设与历史条件频率

固定检验“20 个 session 动量符号与未来 H-session 收益”的假设，用同符号、非重叠历史标签计算上涨频率和均值收益；扩展窗口在每个决策日只使用标签结束日期不晚于该决策日的历史样本。历史账本为全历史逐日 causal walk-forward：每个成熟决策日记录当时会给出的预测（逐条保留训练标签边界）；相邻 H-session 标签重叠，因此同时披露 `prediction_days`、保守的 `non_overlapping_prediction_count` 与 `effective_sample_size_estimate`，阈值使用非重叠计数，重复标签不按等量独立样本计数。输出决策日、标签结束日、训练标签边界及 Brier / 基准。

查看 `probability_source`、`n_raw`、`n_effective`、`n_oos`、历史跨度及 `validation.oos_forecasts`。当前至少 21 根收盘才能形成信号，至少 12 个非重叠成熟匹配才报告条件概率（52 周默认窗口下校准的门槛）；历史 OOS 预测调用相同的 `MINIMUM_MATCHING_SAMPLES`，避免用较宽松规则评价较严格的当前规则。当前不使用 Bayesian prior 或时间衰减，故等权时 `n_effective = n_raw`。新增**未启用的收缩候选**：`validation.quant_shrinkage_candidate` 用 `(wins + 12×p0) / (n + 12)` 把条件频率拉向决策日已成熟的同窗口无条件上涨率 `p0`，每条 OOS 行同时保存 `causal_base_rate` 与 `shadow_shrunk_probability` 供按日配对评估；`used_for_reported_probability` 恒为 false，发布概率仍是原条件频率。12 是预登记先验，不是在本仓库结果上调出的最优值。Wilson 值仅为 IID 近似区间，时间相关下的覆盖率尚未验证。无可用时间外预测时数值结果仅为 `partial`。这是固定动量假设 baseline，不是任意策略发现或成本后收益预测。

## B — 因子验证、去冗余与频率映射

路径自行构造动量、反转、趋势、波动和量价因子，记录公式、IC、分位收益差、覆盖、信号换组和 1D/5D/20D 衰减。前段 50% 样本选择因子、确定符号和标准化参数，相关性筛选与同家族限制减少重复证据；此后按时间滚动（rolling origin）使用当日已成熟的非重叠标签做三档经验频率映射，当前概率只使用决策日之前已成熟的标签，绝不使用未来标签。

核对 `validation.selected_factors`、`factor_decay`、`calibration_observations`、`calibration_bucket_counts`、`calibration_bucket_raw_probabilities`、`probability_shrinkage`、`sample_feasibility`、`holdout_brier` 和基准。52 周标准下概率分桶至少需要每桶 6 个成熟校准观察（三桶共 18 个），因子选择段至少需要 10 个非重叠标签。诊断按固定 50% 选择段、60-session 特征预热和 H+1 标签间距估算容量：5D 约需 240 根日线（48 周），1D 约 160 根，20D 约 540 根（超过 52 周，故 20D 因子路径在 52 周窗口下不可用）。该估算假设桶能均衡分配，不保证因子分数完整、无并列或跨状态稳定；不自动扩窗、不降低 6 样本门槛。桶样本偏薄时概率按 12 个观察的先验权重向已成熟基准频率收缩，并披露 `bucket_weight`。**选择后重用必须披露**：`sample_feasibility` 现在给出 `selection_labels_reused_for_calibration`、`cross_boundary_calibration_labels` 和 `post_selection_calibration_labels`，`leakage_audit.calibration_reuses_selection_labels` 明确说明桶账本包含选择段标签，不能称全部桶样本为独立校准证据。另计算一个只使用选择段之后标签的 `honest_calibration_candidate`（`used_for_reported_probability: false`）；预算不足时它必须 `abstained`，不得用降低每桶门槛来补足。rolling-origin 评估预测不足 10 个时数值结果为 `partial`。单 ticker 的 Rank/Spearman IC 是时间序列统计；代码的 `top_quantile_turnover` 是信号换组诊断，不能称为组合实际换手或成本后业绩。

B 的选择/衰减诊断要求 `index + horizon < diagnostics_label_end_exclusive`，不读取 holdout 标签；1D/5D/20D forward 数组各算一次。当前证据极性来自符号校正后的 `current_contribution`，训练 IC 方向另列，不能用训练正 IC 替代当前信号。`sample_feasibility` 披露校准/holdout 样本预算，不为满足阈值扩窗。

B 在前段训练样本按绝对 Rank IC 排序，`abs(IC) >= 0.02` 是预设的启发式筛选线，不是显著性检验。随后去除与已保留因子绝对相关系数 `>= 0.85` 的候选，每家族最多两项、总计最多五项。`close_location` 按价格区间位置归为 trend，`overnight_gap` 按隔夜价格收益归为 momentum；家族修正会影响同家族数量限制。该选择过程没有用 FDR 控制训练搜索的错误发现率，不能声称选中的因子具有统计显著性。

新增 `validation.oos_factor_inference` 在选段之后的 rolling-origin evaluation 窗口逐项报告所有尝试因子的单标的时间序列 Spearman IC、分半方向一致性、循环 moving-block bootstrap 近似区间和 circular-shift 零假设诊断，并以 Benjamini–Yekutieli 对全部候选检验做保守 FDR 调整。方法、每项样本量、时间区间、`trials`、可计算 p 值数和调整方法都写入审计。该窗口只用于报告 OOS 结果，不参与因子筛选、方向或概率调整。Circular-shift 的平稳性假设及 bootstrap 覆盖率未验证；样本不足时 p 值/调整值为空，结果仍是探索性 IC 诊断。DSR 继续 `not_assessed`，因为没有策略收益序列与有效交易规则。

OOS 的 BY 调整以全部尝试候选作为检验家族，并计入无法计算 p 值的候选数量。调整值是最终评价窗口的诊断输出；当前没有用 `p_adjusted < 0.05` 等阈值再选因子，也不能把它追溯为训练选择阈值 `0.02` 的 FDR 保证。

B 另有三块可靠性诊断，全部只做披露、不参与概率计算。

`sample_diagnostics` 把样本拆成四个数：`raw_sample_count`（决策日减去 H 得到的成熟标签数）、`nonoverlap_sample_count`（按 H+1 间距的保守独立样本数）、`effective_sample_size_estimate`（`raw / H`，明确标注 `approximation_only_raw_over_horizon`）和 `sample_coverage`。`overlapping_observations_are_independent` 固定为 `false`。52 周、250 根日线、60 session 预热下的实际值是（INTC 5D）：`raw_sample_count` 185、`sample_coverage` 0.974；`nonoverlap_sample_count` 取决于取样规则并由 `nonoverlap_count_basis` 标注——锚定相位（生产默认）为 **27**，旧间距回退为 31；20D 锚定相位为 8、旧间距为 9。这些数字说明为什么 B 的概率在 52 周下必然带厚区间，也说明为什么 `raw / H` 不能被当作精确有效样本量。

`probabilistic_scores` 在**同一批日期**上与同期历史基准率配对比较，输出 `brier`、`base_rate_brier`、`brier_skill_vs_base_rate`、`log_loss`、`base_rate_log_loss`、`calibration_slope`、`calibration_intercept`、`paired_common_samples`，以及"模型减基准"的 Brier 差与其循环移动块区间。区间在样本不足时返回 `insufficient_samples` 而不是给一个窄区间；`calibration_slope` 由 `logit(p)` 对标签的最小二乘拟合得到，只作诊断。基准是同日历史频率，因此差异不可能来自样本不同。

`selection` 记录选择规则本身：`metric`、`pool_observations`（选择段全部成熟标签）与 `spaced_observations`（按 H+1 间距）、预设阈值（`minimum_abs_rank_ic` 0.02、`minimum_sign_consistency` 2/3、`max_factors` 5、`max_factors_per_family` 2、`max_abs_correlation` 0.85）、逐因子的依赖感知 IC（含 moving-block 区间、子窗口 IC、`sign_consistency`、`stability_would_pass`）、`rejected` 拒绝日志，以及 BY 多重检验披露（`used_as_selection_gate: false`）。`overlap_treated_as_dependence` 标明重叠标签被当作依赖而非独立证据。

`qualification` 把"能否投票"写成显式路由：`route` 只可能是 `legacy_bucket` 或 `abstained`，`route_reason` 列出未达标的项目；`checks` 给出每项观测值与预设下限。阈值固定在 `reliability.QUALIFICATION_THRESHOLDS` 模块常量里，明确 `thresholds_adjustable_per_symbol: false`。`withheld_models.logistic` 说明为什么一个拟合概率模型在 52 周下不可用：训练 60 + 校准 30 + 验证 30 三个**不可共用**的非重叠阶段合计需要 120 个标签，而 5D 只有 31 个、20D 只有 9 个，缺口 89 / 111。该字段不是"暂未实现"，而是"按方案自身的样本约束不具备资格"。

依赖感知选择规则（`selection.overlapping_ic`，`selection_override="overlapping_block"`）用选择段全部成熟标签估计 Rank IC、以 H+1 为块长做移动块自助法、并要求 3 个子窗口的符号一致性 ≥ 2/3。它已实现并在 `scripts/selection_ab.py` 的 36 标的注册池上做过预先登记的新旧对照：两臂 Brier 为 0.2718 / 0.2681，差值为 −0.0040（新规则略优），但 holdout 只提供 15 个可评分 session，低于 session 块自助法所需的 24 个，因此按登记规则判为 `indeterminate`，**线上仍使用旧的间距点估计规则**。符号聚类区间（披露项，非判定项）为 [−0.0118, +0.0023]，同样跨零。结论：该替代规则没有获得证据支持，也没有被证据否定；换用需要更长的窗口，而不是调阈值。

需要明确区分：以上都是可靠性披露。本轮没有引入拟合概率模型，没有改动概率聚合顺序，也没有声称预测准确率提升。`scripts/selection_ab.py` 的两臂共用同一份 52 周数据，比较的是选择估计量而不是新增历史。

### B 的 52 周加固（v2.19.0）

**资格是权威，不是标签。** `qualification.route = abstained` 时，Research B 一律不产出可报告概率：数值估计移入 `validation.unqualified_estimate`（含 `withheld_reason`），`status` 置 `insufficient_data`，`prob_up = None`。`consensus.forecast_assessment` 另做独立契约检查：若某路径声明 `route: abstained` 却仍带数值概率，记 `QUALIFICATION_CONTRACT_VIOLATION` 并按致命项排除该路径——只靠路径自述不足为凭。该排除只作用于该路径，不影响 A/C/D；数据 VETO 与路径弃权是两件事（前者否决整个预测，后者只是该路径无票）。

**取样相位锚定在日历上。** 旧实现以窗口内第 60 根 K 线为锚，窗口向前滑动一天，31 个非重叠样本会**全部换掉**（实测重叠 0/31），桶成分因此每日无故抖动。新实现 `anchored_non_overlapping` 用绝对工作日序数（`business_day_ordinal`，以周一为纪元）取相位，再按 session 顺序强制 `>= horizon + 1` 间距。**必须分清两层**：`candidate_phase_window_independent: true` 指候选集（哪些日期落在格点上）只由日历决定，与窗口起点无关；`final_membership_window_independent: false` 指最终保留成员**不保证**窗口无关——贪心间距过滤从左边界重启，前段某个候选被接纳或丢弃会沿链改变后续候选。这个 `false` 是**保守披露**，不是实测频率：在接近真实交易所密度（约 4% session 休市）的夹具上，重叠区成员完全不变；只有当缺失密集到约 11%（稀疏/缺口数据）时，才观测到成员既进入又退出（即级联确实可传播）。另一侧的对照是旧实现：滑动 1 天即替换全部 31 个样本（重叠 0/31）。因此正确表述是“候选相位严格稳定、最终成员不保证稳定”，既不写“零漂移”，也不声称成员一定变动。工作日步长取 `horizon + 2` 而非 `horizon + 1`，因为单个交易日假期会让 `horizon + 1` 个工作日只剩 `horizon` 个 session，迫使候选被丢弃并把样本砍掉约四成。

实测代价与收益（INTC 5D，250 根）：成熟非重叠样本 31 → 27；桶样本 9–11 波动 → 恒 9；窗口滑动 0–11 天的概率标准差 0.0633 → 0.0495（−22%）。

**选择段不使用锚定相位，并披露原因。** 5D 的选择段恰为 60 个 session，需要 10 个标签、间距 6，密度上限正好等于下限，没有余量；改用锚定相位只得 8 个标签，会低于 `MINIMUM_SELECTION_LABELS` 并让整条路径弃权。因此选择段保留旧间距，`validation.sampling.selection` 记录 `candidate_phase_window_independent: false` 与 `reason: selection_segment_density_ceiling_equals_label_floor`；定义桶成分的两个账本（校准、留出）用锚定相位，`bucket_defining_segments_candidate_phase_window_independent` 单独披露。**这意味因子选择仍在随窗口漂移**，是当前已知的不稳定来源，不是一个已解决的问题。

**候选设计是显式选项，不是默认。** `SELECTION_PROFILES` 只有两个：`legacy`（5 因子、每族 2、间距点估计 IC，生产默认）与 `restricted`（3 因子、每族 1、依赖感知 IC + 方向稳定性）。`bucket_mode` 为 `three`（三分位，生产默认）或 `two`（中位数二分）。传 `None` 必为生产；候选从不隐式启用。双桶把每桶观察数翻倍（9 → 13），但那只是方差取舍，是否更好是登记在案的经验问题，不是结论。

**预登记对照结果。** `scripts/research_b_ab.py` 在 36 标的注册池上比较四条臂（同一冻结行情、同一锚定账本、同一资格门槛）。Q1 选择规则、Q2 分桶模式的配对 Brier 差分别为 +0.0009 / +0.0009（正=候选更差），但只有 **11 个可评分 session**，低于 session 块自助法所需的 24 个，两问均判 `indeterminate`。按登记规则不切换，生产保持 legacy + three。

**OOS 统计的时间单位已修正。** `probabilistic_scores` 现在接收真实 session 索引，并按实际间距决定依赖单位：若相邻预测间隔大于 `horizon`（即已非重叠取样），块长降为 1 条记录并披露 `block_length_basis: one_record_because_labels_do_not_overlap`；只有逐日重叠预测才用 `horizon + 1`。旧实现把过滤后的列表序号当索引，且对已抽稀的序列套用 `horizon + 1` 块，等价于把 `(horizon+1)^2` 个 session 当作一个依赖块。

**`calibration_slope` 只是诊断。** 它由标签对 `logit(p)` 的最小二乘拟合得到，输出新增 `calibration_slope_method` 与 `calibration_slope_is_logistic_calibration_slope: false`，不得表述为经 Logistic 校准验证的斜率。

**52 周下 B 的可达性（实测）。** 5D 可以给出概率（27 成熟 / 9 单桶 / 11 验证预测，门槛 12/6/8）；20D 只有 8 个成熟非重叠标签，达不到 12，按规则弃权，由 A/C 正常出票。**不允许**通过延长窗口、降低门槛或重新定义独立样本让 B 总有概率。

## C — 逻辑回归与防泄漏验证

使用路径内独立 OHLCV 特征和确定性 L2 Logistic Regression。验证不再把 fold 模型的历史行当作生产证据：生产入口与历史回放共用同一个 `predict_at_origin()`。每次预测在绝对 20-session 锚点（`(origin // 20) * 20`）用当日已成熟标签重拟合，训练决策最多回看 252 个 session（52 周上限），预测特征只读到决策日收盘；回放只评分间隔 H+1 的非重叠决策，每一行都是生产入口在同一输入前缀下会给出的概率。没有冻结的 pre-holdout 映射、没有最终 holdout 迁移步骤；当前概率就是同一函数在 `n-1` 的输出。参数固定为 `LOGISTIC_ITERATIONS=500`、`LOGISTIC_LEARNING_RATE=0.12`、`LOGISTIC_L2=1e-3`、`PLATT_MIN_OBSERVATIONS=40`、`REFIT_SESSIONS=20`、`TRAINING_WINDOW_SESSIONS=252`、`MINIMUM_TRAINING_ROWS=60`，特征集 `full_v1`（9 维，60-session 预热），版本 `ml_params_v2`；改动任一数值必须同时升版本号，否则前向台账会把不兼容的预测当成同一模型。
核对 `validation.production_replay`、`probability_calibration`、`calibration_capacity`、`current_prediction_calibration`、`current_prediction_model_training_boundary`、`oos_forecasts` 和 leakage 声明。52 周窗口扣掉 60-session 预热后只剩约 18～21 个非重叠、已成熟的回放预测，小于 Platt 所需的 40 个，因此真实快照通常输出 raw logistic 概率并标 `partial`；`calibration_status=capacity_insufficient`，校准器只写入 `shadow_calibration`/`current_prediction_calibration`、不参与发布概率。更长的归档（例如 640 根日线）可积累 40 个以上 matured 非重叠预测并拟合 Platt，此时才发布校准值。账本每行保留 `refit_anchor_session`、`training_label_end_session`、`calibration_label_end_session` 与系数；训练标签必须在锚点已成熟，校准标签边界必须严格早于决策日，更晚拟合的映射绝不应用到更早的行。没有校准时保留 raw logistic probability 并明确标 `uncalibrated`、`partial`，不能称为校准概率；残留区间重叠时为 `invalid`，概率置空。若要声明 `strong`/`high` 的 OOS 证据，先看 `calibration_capacity.maximum_non_overlapping_in_window` 与 `production_replay.non_overlapping_predictions`，不能把重叠日行数当独立样本。

两条预登记候选（A 收缩、C compact 特征集）用 `scripts/research_candidates.py --snapshot-dir <dir>` 在同一批决策日上做配对 Brier 差、移动块区间、覆盖率与预登记阈值判断。脚本从不改生产配置，`switch` 恒为 false；`candidate_supported` 也只代表可以进入下一轮前向确认，不是上线结论。样本少于 120 个不同评分 session 时一律 `indeterminate`。


当前仓库没有 `_xgb_estimator.py`，也没有嵌套时序早停实现；Research C 只运行标准库确定性 L2 Logistic Regression。`model_selection` 将 XGBoost/LightGBM 标为 `not_implemented_not_loaded`，不会探测、导入、安装或重试这些可选依赖。校准容量不足（52 周窗口的常态）时 C 标为 `partial`；只有在足够长的归档上拟合成功并应用校准后才是 `success`。不得把基线称为完整 boosted-model 验证，也不得把交叉验证 AUC 单独作为方向结论。要引入 boosted 模型，必须先实现嵌套训练/早停、逐折 Purge/Embargo、隔离最终 holdout，并在独立 OOS 与性能验收通过前保持关闭。

`mode=standard/strict` 不改变预测资格；A/B/C 在各自预定义样本/计算条件满足时生成概率。严格模式不会因 OOS 数不足或未校准而排除已成功生成的路径，缺口降低 confidence 并使报告 `DEGRADED`；显式时间泄漏、无效目标或预测失败仍不可报告。`forecast_eligible` 是路径预测资格，`reporting_status` 说明最终展示状态。

## D — 时间序列因子 IC / forward-return 诊断

D 自行构造固定因子，按时间 70/30 划分训练和 holdout，分位切点只用训练数据确定，评估 1D/5D/20D 非重叠 forward-return。查看 `validation.factor_metrics` 中各 horizon 的训练/holdout 样本和 IC；当前可用 holdout IC 诊断门槛是 20 个观察，不足的数值仅作描述并披露状态。

`result_role: diagnostic`，`prob_up` 和 `expected_return` 不作为预测输出。单证券输出不能称为横截面 Rank IC、长短组合、基准相对 NAV 或真实交易回测。完整横截面路径需多 ticker 因子面板、PIT universe、交易日历、复权/可交易价格、安全掩码和基准；当前未实现该引擎，即使用户提供这些输入也不能宣称本地代码已完成完整回测。

## 共识与 E — 偏差审计

共识只纳入通过 `forecast_assessment()`、身份匹配、有定量来源且状态为 `success/partial` 的 A/B/C 数值预测，当前采用等权概率平均，报告 range、dispersion、证据家族重叠和分类 confidence。新增 `oos_probability_quality` 报告每路径证据审计、当前合格路径交集、全候选路径交集和共同完整预测样本的 Brier、Log Loss、可靠性分箱、ECE、覆盖率及弃权率；各路径的 OOS 记录同时披露 `prediction_days`、保守的 `non_overlapping_prediction_count` 与 `effective_sample_size_estimate`，阈值使用非重叠计数。冻结行情验证决策 session、精确 H-session 标签、目标真值、目标定义、标签成熟度和预测时训练标签边界；无效样本不进入质量或权重指标。`algorithm_policy_replay` 按每个历史日期实际有有效预测的路径做等权均值，并与相同日期上的因果扩展基率比较，披露 coverage、`active_path_mask`、Brier Skill、保守的非重叠预测日计数及 moving-block bootstrap 百分位区间（阈值使用非重叠计数；独立块少于 4 时区间标记为不可估计、不据此判分）。它是历史策略诊断，不因今天的活跃路径集合筛选过去日期，也不构成真实独立市场 OOS 验收。`ensemble_calibration` 在预测日前 60% 上拟合确定性 Platt、在净化后的后段做回顾性检查；检查段与各路径既有评估窗口存在重叠，故该检查是描述性、非独立确认。它只写入 `shadow_calibration`（`applied: false`），不改变发布概率：`calibrated_probability` 保持 null，reportable probability 仍是 raw 等权均值，`calibration_status` 标 `ensemble_calibration_shadow_only`。只有未来积累真正前向结果并冻结映射后，才允许另开版本启用。新增 `skill_weight_validation` 仅做严格先早后晚（评价段从最后估计标签成熟后开始）的实验性 Brier Skill 权重估计/评价；当前 `weighting` 仍为 equal，不能把实验权重用于当前预测。等权均值不是校准 ensemble；上述回顾性检查也不足以启用校准，只能写入 shadow，须等真实前向证据与冻结映射后再另开版本。`confidence_score` 是启发式研究支持度，不是预测准确率；`predictive_evidence_grade` 单独呈现基准技能点估计和证据限制。共识还输出三块割裂披露，任何一块都不得被宿主编排合并或省略：`forecast_feasibility` 给出每条路径在本题窗口下的非重叠容量上限与既有门槛（容量不足时路径必须弃权，不得降门槛）；`timing` 记录决策所用确认收盘、`label_basis=close_to_close` 与 `entry_basis=not_modelled`（`first_executable_at` 留空而不是猜下一交易日）；`eligibility` 把概率可报告、方向可报告与交易信号拆成三个布尔，交易信号在入场、成本与独立 edge 齐备前恒为 `false`。`ensemble_uncertainty` 在路径共享标签、`effective_independent_paths < 2` 时给 `not_estimable`，其余情况只给描述性 `path_probability_range` 与 `interval: null`，不得伪造概率置信区间；`path_dependency` 用共同日期上 `probability - target` 残差的相关性估计有效独立路径数。单路径称为 `single_path`；两路径结果披露缺失路径。详见 [consensus-protocol.md](consensus-protocol.md) 与 [probability-policy.md](probability-policy.md)。

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
