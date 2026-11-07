# 研究与验证

## 实验协议

`ExperimentProtocol` 必须在查看结果之前登记：

- 市场、期限、目标、因子集合。
- 训练/开发/校准/测试日期块。
- 候选模型与超参数网格（默认 L2 逻辑回归）。
- 基线：历史可得上涨率、简单市场风险基线、组间等权。
- 验收政策：最小样本、独立日期、Brier skill CI 下界、校准斜率区间、最大 ECE。
- 随机种子、代码版本、登记时间。

实现：`models/training.py`、`datasets/splits.py`、`validation/evaluation.py`。

## 时间分区

- 所有同日证券进入同一时间分区；禁止随机拆行。
- 训练标签必须在开发块开始前成熟，减 embargo；开发标签在 calibration 开始前；校准标签在 test 开始前。
- test 标签必须在 test 窗口内完成，否则不评估。
- `assign_partition` 只按实际日期和标签结束日判定；`validate_partitions` 拒绝边界违规。
- 纯向前 walk-forward 仍需要 purge；使用后段数据的诊断还需 embargo。

## 训练流程

1. 训练段拟合 `FeatureTransformer`（winsor、median/MAD、缺失指示）。
2. 每个 L2 候选在训练段拟合，在开发段选 Brier 最低；记录全部候选。
3. 用选定 L2 在训练+开发重拟合；变换重新拟合于训练+开发。
4. 校准段单独拟合 Platt sigmoid；校准数据不参与选参。
5. 测试段只预测和评估；预测不得被后续修订覆盖。

## 条件收益分位

`Target.QUANTILE` 使用独立 `QuantileArtifact`：

- 在训练+开发段拟合 10%/50%/90% 分位回归，测试段只评估。
- 评估 pinball loss、10–90% 区间覆盖和宽度；与训练段无条件分位基线比较。
- 证据单独登记为 `target=quantile`；覆盖区间和 pinball skill 下界按政策登记。
- 分位交叉在推理时按排序重排，并记录重排前是否交叉。
- 分位工件与方向概率工件分开注册、激活和暂停。

## 评估指标

| 对象 | 指标 |
|---|---|
| 概率质量 | Brier、log loss、对基准的配对日期块 bootstrap |
| 校准 | 等频分箱频率、ECE、校准截距/斜率 |
| 区分 | ROC-AUC、balanced accuracy、混淆统计 |
| 收益分布 | pinball loss、10–90% 区间覆盖与宽度 |
| 稳定性 | 按年份、分区、最差年份 |
| 有效样本 | 样本数、独立日期、证券数、市场阶段 |

Brier skill = `1 - BS_model/BS_base`。基准使用训练/历史可得上涨率，不使用测试期真实平均上涨率。

## 发布判定

`classify_evidence` 默认规则（工程默认，实验前必须登记）：

1. 样本数或独立日期不足 → `unverified`。
2. skill CI 下界 ≤ `min_brier_skill_ci_low` → `unverified`。
3. 校准斜率超出登记区间或 ECE 超限 → `failed`。
4. 全部通过 → `verified`。

`CapabilityDecision` 按市场/期限/目标/证券独立判定。只有 `verified` 才发布概率；其他状态保留描述、基准和缺口。

## 防泄漏检查

- 加未来财务/宏观修订不改变旧 cutoff 特征。
- 加未来拆股不改变已封存过去特征。
- 修改测试数据不改变训练变换、选参和聚类。
- 训练标签不得跨入后续分区。
- 缺失开盘/停牌不补零、不顺延。
- 现货/当前期权/新闻不得进入历史特征，除非有合格历史时点。
- 极端真实收益不被清洗阈值自动删除。

## 失败即结果

“没有发现稳定预测优势”是允许且有用的结论。不能因模型失败而改用新闻主观判断填一个概率，也不能把候选模型数值作为默认报告结论。
