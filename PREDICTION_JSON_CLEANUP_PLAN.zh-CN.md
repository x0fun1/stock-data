# 预测结果输出后的 JSON 清理实施方案

日期：2026-10-11。状态：**首版已实施；220 项自动测试中 219 项通过、1 项因本机拒绝创建符号链接而跳过**。代码质量复核修复了失效清理锁的并发抢占问题，确保运行归属验证前不移除失效 pipeline 标记，并拒绝覆盖内容不符的既有清理锁文件。Skill 结构校验器因运行环境缺少 PyYAML 未能启动；已另行检查 frontmatter 与本地引用。适用范围：`stock-data` Skill 的完整预测流程（包括最终结果为 `complete_with_limitations`、`degraded` 或 `abstain` 的运行）；事实查询和用户明确要求保留研究档案的流程不自动清理。

实现入口：`pipeline --cleanup-json` 创建私有运行目录；最终文本校验后由 `cleanup-run --run-dir ... --analysis-dir ... --response-file ...` 再验证并清理。`cleanup-run --analysis-dir ...` 仅支持旧共享 analysis，`--dry-run` 预览，失败/中断运行需显式 `--force-incomplete`。CLI 不带 `--cleanup-json` 保持旧共享布局；`--keep-json` 显式标注研究保留模式。Snapshot schema 仍为 1.2。实测包括完整 abstain 链路、回复校验、report/freeze 篡改、未知文件、active run、force-incomplete、部分失败重试、幂等、外部输入与共享快照保护；符号链接用例因当前 Windows 环境不允许创建 symlink 而跳过。

## 1. 目标和触发点

在最终预测回复已生成、实际待发送文本已通过 `validate-response`、人可读结果已安全落盘后，清理**本次运行拥有**的过程 JSON。默认保留 `final_response.md`、`report.md` 和一个不含行情/新闻正文的 `cleanup_receipt.txt`。不清理用户提供的请求、网关响应、已有快照、其他分析运行或 Yahoo 缓存。清理不得改变预测值、数据 gate、Quant → 冻结 → News → 综合的顺序。

当前 CLI 无法获知聊天消息是否真正送达。因此自动清理的可验证触发点定义为“最终回复文件写入并校验成功，宿主已读取或复制待发送文本”；若产品需要严格的**用户已收到回复之后**才清理，必须由宿主增加送达回调，再调用同一个清理入口，不能将文件写入伪称为消息送达。

## 2. 当前产物与依赖

现行 `pipeline` 创建 `collections/collection_*.json`；`freeze_snapshot` 创建一个快照目录，含 `manifest.json`、九个规范化域 JSON 和 `raw/` 下九个脱敏原始域 JSON；`run_analysis` 创建四个 `researchers/*.json`、`quant_result.json`、`quant_result.freeze.json`、`news_result.json`、`news_result.freeze.json`、`report.json`、`agent_summary.json`、`report.md`、`final_response.md`。不计宿主自建的请求/响应临时文件，一次完整运行通常生成 **30 个 JSON**。

`run_analysis` 中，News 之前必须冻结 Quant；综合阶段必须重新读取两份冻结结果。`validate_final_response` 还要读取 `quant_result.json`、`news_result.json`、各自的 freeze 凭据和 `report.json`，重算身份、摘要、综合及阶段状态；因此不能在 `analyze` 或普通 `pipeline` 返回前删除这些文件。当前 `pipeline` 成功回执还给出 `report.json` 和 `agent_summary.json` 路径；清理后不能继续把这些路径称为可用产物。

| 类别 | 清理模式下的处理 | 理由 |
|---|---|---|
| 本次专属暂存区内的请求/网关响应 JSON | 清理 | 仅当由 Skill 在本次专属目录创建；外部输入绝不自动删除 |
| 本次 `pipeline` 创建的 collection JSON | 清理 | 已完成冻结、分析和最终文本校验 |
| 本次独占快照的 19 个 JSON | 清理 | 仅限新建的私有快照；共享或外部 `--snapshot-dir` 保留 |
| 本次 analysis 的 4 个 researcher JSON、两份冻结结果及两份 freeze JSON、`report.json`、`agent_summary.json` | 清理 | 校验完成后不再作为交付依赖 |
| `final_response.md`、`report.md`、`cleanup_receipt.txt` | 保留 | 用户回复、可读研究说明、清理状态 |
| 用户输入、其他运行、已存在快照、缓存及非 JSON 文件 | 不触碰 | 所有权不明确，或不属于本功能 |

清理后 `validate-response` 无法再重验该次运行；单靠保留 Markdown 和哈希也无法复算源行情或审计研究路径。需要复现、正式评估、纠纷审查或 prediction ledger 的运行必须使用 `--keep-json`/研究档案模式；清理回执不得宣称仍具有完整可复现性。

## 3. 推荐运行契约

1. 新的 Skill 默认预测工作流在 `runtime/runs/<run_id>/` 建立唯一专属目录，容纳本次由 Skill 创建的 `staging/`、`collections/`、`snapshots/`、`research/`。`run_id` 使用安全单级文件名；运行开始记录目录创建成功与各产物的所有权。普通 CLI `request`、`adapt`、`freeze`、`analyze` 和已有 `pipeline` 的默认输出行为先保持兼容。
2. `pipeline` 仍完整产出 JSON 和 Markdown，返回分析目录与状态。Skill 读取 `final_response.md`，把实际待发送文本另存为 UTF-8 Markdown，并调用现有 `validate-response`。只要分析或校验失败，**不自动清理**，保留当次诊断文件，另提供显式手动清理入口。
3. 新增 `cleanup-run --run-dir <本次专属目录> --analysis-dir <本次分析目录> --response-file <已校验的回复> [--dry-run]`。正式清理时在同一进程再次执行 `validate_final_response`，核对分析 ID、快照 ID、回复内容哈希及运行所有权；成功后生成并持久化最小清理回执，再逐个清除允许名单中的 JSON。`--dry-run` 只输出待清理清单、数量和总字节数。
4. Skill 的常规预测调用按“运行 → 读取最终文本 → 校验实际文本 → 清理本次 JSON → 发送同一文本”执行。这里的“输出后”指最终预测文本已写入并成功交给宿主；如果要求聊天实际送达后清理，宿主的送达回调改为触发第 3 步，CLI 本身不猜测送达状态。
5. 清理完成后的机器回执仅返回仍存在的 Markdown 路径、`analysis_id`、`analysis_status`、`cleanup_status`、删除文件数/字节数及失败清单；不得再返回已删除 JSON 的可用路径。现有 `pipeline`/`analyze` 回执保持原状；新清理入口提供独立回执。更新 `schema --kind final` 的产物生命周期说明，不改 Snapshot 1.2 的字段语义。

`analyze --snapshot-dir` 的快照可能被其他运行复用。该模式仅能清理本次 analysis 目录中的 JSON；collection、快照和外部响应不能因为 analysis 完成而被删除。旧版 `runtime/collections`、`runtime/snapshots` 的共享布局没有可靠的运行所有权标记，不能用名称前缀猜测并批量删除；旧分析最多支持明确指定目录的 analysis-only 清理。

## 4. 清理实现及安全边界

- 独立模块建议为 `scripts/quant_research/cleanup.py`；CLI 增加 `cleanup-run`，Skill 入口及 `references/snapshot-schema.md`、`references/researcher-contract.md` 说明生命周期。`orchestrator.py` 和 `snapshot.py` 只登记新创建文件，不在计算阶段清理。
- 用固定相对路径允许名单和运行时登记的所有权处理文件；不使用 `*.json` 全局 glob、`runtime` 整树递归删除或按 ticker/时间猜测旧目录。清理前核对每个目标的真实路径仍在本次专属根内，并拒绝符号链接、Windows junction/reparse point、目录类型变化、未知文件和越界路径。共享输出根或传入的外部路径不在可删除集合。
- 在清理期间持有本次运行的独占锁；在 Windows 上处理文件占用时返回明确失败。先完成所有验证、生成最终 Markdown 与回执，再逐文件删除，最后仅移除空的过程子目录。多文件删除并非原子事务，失败必须返回 `partial_cleanup` 和未删除项；不可把部分成功报成全部成功。
- `cleanup_receipt.txt` 只保存运行 ID、分析/快照 ID、最终文本与报告的 SHA-256、模式、清理时间、数量/字节数、状态和相对文件名，不保存价格序列、新闻正文、凭证或绝对外部输入路径。回执应可用于重复调用：已删除的已登记文件可跳过，未登记的新文件不得顺手删除。回执本身不是冻结结果的替代品。
- `--keep-json` 用于研究/审计/调试；`--dry-run` 用于预览；默认 Skill 预测工作流启用清理，直接 CLI 的既有命令不静默变更。失败或中断的运行只在用户/宿主显式调用清理命令时处理；可在后续阶段增加按年龄清理未完成私有运行，但不纳入首版。

## 5. 实施顺序

1. **产物登记与隔离**：为新 `pipeline` 工作流增加专属运行目录和文件所有权清单；所有新建路径保留现有不覆盖与 `safe_output_destination` 约束。旧 CLI 默认行为不变。
2. **清理器与预演**：实现固定允许名单、路径和链接检查、大小/哈希清单、`--dry-run`、独占锁、逐文件状态、可重复清理。先只支持 analysis-only，再支持新私有 `pipeline` 的完整 run-owned 模式。
3. **交付衔接**：把最终文本校验放在清理器入口前置步骤；确保 `complete_with_limitations`、`degraded`、`abstain` 均按相同交付规则处理；修订清理后回执，移除失效 JSON 路径。
4. **Skill 与契约更新**：更新 `stock-data/SKILL.md` 的第 8–9 步、最终交付契约、CLI 帮助、README 和相关 references；建议将 Skill/runtime 次版本递增，Snapshot schema 仍保持 1.2。
5. **验证与灰度**：先以显式 `cleanup-run` 运行合成完整链路和失败场景；保留旧模式回归。确认没有外部路径被删、没有提前删除导致校验失败后，再让 Skill 常规预测工作流默认调用清理。研究/审计运行继续保留 JSON。

## 6. 必须通过的验收

- 完整合成预测在最终文本校验前保留全部冻结 JSON；校验后清理本次独占 JSON，留下两份 Markdown 和成功回执，回执不引用已不存在的 JSON。`degraded`、`abstain` 同样可完成清理。
- 错误标的/概率、被篡改的 `report.json` 或 freeze SHA、缺阶段凭据、回复文本不一致时，清理返回失败，所有 JSON 保留供诊断。
- 外部 request/responses、共享快照、别的 run、用户自建 JSON、Yahoo 缓存和非 JSON 文件逐字节不变；链接/junction 指向运行根外、未知文件、路径穿越、并发清理均被拒绝。
- 模拟中途删除失败后报告 `partial_cleanup`，可重试完成；第二次成功调用幂等；没有把异常吞掉或报告假成功。
- 现有 `python -m unittest discover -s tests` 全部通过，新增生命周期、路径安全和 CLI 契约测试；`compileall` 与 `git diff --check` 通过。验收记录区分“减少本地 JSON 留存”与“市场预测有效性”，后者不因清理功能改变。
