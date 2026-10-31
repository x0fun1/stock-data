# 20 日综合动量特征（momentum_20d_v1）

本文件定义 `momentum_20d_v1` 的公式、配置、失败语义与验收要求。它是**特征模型**，
不是概率模型，也不是对原 ROC 基线的替代。

> **v2 变更（2026-10-26）**：ROC20 的符号不再进入生产概率。`quant_drift_shrinkage_v2`
> 只把因果基率向波动率标定的漂移先验收缩；ROC20 仍被计算、冻结并用于同状态匹配样本的
> **披露**（`matching_origins`、`wins`、`observations`），但 `probability_transform.feature`
> 明确记录 `used_for_probability = false`，审计会拒绝任何声称相反的记录。原因：15 条真实
> 序列的实测显示，把约 68% 权重压在 ROC20 符号上会让 Brier 技能稳定为负（15/15，均值
> −1.00%），其损失与解析噪声惩罚吻合，属结构性损失而非运气。详见
> [probability-policy.md](probability-policy.md)。

## 1. 定位与边界

- 综合动量评分 `Score20` 取值 `-100 ~ +100`，只描述近期价格行为的风险调整强度。
- `Score20` **不得**解释为未来上涨概率，也不得据此推导概率（`+65 → 65%` 是禁止的）。
- 概率始终来自唯一生产路径 A 的固定收缩估计；B/C 是影子研究，D 是描述性诊断。本特征不进入
  `prob_up`、不进入路径权重、不进入共识、不改变任何资格字段。
- 计划明确禁止把 `Score20` 当作新的历史样本筛选条件：要求历史样本与当前得分"完全
  相同"会使有效样本急剧减少，因此基线仍按 20 日 ROC 的**符号**筛选同号样本。
- 声明基准（benchmark）不会改变任何概率。运行器只把综合动量作为独立诊断输出，不把它作为生产概率的输入。

## 2. 数据规范

| 项目 | 要求 |
|---|---|
| 价格字段 | 优先 `adj_close`；缺失时仅在源声明复权（`adjustment=adjusted`）时使用 `close` |
| 口径溯源 | 输出 `price_basis`（`price_field`、`basis_status`、`declared_adjustment`、`sessions`） |
| 对齐 | 目标与基准按**交易日精确对齐**；缺口不填充、不前向填充 |
| 成交量门槛 | 沿用既有 `market_bars` 预测资格校验 |
| 数值 | 全部价格必须有限且严格为正；否则该因子 `invalid` |

不得把不同复权口径的序列直接比较。`basis_status=unverified` 时追加
`price_basis_unverified` 警告，但序列自身仍保持内部一致。

## 3. 窗口与权重（固定，不可运行时调整）

| 参数 | 默认值 |
|---|---:|
| 动量观察窗口 `momentum_days` | 20 |
| 波动率观察窗口 `volatility_days` | 60 |
| 回归价格点 `regression_price_points` | 21 |
| 跟踪误差窗口 `tracking_error_days` | 60 |
| 年化交易日 | 252 |

权重固定为 `risk_adjusted=0.50`、`regression_trend=0.30`、`relative_strength=0.20`，
和必须为 1。缺失某个因子时**不允许**把剩余权重重新归一化后仍声称使用标准模型。

## 4. 基础量

- `P`：复权收盘价；`r[k] = ln(P[k]/P[k-1])`
- `ROC20 = P[t]/P[t-20] - 1`（绝对方向）
- `L20 = ln(P[t]/P[t-20])`
- `sigma60 = stdev(r[t-59..t], ddof=1)`

`ROC20` 与 `L20` 方向恒一致（对数单调），二者不可作为两个独立因子重复加权。

## 5. 三个因子

### 5.1 风险调整动量（50%）

`A20 = L20 / (sigma60 * sqrt(20))`

`sigma60 <= 1e-6`（日对数收益标准差）判定为退化：该因子 `invalid`，不是 0。

### 5.2 回归趋势动量（30%）

取最近 21 个收盘价，`y[k] = ln(P[t-20+k])`，`k = 0..20`，OLS 拟合
`y = alpha + beta*k`，取斜率 `beta` 与 `R²`：

`T20 = (20 * beta / (sigma60 * sqrt(20))) * R²`

- 时间索引必须递增，不得把上涨算成负斜率。
- `R²` 是历史拟合质量，不是未来概率；乘以 `R²` 是本方案的工程设计，其增量价值
  尚待样本外验证。
- 完全平坦窗口（`syy = 0`）：`beta = 0`、`R²` 未定义，输出
  `r_squared_defined: false` 并追加 `flat_regression_window_r2_undefined`；由于此时
  `sigma60` 通常也为 0，最终仍按退化处理。

### 5.3 相对强度动量（20%）

`Excess20 = L20_stock - L20_benchmark`

`d[k] = r_stock[k] - r_benchmark[k]`，`TE60 = stdev(d[t-59..t], ddof=1)`

`R20 = Excess20 / (TE60 * sqrt(20))`

- 基准必须由请求显式声明（`instrument.benchmark`），见
  [collection-adapter.md](collection-adapter.md)。系统不做板块/指数成分推断。
- 基准不可获取时**不自动替换**为其他标的；该因子 `unavailable`，总分置空。
- `TE60 <= 1e-6` 判定为退化（目标与基准几乎重合，缺乏经济解释）：`invalid`。
- `TE60 < 0.10 * sigma60` 时追加 `benchmark_near_identical_low_economic_signal`。

## 6. 变换与合成

`A_norm = tanh(A20 / scale_A)`，`T_norm`、`R_norm` 同理，第一版
`scale_A = scale_T = scale_R = 1.0`，`calibration_status = "uncalibrated"`。

`Score20 = 100 * (0.50*A_norm + 0.30*T_norm + 0.20*R_norm)`

三项贡献 `100 * weight * normalized`，且严格满足
`Score20 = Contribution_A + Contribution_T + Contribution_R`（代码对贡献求和后作为
得分，浮点恒等式成立）。

`tanh` 是有界压缩，**不等价于** Z-score，也不会自动消除因子间尺度差异。尺度校准
必须只用训练时点之前的数据、必须保存版本化参数、不得用测试集拟合；校准后仍不得
改变 50/30/20 名义权重。

## 7. 失败语义（必须显式）

| 情况 | `score_status` | `momentum_score_20d` |
|---|---|---|
| 三个因子全部有效 | `valid` | 数值 |
| 历史不足 / 缺基准 / 基准缺口 | `unavailable` | `null` |
| 零波动、零跟踪误差、非法价格、回归退化 | `invalid` | `null` |

- 缺分数**不得**伪装成 0 分，也不得由 LLM 根据其他指标猜测。
- `status_reasons` 记录 `因子:原因`，例如 `relative_strength:benchmark_unavailable`。
- 单项 `components[*].status` 独立报告，因此部分可用时仍能看到已算出因子的原始值。

## 8. 绝对方向与综合得分分离

`momentum_direction = sign(ROC20)`（容差 `1e-12`，否则 `flat`），与 `Score20` 独立输出。
两者可以不同，例如 20 日收益为负但相对基准显著跑赢时 `Score20` 可能接近零甚至为正。
系统必须保留这种分歧，不得强制两者一致。

## 9. 诊断：rank IC（描述性，非验证）

`diagnostic_rank_ic` 对每个已成熟决策日计算 `Score20` 与 `ROC20`，再与已实现的
`forward_return_{H}d` 求 Spearman 秩相关，并给出前后两半的稳定性。

- 严格因果：`t` 时点的因子只用 `closes[:t+1]`；标签只用已经成熟的 `t+H`。
- 输出 `scope = in_sample_descriptive_no_hyperparameter_fitting`、
  `incremental_value_claim = not_established`。
- 它**不是**样本外证据，不能据此宣称"优于原算法"。权重未拟合只能说明该诊断未被
  权重拟合污染，不能替代 walk-forward 的独立检验。

## 10. 输出位置

`analysis.json → researchers.quant.validation.momentum_20d`，主要字段：

```text
model_version, as_of, symbol, benchmark{symbol,selection,status,alignment,aligned_sessions},
price_field, price_basis{...}, momentum_direction, return_20d, momentum_score_20d,
score_status, status_reasons, calibration_status, interpretation="feature_not_probability",
weights, windows, normalization, daily_volatility_60d, annual_volatility_60d,
components{risk_adjusted,regression_trend,relative_strength}, diagnostic_rank_ic, warnings
```

`report.md` 用**独立小节**呈现动量，不与上涨概率混排：

```text
- 动量诊断：Score20=<得分>；方向=<momentum_direction>；return_20d=<20日累计收益>；仅描述当期价格路径，不进入概率
```

**v2 起不再有“动量与上涨概率的一致性”固定行。** 该行是 v1 交付契约的一部分，但 v1 的实现
从未生成它（`_momentum_relationship_line` 只是回填 `模型倾向`），而 v2 又取消了 ROC20 对概率
的条件作用——此时再比较“动量方向 vs 概率方向”会把一个不参与估计的量描述成证据。契约与实现
的差异以**删除契约**解决，而不是补一行没有统计含义的判定。

历史说明（仅用于读取旧报告）：v1 的旧规则曾把“动量正且概率≥50%”记为一致、“动量正且概率<50%”
记为背离，并要求背离分支引用条件桶与 Wilson 区间。读取 v1 旧报告时仍可用该规则解释，但新报告
不再产出这一行，也不得手工补写。

## 11. 已知限制与待回测事项

- 尺度参数未校准（`scale = 1`），`tanh` 在极端行情下饱和，分项贡献被压缩到 ±(权重×100)。
- `R²` 乘数、50/30/20 权重、20/60/21/60 窗口均为先验设定。
- 基准只能人工声明；系统无法判断标的属于纳斯达克 100、行业指数或宽基。
- 未实现：尺度校准版本化（C 组）、条件分箱概率模型、换手率/交易成本/回撤的策略级评估。

### 11.1 A/B 对照（已执行，协议 momentum-ab-v1）

原生 `experiments momentum-ab`（历史实现见仓库 `legacy/python/momentum_ab.py`）实现预登记协议，只做评估：不写 `prob_up`、不拟合权重、
不重标定尺度、不改聚合顺序。判定规则在看结果之前写进 `PREREGISTRATION` 并原样进入报告。

- 信号 A = 裸 20 日 ROC（概率路径实际依赖的量）；信号 B = 冻结的 50/30/20 综合分。
- 指标 = 逐 session 的**横截面** Spearman IC，再按 session 取均值。横截面排序消掉共同市场
  因子，这是池化多个标的有意义、而不是把同一次大盘波动重复测量多遍的原因。
- 区间 = 循环块自助法，块长 = 期限 + 1（重叠前瞻收益使相邻 session 相关，按观测独立重采样
  会低估区间）。
- 主判定期限 5D / 10D；20D 只报告不判定（约 10 个独立块不足以支撑该期限的结论）。

**首次运行结果**（36 只标的，含 6 只行业 ETF，各自 52 周 250 session）：

| 期限 | 可用 session | 独立块 | IC(A 裸 ROC20) | IC(B 综合分) | ΔIC | Δ 的 95% 区间 |
| --- | --- | --- | --- | --- | --- | --- |
| 5D | 185 | 30 | +0.0310 | +0.0481 | +0.0171 | [+0.0051, +0.0296] |
| 10D | 180 | 16 | +0.0535 | +0.0731 | +0.0196 | [+0.0017, +0.0389] |
| 20D | 170 | 8 | +0.0538 | +0.0746 | +0.0208 | [−0.0034, +0.0456] |

判定 `composite_better`，但 `durability = pool_dependent`：同一协议在剔除 6 只 ETF 的 30 只
股票子集上，10D 的 Δ 区间为 [−0.0002, +0.0388]，**包含零** → 该子集判定 `indistinguishable`。
因此结论必须写成"在注册池内支持"，不能写成"该特征已被证明更好"。

注意 B 自身的区间（5D [−0.0282, +0.1225]）**含零**：单个信号的绝对 IC 在这个样本量下不可分辨，
配对差值因为共享标签噪声而窄得多（自相关只在 lag 1 为 0.45，单信号为 0.73–0.80）。
判定用的是配对差值，这是本协议能站住的唯一原因。

消融（去掉该因子后重新归一化，Δ = 去掉后的 IC − 完整 IC）：

| 期限 | 去掉风险调整 | 去掉回归趋势 | 去掉相对强度 |
| --- | --- | --- | --- |
| 5D | +0.0470 (Δ −0.0010) | +0.0422 (Δ −0.0059) | +0.0488 (Δ **+0.0007**) |
| 10D | +0.0719 (Δ −0.0012) | +0.0651 (Δ −0.0080) | +0.0761 (Δ **+0.0030**) |
| 20D | +0.0637 (Δ −0.0109) | +0.0724 (Δ −0.0022) | +0.0765 (Δ **+0.0019**) |

**三个期限下去掉相对强度都使 IC 略升**：这 20% 权重没有挣到自己的份额，主要贡献来自回归趋势
（5D/10D）与风险调整（20D）。这是样本内的单次观察、幅度小于配对区间的数量级，不足以据此改权重
（改权重属于拟合，本次协议明确不拟合），但足以说明"三因子都有效"这个直觉不成立。

**没有回答的问题**：C 组（尺度校准）未做，因为 A 与 B 都没有可拟合参数、不存在泄漏，
而 C 需要在训练折上拟合，必须先有版本化尺度与更大样本；本协议不涉及，也不得据此声称
校准有价值。可交易性（换手、成本、回撤）未评估。

- 在上述事项完成前，本模型应表述为"新的综合动量特征模型"，不得声称预测精度提高。

## 12. 验收

`selfcheck.py` F25 覆盖：权重和为 1、贡献加总等于得分、得分严格在 `(-100,100)`、
`tanh` 输出严格在 `(-1,1)`、绝对方向与 ROC 一致、追加未来数据不改变历史时点评分、
缺基准/基准缺口 → `unavailable`、零波动与零跟踪误差 → `invalid`、以及"声明基准不改变
`prob_up`"的隔离断言。F26 覆盖呈现隔离：动量不得出现在 Quant 段内、四种判定分支、
强制行的改写/删除必须被 `validate-response` 拒绝、以及渲染不改变 `prob_up`。
