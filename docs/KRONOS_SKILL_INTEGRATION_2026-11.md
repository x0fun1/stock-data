# Kronos-small 完整skill封装 · 2026-11-07

用户选定Kronos-small作为固定基准，本轮已把完整模型代码和权重融入stock-research，而非连接外部模型服务。自定义神经训练尚未开始。

## 安装包

[stock-research-kronos-small.zip](../runtime/kronos-skill-integration/stock-research-kronos-small.zip) 约117 MB，含205个源文件及包清单。包含固定commit的官方完整Kronos源码92文件（MIT LICENSE、原生模型、finetune和finetune_csv）、small/tokenizer冻结权重、SHA清单、skill实现、依赖锁、安装器、测试及训练方案。权重与前期实测版本相同，原生4文件只做已登记的行尾格式规范化。

解压后从skill目录执行一条命令：

```powershell
python scripts/install_skill.py --destination "$env:USERPROFILE\.codex\skills\stock-research"
```

安装器复制完整包，验证源代码与权重，创建目标目录内的`.venv`，安装锁定CPU依赖并实际调用模型自检。已安装的目标需要显式`--update`，不能误覆盖其他项目。已有权重内置，无需下载在线模型；依赖首次安装需要访问包源。缺失权重时只按固定官方revision和SHA补齐。训练行情和账户凭证不随包分发。

安装后通过`python scripts/skill.py ...`运行，入口自动切换到本skill的私有Python，移除继承的PYTHONPATH/PYTHONHOME；不会借用其他试验目录的源码、权重或依赖，不需MCP。模型使用包内PyTorch与safetensors直接推理。

## 量化入口

当前`research`对美股默认执行Kronos-small下一交易日开收盘方向基准，报告同时保留价格因素、财务/宏观与新闻上下文、能力发布检查和限制。财务/新闻仍需要合格数据，不由Kronos填造。未验证的模型方向与路径票数单列，不填入投资概率表；HK不会无条件调用US基准。

```powershell
python scripts/skill.py research --ticker NVDA --benchmark SPY --root YOUR_RUNTIME
python scripts/skill.py quant-baseline --file NVDA-hourly.csv --ticker NVDA --frequency 1h --cutoff 2026-11-05T20:00:00Z
```

输入全部截止之前已完成、当时可得的有效OHLCV。小时入口保留UTC开始/结束，只使用完整60分钟常规时段，排除半小时、休市、早收盘尾部和质量缺口，不能经日线契约丢弃小时精度。输入先保存，模型输出再封存，随后才允许揭示/评分；本轮单次入口不主动读取目标真实行情。

Kronos权重冻结，16路径、固定种子规则、不截断历史，超过512训练上下文参考时标记研究压力测试。OHLC排序异常路径如实保存，不据方向成败删样本，不用于极值或风险预测。基准、未来候选权重和已封存预测各自保存。

运行记录包括输入文件、输入/模型哈希、原始路径、预测JSON、Markdown及目录清单。`reproduce`现读取落盘输出路径，修复了此前JSON先写、outputs后赋值的问题。

## 实际验证

- 一条完整安装命令在隔离目录成功，CPU Torch 2.8.0及全部依赖来自私有`.venv`，未改全局Python。
- 同一安装环境的77项skill测试全部通过。
- 包装/日历/安装目录同行核对114项通过。
- 独立真实入口核对5,590项通过：系统Python启动`skill.py`后实际切换私有环境；小时模型吃完整2,764根历史；仅修改未来OHLCV时源文件校验和变化，但模型输入哈希、种子、tokens、路径和方向不变。
- 默认日频报告真实使用463根NVDA日线调用模型；发布门未绕过；`reproduce`返回相同冻结报告。

验证证明安装、遮蔽、调用和报告行为正确，不代表投资预测准确率已提高。旧全量NVDA方向准确率49.31%仍是开发基准记录，没有重新训练后宣称新成绩。

证据：[安装记录](../runtime/kronos-skill-integration/installed-skill/install-receipt.json)、[独立前向检验](../runtime/kronos-skill-integration/skill-forward-validation.json)、[包装检验](../runtime/kronos-skill-integration/peer-review.json)、[测试记录](../runtime/kronos-skill-integration/installed-tests.xml)、[ZIP回执](../runtime/kronos-skill-integration/stock-research-kronos-small.receipt.json)。

后续七姐妹历史遮蔽、微调及训练数据量学习曲线见随skill安装的[训练方案](../stock-data/references/kronos-training-plan.md)。其他五姐妹训练库尚需从既有来源补齐，不需要等待长期未来行情。
