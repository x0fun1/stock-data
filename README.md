# Stock Research

当前主线是可独立安装的美股/港股研究 skill：包内原生 Kronos-small 量价推理、多因子分析、财务与事件描述，以及历史遮蔽研究和报告复现。仓库根目录就是完整 skill，入口为 [`SKILL.md`](SKILL.md)，执行代码位于 [`scripts/`](scripts/)。

## 安装

需要 Python 3.12+；完整安装已在 Windows x64、Python 3.13.7、CPU 环境实测。克隆仓库后，从仓库根目录运行：

```powershell
python scripts/install_skill.py --destination "$env:USERPROFILE\.codex\skills\stock-research" --update
```

安装器复制 skill、创建目标内独立 `.venv`、安装依赖，并执行真实 Kronos 推理及 LightGBM/因子处理自检。模型的完整推理及微调源码在 `vendor/kronos/`；首次安装从清单指定的官方固定 revision 下载权重并校验 SHA256。已有权重则校验复用。模型在本地运行，不通过 MCP 或在线推理服务调用。

GitHub 源码发布只包含当前核心 skill、固定模型源码、依赖与使用/训练方案。旧横向模型比较代码、全部本地生成数据与报告、本地 `docs/`、大体积权重、账户凭证和环境缓存不上传；本地数据保留。安装器和便携包同样排除旧比较及本地运行数据。GitHub 克隆安装时，权重由固定清单指定的官方 revision 获取并校验 SHA256；使用已包含权重的便携包时可离线复用权重。`--offline` 只禁止权重下载，首次 Python 依赖安装仍需联网。

## 研究入口

从仓库根目录或安装后的 skill 目录运行，`MY-DATA` 为已导入数据的运行根；默认运行数据保存在该 skill 目录内的 `runtime/`：

```powershell
python scripts/skill.py doctor --root MY-DATA
python scripts/skill.py research --root MY-DATA --market US --ticker NVDA --benchmark SPY
```

提交前运行 `python scripts/check_repository.py` 检查实际 Git 暂存区。共享 `.gitignore` 排除本地数据与权重；检查器还会拒绝被强制暂存的实验文件和未登记资源。安装器与便携包使用同一份核心文件选择规则，上游文件仅按固定清单分发。

日频研究默认执行 Kronos 和多因子分析。33 项候选因子覆盖量价、价值、质量、成长、规模、流动性和宏观；合成研究支持家族等权、Ridge 和本地 LightGBM，独立 FF3 + Momentum 层解释已实现风险。历史研究按共同日期分区、标签成熟时间与封存留出进行，Kronos 增强须另做增量检验。

数据库存由使用者配置或导入，运行前用 `doctor --root ...` 核验。少量证券能够输出因子画像与风险解释；同行排名、中性化和多因子预测权重需要合格的更广历史面板。路径票数、因子历史分位及研究分数都不是已校准的上涨概率。功能测试覆盖数据处理、模型调用、报告复现和安装包边界；通过功能测试不代表投资预测准确率得到验证。

## 文档

- [Skill 使用说明](SKILL.md)
- [原生多因子工作流](references/multifactor-workflow.md)
- [因子定义与分组](references/factor-catalog.md)
- [Kronos 历史训练方案](references/kronos-training-plan.md)
- [存储与复现](references/operations-and-reproducibility.md)

本地 `docs/` 保留规划、调研和数据渠道审计，不随源码发布。文档中指向 `runtime/` 的文件是本机实验与复现证据。旧实验脚本和上游示例生成的报告、预测图片保留在本地 `runtime/` 档案；Git 本地配置也不随发布或安装包分发。模型、微调与示例代码、LICENSE，以及教程和回归测试所需资源保留原出处，并由模型清单记录实际分发文件和上游来源。
