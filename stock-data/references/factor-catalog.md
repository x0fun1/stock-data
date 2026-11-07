# 因子目录

实现位于 `features/registry.py`、`features/price.py`、`features/financial.py`、`features/macro.py`。所有窗口、方向和变换在实验前登记；目录是有限候选，不是自动全入模。

## 趋势与反转

| 因子 | 算法 | 说明 |
|---|---|---|
| `mom_60` | `log(J_t/J_(t-60))` | 60 个有效交易日总收益动量 |
| `mom_12_1` | `log(J_(t-21)/J_(t-252))` | 跳月长期动量 |
| `trend_diff` | `log(EMA20(J)/EMA60(J))/sigma60` | 无量纲趋势状态 |
| `residual_reversal` | `-sum(eps_(t-4:t))/(std60(eps)*sqrt(5))` | 只用过去数据做个股对基准回归 |
| `relative_strength` | 60 日个股累计总收益减基准累计总收益 | 与残差动量相关时二选一 |

## 波动、回撤与流动性

| 因子 | 算法 |
|---|---|
| `rv_20` | `std(r[-20:], ddof=1) * sqrt(A)` |
| `vol_change` | `sigma20/sigma60 - 1` |
| `downside_vol_20` | `sqrt(A*mean(min(R,0)^2))`，20 日 |
| `dd_60` | `J_t/max(J[-60:]) - 1` |
| `max_dd_60` | 60 日窗口内运行峰值最大回撤 |
| `abnormal_volume` | `log(V_basis,t/median(V_basis,t-20:t-1))`，先统一拆股 |
| `amihud_20` | `mean20(abs(R_t)/Amount_t)`，Amount>0 |
| `beta_252` | 过去最多 252 日个股对基准回归斜率，最低 120 日 |
| `idio_vol_252` | 上述回归残差标准差年化 |

`A` 为市场年化交易日数（默认 252）。年化只用于显示和统一尺度，不从单日概率推导多期概率。

## 价值、质量与增长

| 因子 | 算法 | 约束 |
|---|---|---|
| `size_log` | `log(raw_close_asof * known_reported_shares * split_factor)` | 单股时点股数；非EPS加权股数；PIT/行动未核验时仅估计 |
| `bp` | `ShareholderEquity_asof / MarketCap_asof` | 保留负权益原值，并检查资本结构口径 |
| `ep_ttm` | `NetIncome_TTM / MarketCap_asof` | 保留负盈利 |
| `fcf_yield_ttm` | `(CFO_TTM - |Capex|)/MarketCap_asof` | Capex 符号统一 |
| `roa_ttm` | `NI_TTM / avg(Assets_now, Assets_1y_ago)` | 使用历史可知报表 |
| `roe_ttm` | `NI_TTM / avg(Equity_now, Equity_1y_ago)` | 平均权益非正时缺失 |
| `asset_growth_yoy` | `Assets_now / Assets_1y_ago - 1` | 低增长为等权基准的候选假设；先检验 |
| `accruals_ttm` | `-(NI_TTM - CFO_TTM)/avg_assets` | 高值表示现金支持较强 |
| `gross_margin_ttm` | `GrossProfit_TTM/Revenue_TTM` | 银行/保险缺失 |
| `revenue_growth_yoy` | `Revenue_TTM/Revenue_TTM_1yago - 1` | 基数非正缺失 |
| `profit_improvement_yoy` | `(NI_TTM - NI_TTM_1yago)/avg_assets` | 处理盈亏切换 |
| `leverage` | `DisclosedDebtScope/Assets` | 优先明确披露总额；长期债须含流动部分，并保留实际口径 |
| `net_debt_ratio` | `(DisclosedDebtScope-Cash)/Assets` | 可为负；检查债务、现金、资产期间与单位一致 |
| `interest_coverage` | `OperatingIncome_TTM/InterestExpense_TTM` | 分母<=0 缺失 |
| `share_change_yoy` | `Shares_now/(Shares_1yago*q)-1` | 先剔除拆股 |

市值由截止时原始收盘价和当时已知股数重建，不能把今天市值套过去。

债务输出保留`debt_scope`。例如NVDA当前可用的是长期债含流动部分，不代表覆盖所有短期借款；`LongTermDebt`不能重复加流动部分。没有明确总额时，只用同期间/单位的`LongTermDebtNoncurrent + LongTermDebtCurrent`重建，缺项返回缺失；不使用通用`DebtCurrent`或资产标签`DebtSecuritiesCurrent`代替。

TTM优先采用同concept/单位的已知年度＋本年累计−去年同期累计；保留连续四季合计及差额。尚无本年年报时，也可从本年Q1/Q2/Q3累计拆季度。52/53周财年同比在周年附近匹配对应期间，缺同期就返回缺失；旧利息期间不能与新营业利润配比。

## 宏观

- `curve_10y_2y`：10Y - 2Y，两者须在 cutoff 前已发布。
- `cpi_yoy`：最新 CPI 与约 12 个月前同序列比较。
- `fx_20d`：20 个观测的 log 变化。
- `policy_change_20d`：20 个观测的利率变化。

宏观按发布批次更新，不把一次发布复制成许多独立日观测。

默认利差用`DGS10/DGS2`、CPI用`CPIAUCSL`，汇率和政策利率保留`FX/RATE`占位符，需明确选择实际序列。`policies/defaults.json`可配置`"macro_series_map": {"policy_change_20d": ["DFEDTARU"]}`（联邦基金目标区间上限）；单序列也可用字符串，两条利差序列须用列表。选定系列、单位、时间口径应在实验协议中冻结。当前库存没有汇率系列，已有FRED系列也缺精确公开时刻和历史vintage验收；报告分别显示这些缺口。

## 显示指标

`rsi_14`、`macd_12_26_9`、`bollinger_position_20`、`atr_14_pct` 只用于解释，不默认与同义趋势/波动因子重复计票。显示指标与模型特征在 `FeatureObservation.display_only` 分开。

## 清洗与标准化

- 训练段拟合 winsor 分位（默认 1%/99%）、median/MAD 标准化、裁剪 [-3,3]。
- MAD 为 0 时依次回退 IQR、标准差；仍为 0 则删列。
- 缺失不填 0：训练内中位数填充 + 缺失指示；严重缺失列删除或暂停模型。
- 相关矩阵只在训练段计算；默认 |rho| 告警线 0.8 是工程候选，不是自然定律。

原生截面、固定家族等权、条件风险处理和LightGBM研究的完整执行规则见[多因子工作流](multifactor-workflow.md)。当前33项因子不是33个独立alpha，也不在数据不足时自动全部入模。
