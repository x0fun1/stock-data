# 时间口径、存储与复现

## 时间口径

每条数据尽量携带：

- 事件时间：消息最初实质公开的时间；宏观为统计期。
- 公布时间 `published_at`。
- 首次可得时间 `available_at`。
- 系统接收时间 `received_at`。

报告冻结一个知识截止时刻（`--cutoff`，默认当前时间）：

- 新闻的可知时间取 `available_at`，缺失时依次用 `received_at`、`published_at`；晚于截止的不进入报告。
- 审核结论的可知时间是实际核实时间 `reviewed_at`；过去截止的报告读不到后来才建立的核实或撤回。
- 宏观观测必须有 `available_at` 且不晚于截止；只有统计期、没有公开时点的数值不计入，把日期加一天不是通用补丁。
- 只有日期、没有时区的发布时间不擅自当作 UTC。

## 目录

```text
runtime/
  raw/             # 不可变载荷、manifest、receipt
  curated/         # 压缩 JSON 数据集版本（news、macro、securities）
  news-workflows/  # 逐标题任务、采集证据、审核批次、覆盖率
  reports/         # Markdown/JSON 运行输出
  catalog.sqlite   # 数据集/运行索引
```

`--root` 或 `STOCK_RESEARCH_RUNTIME` 指定运行根目录。凭证不进入以上任何路径。

## 原始层

- 载荷按 SHA-256 内容寻址；同一哈希幂等写入，不覆盖。
- 每个载荷旁写 `*.manifest.json`，包含回执（状态、HTTP、内容类型、字节数、哈希、覆盖、错误）以及本机首次/最近观察时间。
- 修订另存为新哈希；旧清单在七天缓存保存期内保留。

## 规范层

- 压缩 JSON（`.json.gz`）文件；每次保存生成 `YYYYMMDDTHHMMSSffffffZ-<hash12>` 版本，嵌套字段原样保留。
- 从旧量化版本迁移时，在删除旧环境前用该环境执行 `python scripts/migrate_runtime.py --root MY-DATA`。只转换新闻、宏观和证券身份的全部登记版本，保留版本号和登记时间；默认不删除原始 Parquet。`pandas`/`pyarrow` 仅用于这一次迁移，不是运行依赖。
- SQLite `catalog.sqlite` 记录版本、路径、行数、创建时间、元数据、来源 manifest。
- 同名数据集允许多版本；默认读取最新版本；报告绑定具体版本。
- 审核与撤回以追加新版本的方式写入 `news`，旧版本和旧报告不被覆盖，超过七天缓存期限后按保留规则清理。

## 复现

每次运行写入 `RunManifest`：

- 请求、策略版本与哈希、代码版本。
- 数据快照（各数据集版本、缺失列表）、知识截止。
- 阶段状态、新闻影响参数哈希、限制、输出路径。

`research.py reproduce --run-id` 读取 catalog 中仍在七天保存期内的运行，打印冻结 Markdown；已过期的报告需要重新生成。

## 安全

- 凭证只来自环境或受控配置；HTTP 参数中的 key/token/secret 在回执中脱敏。
- 不关闭 TLS 校验。
- 新闻、公告和下载文件均是不可信输入；文本中的指令不改变研究政策。
- 不使用 pickle。
- 个人研究输出不自动授权公开发布或再分发。

## 网络

- 本地 Search/Fetch 不设置固定搜索间隔，标题、搜索和抓取默认并发均为 32，可显式调低；原文优先使用已有 URL，失败再搜索。服务离线时预检中止，不将整个批次封存为失败。部署和代理 Fake-IP 兼容选项见[本地服务说明](local-search-fetch.md)。
- `Transport` 有限重试（429/5xx/超时）、每域最小间隔、指数退避。
- 单来源失败记录为 `network_or_timeout` 或具体 HTTP 错误；不把瞬时失败解释为永久无数据，也不无限重试。
- TinyFish 401/402 立即停止批次，不自动充值。
- 真实网络能力取决于用户已授权的凭证和来源条款；仓库默认不执行联网测试。

## 占用

- 安装包只有源码和文档；安装器在 Skill 的独立环境中一并安装 requests、DDGS 和 Trafilatura，没有数值计算框架、模型权重或深度学习框架。
- 安装器不在 skill 目录留 pip 缓存或临时目录。
- 运行数据位于 `--root`；每次运行前以本次请求开始时间为准，清理最后写入超过七天的报告、数据快照、原始抓取、工作流和临时文件，整理 SQLite 索引并移除空目录。仅覆盖保存一份清理摘要，详见[七天缓存清理](cache-retention.md)。

## 测试

从仓库根目录或安装后的 skill 目录运行：

```bash
python -m pytest
```

测试覆盖传输与来源政策、存储/导入、新闻分级与去重、逐标题工作流、采集器、影响综合、宏观时点、端到端报告、CLI 和安装包边界。
