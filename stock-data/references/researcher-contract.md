# Request Contract

请求对象（JSON）字段：

- `ticker`：必填，已校验的证券代码。
- `market`：`US` 或 `HK`，默认 `US`。
- `asof`：带时区的 ISO-8601 时间；历史 as-of 输出带 `pit_status=published_at_only`。
- `intent`：`news`（默认）或 `descriptive`。
- `horizon`：`30D` 或 `60D`，默认 `60D`。
- `mode`：`standard` 或 `strict`。
- `asset_type`：`stock` / `etf` / `index` / `unknown`。
- `instrument`：可选的已解析代码、provider symbol、币种与单位。
- `history_window`：描述性走势窗口；最多最近 52 周。

旧 `intent=forecast` 或 `horizon=5D` 不再产出任何估计，返回结构化错误 `FORECAST_REMOVED`，并指向 [news-analysis.md](news-analysis.md)。不存在隐藏开关。

请求只描述“要分析什么/截至何时”，不包含 provider 密钥、抓取命令或外部模型的指令。
