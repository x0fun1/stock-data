# News Analysis

本文件是新闻消息面流程的权威规则。运行时不预测方向，不输出概率、综合分数或买卖/仓位建议；所有输出是描述性事实、来源分级、逐条 AI 判断与局限。

## 固定窗口与 PIT

- 采集窗口写死为 `(as-of − 60 天, as-of]`；另分别统计最近 30 天与第 31–60 天条数。
- 窗口外条目丢弃并计数；窗口参数不得被静默忽略。
- 所有时间转 UTC ISO；缺失或无法解析的时间戳丢弃并计数。
- 历史 as-of 输出 `pit_status=published_at_only`：只能保证 `published_at <= as-of`，不能重建当时可见性。不得用抓取时间冒充发布时间。

## 来源

| 来源 ID | 入口 | 角色 |
|---|---|---|
| `sec_edgar` | `https://data.sec.gov/submissions/CIK##########.json` | 一手披露；需要 `SEC_USER_AGENT` |
| `ir_rss` | `references/news-sources.json` 登记的 IR 新闻稿 RSS | 一手披露 |
| `finnhub_rest` | `https://finnhub.io/api/v1/company-news` | 第三方新闻；需要 `FINNHUB_API_KEY`，用请求头传递 |
| `finnhub_mcp` | 宿主 `get-news-pulse` | 可选；宿主已连接时合并去重 |

Yahoo Finance 与 The Economist 只作为读者可点击的参考链接，程序不请求、不解析、不存储、不交给 AI 判断，不计入任何计数。

## 状态

| State | Meaning |
|---|---|
| `complete` | 采集并筛选，无证据性警告 |
| `partial` | 采集并筛选，但有覆盖或核实警告；`partial` 是已评估证据 |
| `not_assessed` | 全部来源失败或未配置，或没有 AI 评估 |

`partial` 不得写成“未评估”；`not_assessed` 不得暗示“AI 还没做完”。

## 通用规则

- 去重：标题规范化（去掉尾部“ - 来源”、转小写、去标点）加 URL。一手披露与第三方转载并存时保留一手条目，转载写入 `also_seen_in`。
- 相关性：Finnhub `related` 字段与本地 ticker 和公司名别名匹配；不相关条目丢弃并计数。
- 来源分级：`A_primary`（SEC 与公司 IR）、`B_media`（白名单主流媒体）、`C_aggregator`（聚合与个人站）。C 级不得单独构成“重大”。
- 覆盖披露：每个来源输出实际观测首末时间、条数、是否 `feed_truncated` / `partial_window`。不得把任何来源说成完整覆盖 60 天，除非实测首条早于窗口起点。
- 失败语义：各来源独立 try，一个失败不影响其他；429 标 `rate_limited`，不重试；断网/4xx/5xx 标 `error`，不崩溃，不用旧缓存顶替。
- 只保存标题、时间、来源、链接与摘要字段，不保存正文。

## 逐条 AI 判断

宿主 AI 读取候选后，为每条直接相关候选给出：

- `lean`：`bullish` / `bearish` / `neutral` / `mixed` / `unclear`
- `strength`：`strong` / `medium` / `weak`（规则版本与事件表见 [news-impact-rubric.md](news-impact-rubric.md)）
- `reason`、核实状态与证据链接

无法判断标 `unclear` 并写原因。标题与摘要级判断必须标注，不得声称读过正文。`A_primary` 的发行人新闻稿属于发行人陈述，判断须写明。

## 核实

核实由 `scripts/news_verify.py` 与宿主 AI 共同完成：

| 状态 | 定义 |
|---|---|
| `primary_confirmed` | 存在同一事件的一手披露链接，由宿主 AI 确认并写出依据 |
| `multi_source` | 除原条目来源域名外，至少还有 2 个不同可注册域名的白名单媒体报道同一事件 |
| `single_source` | 只找到原条目来源 |
| `unverified` | 搜索无结果或结果不能确认为同一事件 |

程序只做域名与日期过滤，不做语义判断。核实按域名计独立，无法识别转载同源，这一局限必须写进交付。`verification_pit=post_hoc` 的条目不得进入“强”档。

## 描述性汇总

- 输出 lean × strength 条数矩阵，近 30 天与第 31–60 天分开。
- 每类 lean 下最强至多 3 条，含链接、核实状态与理由。
- 直接相关条目少于 5 条时矩阵照列，但标注“样本过少，不作倾向性描述”，不输出任何整体性措辞。
- 禁止概率、方向结论、综合分数或加权求和。

## 价格反应与波动

价格反应只使用冻结快照中已经走完的历史窗口，描述事件后的实际价格表现；没有成熟窗口写“无成熟观察窗”。`expected_volatility_regime`（`elevated` / `normal`）不含方向。二者都不进入任何分数。

## 来源条款

Finnhub 仅限个人使用、不得再分发；其内容与派生结果只用于用户本人，技能输出不得作为对外分发材料。SEC 要求含联系方式的 UA 并限速；IR RSS 遵守 robots 的 `Crawl-delay`。详见 [source-policies.md](source-policies.md)。
