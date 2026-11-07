# Stock Research

当前主线是可独立安装的美股/港股研究 skill：包内原生 Kronos-small 量价推理、多因子分析、财务与事件描述，以及历史遮蔽研究和报告复现。源码入口位于 [`stock-data/`](stock-data/)，规划、调研及数据渠道审计位于 [`docs/`](docs/)。

## 安装

需要 Python 3.12+；完整安装已在 Windows x64、Python 3.13.7、CPU 环境实测。克隆仓库后，从仓库根目录运行：

```powershell
python stock-data/scripts/install_skill.py --destination "$env:USERPROFILE\.codex\skills\stock-research" --update
```

安装器复制 skill、创建目标内独立 `.venv`、安装依赖，并执行真实 Kronos 推理及 LightGBM/因子处理自检。模型的完整推理及微调源码在 `stock-data/vendor/kronos/`；首次安装从清单指定的官方固定 revision 下载权重并校验 SHA256。已有权重则校验复用。模型在本地运行，不通过 MCP 或在线推理服务调用。

Git 源码仓库不提交大体积权重、账户凭证、采集运行数据或环境缓存。使用已包含权重的便携包时可离线复用权重；`--offline` 只禁止权重下载，首次 Python 依赖安装仍需联网。

## 研究入口

从安装后的 skill 目录运行，`MY-DATA` 为已导入数据的运行根：

```powershell
python scripts/skill.py doctor --root MY-DATA
python scripts/skill.py research --root MY-DATA --market US --ticker NVDA --benchmark SPY
```

日频研究默认执行 Kronos 和多因子分析。33 项候选因子覆盖量价、价值、质量、成长、规模、流动性和宏观；合成研究支持家族等权、Ridge 和本地 LightGBM，独立 FF3 + Momentum 层解释已实现风险。历史研究按共同日期分区、标签成熟时间与封存留出进行，Kronos 增强须另做增量检验。

当前最完整登记数据只有 NVDA 一家公司和 SPY 基准，能够输出因子画像与风险解释；同行排名、中性化和多因子预测权重需要合格的更广历史面板。路径票数、因子历史分位及研究分数都不是已校准的上涨概率。当前源码和独立安装各通过 166 项功能测试，这不代表取得了相同数量的独立投资预测验证。

## 文档

- [Skill 使用说明](stock-data/SKILL.md)
- [原生多因子工作流](stock-data/references/multifactor-workflow.md)
- [多因子架构调研与实现](docs/MULTIFACTOR_ARCHITECTURE_2026-11.md)
- [数据渠道与缺口审计](docs/MULTIFACTOR_DATA_CHANNEL_AUDIT_2026-11.md)
- [Kronos 历史训练方案](docs/KRONOS_TRAINING_PLAN_2026-11.md)

文档中指向 `runtime/` 的文件是本机实验与复现证据，不随源码发布。`stock-data/scripts/rolling_baseline.py` 保留为旧研究档案，不是当前 CLI 入口，也不进入独立安装包。固定上游代码、LICENSE 和公开示例保留原出处。
