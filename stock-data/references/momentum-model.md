# 20 日综合动量特征（momentum_20d_v1）

本文件定义 `momentum_20d_v1` 的公式、配置、失败语义与验收要求。它是**特征模型**，
不是概率模型，也不是对原 ROC 基线的替代。

## 1. 定位与边界

- 综合动量评分 `Score20` 取值 `-100 ~ +100`，只描述近期价格行为的风险调整强度。
- `Score20` **不得**解释为未来上涨概率，也不得据此推导概率（`+65 → 65%` 是禁止的）。
- 概率始终来自 A 条件频率、B 因子验证、C Logistic 等既有路径；本特征不进入
  `prob_up`、不进入路径权重、不进入共识、不改变置信度。
- 计划明确禁止把 `Score20` 当作新的历史样本筛选条件：要求历史样本与当前得分"完全
  相同"会使有效样本急剧减少，因此基线仍按 20 日 ROC 的**符号**筛选同号样本。
- 声明基准（benchmark）不会改变任何概率。这一点由 `selfcheck` F25 强制断言：
  带基准与不带基准两次运行必须得到逐位相同的 `prob_up`。

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

`quant_result.json → researchers.quant.validation.momentum_20d`，主要字段：

```text
model_version, as_of, symbol, benchmark{symbol,selection,status,alignment,aligned_sessions},
price_field, price_basis{...}, momentum_direction, return_20d, momentum_score_20d,
score_status, status_reasons, calibration_status, interpretation="feature_not_probability",
weights, windows, normalization, daily_volatility_60d, annual_volatility_60d,
components{risk_adjusted,regression_trend,relative_strength}, diagnostic_rank_ic, warnings
```

`final_response.md` 与 `report.md` 的 Quant 段各增加两行：综合动量得分与分项贡献。
这两行不是 `validate-response` 的强制字段，但属于报告内容，不得改写数值。

## 11. 已知限制与待回测事项

- 尺度参数未校准（`scale = 1`），`tanh` 在极端行情下饱和，分项贡献被压缩到 ±(权重×100)。
- `R²` 乘数、50/30/20 权重、20/60/21/60 窗口均为先验设定，**尚未**经样本外验证。
- 基准只能人工声明；系统无法判断标的属于纳斯达克 100、行业指数或宽基。
- 未实现：尺度校准版本化、条件分箱概率模型、Purged Walk-forward 的 A/B/C 对照、
  换手率/交易成本/回撤的策略级评估。
- 在完成上述样本外验证前，本模型应表述为"新的综合动量特征模型"，不得声称预测精度提高。

## 12. 验收

`selfcheck.py` F25 覆盖：权重和为 1、贡献加总等于得分、得分严格在 `(-100,100)`、
`tanh` 输出严格在 `(-1,1)`、绝对方向与 ROC 一致、追加未来数据不改变历史时点评分、
缺基准/基准缺口 → `unavailable`、零波动与零跟踪误差 → `invalid`、以及"声明基准不改变
`prob_up`"的隔离断言。