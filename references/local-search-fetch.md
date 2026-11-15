# 本地 Search / Fetch 部署

默认采集路径使用本地服务，不经过 TinyFish，无须 TinyFish API Key。搜索由 DDGS 在本机执行，正文由 requests 下载、Trafilatura 抽取；可选接入本地 SearXNG。标题、搜索和抓取默认并发均为 32，不设置一分钟或其他固定搜索间隔。上游搜索引擎与新闻网站仍可能返回 429、403、验证码或付费墙；这些失败保留为访问缺口。

## 原生 Python 部署（无需 Docker）

正常安装器已从 `requirements.txt` 安装完整依赖；部署助手使用[安装 Prompt](install-prompt.md)。在源码环境补装或更新依赖时执行：

```bash
uv pip install --python .venv/bin/python -r requirements.txt
```

没有 uv 的环境，可用 `.venv/bin/python -m pip install -r requirements.txt`。安装包含 requests、DDGS 和 Trafilatura，全部位于 Skill 自己的 `.venv`。若尚未创建环境，先按 README 安装 skill；安装器会实际自检搜索模块导入和 HTML 正文抽取。

在一个终端启动服务，保持此终端运行：

```bash
python scripts/skill.py news-local-serve
```

健康检查：

```bash
curl http://127.0.0.1:8765/health
```

另一个终端运行完整标题批次：

```bash
python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step prepare
python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id LOCAL-ID
```

`news-collect` 默认 provider 是 `local`，本地默认 32 个标题工作线程；服务的搜索和抓取上限同为 32。`--workers`、`--search-workers`、`--fetch-workers` 均支持 1–32；可显式降到 16 或更低。TinyFish 和 archive provider 默认串行。CLI 参数 `--search-url` / `--fetch-url` 或环境变量 `STOCK_SEARCH_URL` / `STOCK_FETCH_URL` 可分别指定两个本地接口。默认接口为 `http://127.0.0.1:8765/search` 和 `/fetch`。`--local-timeout` 默认 45 秒，服务单次上游请求 `--timeout` 默认 15 秒。调整并发时应同时考虑服务端上限，避免排队超过客户端超时。

结束服务在其终端按 Ctrl+C；下次采集前重新启动。不修改系统登录项，不要求连接聊天插件。

如果使用 Tun 代理（例如 Fake-IP DNS），公网域名可能解析成 `198.18.*` / `198.19.*` 合成地址。确认本机采用该模式后，启动时加 `--allow-vpn-fake-ip`：

```bash
python scripts/skill.py news-local-serve --allow-vpn-fake-ip
```

这个选项仅放行公网域名的 `198.18.0.0/15` 合成 DNS 地址；仍拒绝 loopback、普通私网、链路本地地址、局域网主机名及直接使用合成 IP 的 URL。默认不开启。

## 采集顺序与证据

1. 有 Finnhub 或导入文件提供的 `source_url` 时先 Fetch。它是已有链接，不计作新搜索；Finnhub 中转 URL 的最终地址保存为 `resolved_url`。
2. 正文不可用时 Search 原标题，Fetch 最多 `--max-sources` 个未尝试过的候选文章。
3. 仍不可用时 Search 公司、原标题与日期组合，继续 Fetch 新候选。
4. 每条完成即写入 `collections/LOCAL-ID/items/`；最终 `collection.json` 与 `evidence-table.md` 按原标题顺序排列。相同命令断点续跑只处理尚未落盘的条目。已封存的失败需用新 collection ID 重新采集。

`completion.json` 包含标题数、取得正文数、访问缺口数、搜索次数、工作线程数与本次用时。Search 失败原因保留在每条的 `searches[].result` 中，正文失败保留在 `documents[].reason` 中。采集不读取之前的方向判断，也不调用大语言模型。

每次采集或分析前自动执行[七天缓存清理](cache-retention.md)。超过七天的旧 collection 和工作流会被移除；读取封存结果不会刷新保存时间。过期后需重新准备任务或使用新的 collection ID 采集。

服务只抽取公开 HTML，不渲染 JavaScript 页面，不绕过付费墙和验证码。正文只在内存与本地响应中存在，工作流持久化最多 200 词截段及内容 SHA256。日期保留原始值，仅显式带时区的发布时间进入规范化字段；Finnhub 的消息时间不补作文章发布时间。取得正文不代表公司、事件和日期已经语义核实，仍需后续逐条审核。

## HTTP 接口

两个接口均为 POST JSON，仅允许 loopback 客户端，不开放浏览器跨域调用；Search 和 Fetch 可以部署到不同端口，各服务须提供 `GET /health` 返回 `{"status":"ok"}`。

```json
{"query":"NVIDIA announces financial results"}
```

`/search` 返回 `{"status":"ok","sources":[{"url":"https://...","title":"...","snippet":"...","published_at":null}]}`；失败返回 `status=unavailable` 与 `reason`。

```json
{"urls":["https://nvidianews.nvidia.com/news/..."],"expected_title":"NVIDIA announces financial results"}
```

`/fetch` 每次支持 1–5 个 URL，返回 `documents`，每个 URL 独立带 `status`、正文 `markdown` 或失败 `reason`、标题、原始发布日期、抓取时间和 SHA256。`expected_title` 是后续审核上下文，服务不会据此自动宣告标题已验证。抓取仅接受公网 HTTPS，逐次检查重定向地址，页面大小限制为 5 MB。

## 可选 SearXNG 后端

已有本地 SearXNG 时，启用其 JSON 输出后启动：

```bash
python scripts/skill.py news-local-serve --search-backend searxng --searxng-url http://127.0.0.1:8080
```

SearXNG 的 `settings.yml` 至少包含：

```yaml
search:
  formats:
    - html
    - json
```

本机服务通过 `/search?q=...&format=json` 调用 SearXNG；SearXNG 是否限流由其自身配置和上游引擎决定。本地适配器不再访问 TinyFish。

接口依据：[DDGS 官方说明](https://github.com/deedy5/ddgs)、[SearXNG Search API](https://docs.searxng.org/dev/search_api.html)、[Trafilatura](https://trafilatura.readthedocs.io/)。
