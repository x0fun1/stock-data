# Stock Research

可独立安装的美股/港股消息研究 skill：采集新闻、宏观消息和公司消息，逐条核对原文证据，解读方向与影响强度，汇总成带引用的报告。仓库根目录就是完整 skill，入口为 [`SKILL.md`](SKILL.md)，执行代码位于 [`scripts/`](scripts/)。

本仓库不含 K 线/量价模型、多因子、概率模型或模型权重，不输出价格预测或上涨概率。

## 安装

需要 Python 3.12+。安装器在目标 Skill 的独立环境中安装 `requests`、DDGS 和 Trafilatura，同时提供本地 Search / Fetch 应用，无须 Docker。JSON/CSV 导入、压缩 JSON 存储和 SQLite 索引使用 Python 标准库。克隆仓库后，从仓库根目录运行：

```bash
python scripts/install_skill.py --destination ~/.claude/skills/stock-research
```

安装器复制 skill、创建目标内独立 `.venv`、安装依赖，并自检审核导入、报告生成、DDGS 导入和 Trafilatura 正文抽取。输出包含本地服务启动命令；安装后启动 `news-local-serve` 即可使用。相同轻量版本的已有安装用 `--update` 更新。旧量化版本应先用旧环境执行 `scripts/migrate_runtime.py --root MY-DATA`，然后安装到新的空目录；普通更新不会替使用者删除旧模型文件或重型环境。

交给安装助手操作时，直接复制[Skill 安装 Prompt](references/install-prompt.md)，其中包含本地应用部署、32 并发、代理兼容和联网验证步骤。

## 研究入口

从仓库根目录或安装后的 skill 目录运行，`MY-DATA` 为运行数据根；默认保存在该 skill 目录内的 `runtime/`：

```bash
python scripts/skill.py doctor --root MY-DATA
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step prepare
python scripts/skill.py research --root MY-DATA --market US --ticker NVDA
```

完整流程是：准备标题 → 程序采集原文证据 → 逐条解读并写回 → 生成报告。新闻综合判断是可审计的研究分数，不是上涨概率；通过功能测试不代表消息解读对未来价格有预测力。

新闻原文采集默认使用本地 Search / Fetch，标题、搜索和抓取并发均为 **32**。从安装后的 Skill 目录，在一个终端启动服务，在另一个终端运行采集：

```bash
python scripts/skill.py news-local-serve
# 另一个终端：
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id LOCAL-ID
```

服务没有固定搜索等待，先抓取已有新闻 URL，失败后搜索原标题。可用 `--workers 16` 降低标题并发，服务上限通过 `--search-workers` / `--fetch-workers` 调整；其他 provider 默认串行。上游引擎与网站的限流、付费墙和 JavaScript 页面仍可能导致访问缺口。部署、接口与 SearXNG 配置见[本地 Search / Fetch](references/local-search-fetch.md)。

每次运行前自动清理最后写入超过 **7 天**的旧报告、抓取缓存、数据快照、工作流和临时文件，并同步清理失效索引。只保留一份清理回执，保护正在执行的任务。用 `python scripts/skill.py cleanup --root MY-DATA --dry-run` 可预览；去掉 `--dry-run` 立即执行。详见[七天缓存期限](references/cache-retention.md)。

提交前运行 `python scripts/check_repository.py` 检查实际 Git 暂存区。共享 `.gitignore` 排除本地数据；检查器还会拒绝被强制暂存的本地文件。安装器与便携包（`scripts/build_bundle.py`）使用同一份文件选择规则。

## 文档

- [Skill 使用说明](SKILL.md)
- [Skill 安装 Prompt](references/install-prompt.md)
- [新闻证据、七档与影响综合](references/news-evidence-and-grades.md)
- [本地 Search / Fetch 部署](references/local-search-fetch.md)
- [七天运行缓存清理](references/cache-retention.md)
- [来源政策](references/scope-and-source-policy.md)
- [报告结构](references/publication-and-report.md)
- [存储与复现](references/operations-and-reproducibility.md)

本地 `docs/` 和 `runtime/` 保留规划、调研和运行数据，不随源码发布。
