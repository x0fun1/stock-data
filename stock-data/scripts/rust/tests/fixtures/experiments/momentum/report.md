# 20 日动量 A/B 对照（预登记协议 momentum-ab-v1）

- 池：14 只标的，各自 260 个 session（2025-10-27 → 2026-10-23），基准 SPY
- 判定：**indistinguishable** → `incremental_value_claim = not_established`
- 结论强度：`consistent_across_pool_slices`

## 逐期限结果

| 期限 | 可用 session | 独立块 | IC(A 裸 ROC20) | IC(B 综合分) | ΔIC | Δ 的 95% 区间 |
| --- | --- | --- | --- | --- | --- | --- |
| 5D | 0 | 0 | n/a | n/a | n/a | n/a |
| 10D | 0 | 0 | n/a | n/a | n/a | n/a |
| 20D | 0 | 0 | n/a | n/a | n/a | n/a |

## 单因子消融（去掉该因子后重新归一化）

| 期限 | 去掉风险调整 | 去掉回归趋势 | 去掉相对强度 |
| --- | --- | --- | --- |
| 5D | n/a | n/a | n/a |
| 10D | n/a | n/a | n/a |
| 20D | n/a | n/a | n/a |

## 稳健性披露（不参与判定）

- 仅 11 只股票（剔除 3 只 ETF）按同一协议复算 → **indistinguishable**

| 期限 | ΔIC | Δ 的 95% 区间 |
| --- | --- | --- |
| 5D | n/a | n/a |
| 10D | n/a | n/a |
| 20D | n/a | n/a |
- 若该子集在某个主判定期限上的区间包含零，说明主判定依赖于池子构成。

## 预登记判定规则（结果出来前已固定）

- 指标：`mean_cross_sectional_spearman_ic`，按 session 做横截面排序后取均值
- 主判定期限：[5, 10]；仅报告：[20]
- 自助法：circular_block，块长 = 期限 + 1，2000 次，95%，seed 20261019
- 判定：composite_better: delta_ic > 0 and the 95% paired interval excludes zero on every primary horizon; composite_worse: delta_ic < 0 and the 95% paired interval excludes zero on every primary horizon; otherwise indistinguishable
- 权重：the 50/30/20 weights stay frozen regardless of the outcome; no re-fitting is performed by this protocol
- 说明：20-session results are reported but never decide the verdict: roughly ten independent blocks cannot support a claim at that horizon.

## 解释边界

- 本协议只评估排序信息（IC），不评估校准，也不评估可交易性；没有计入交易成本、换手或回撤。
- 结论是关于**这个标的池**在这个 52 周窗口内的，不是关于任何单只标的的。
- 无论结果如何，`prob_up`、路径权重与聚合顺序都不因本协议改变。
