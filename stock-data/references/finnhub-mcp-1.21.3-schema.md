# Finnhub MCP 1.21.3 Runtime Schema Reference

本文件承载主 `SKILL.md` 中拆出的运行时 schema 细节。基线为 `finnhub-mcp@1.21.3`；工具名、参数和响应字段来自已安装服务器的实际 `tools/list` 描述，而非推测。服务器本次没有提供独立的 MCP `outputSchema`，因此响应字段以工具描述及实际返回的 JSON envelope 为准。升级后先重新检查当前 schema，再更新本文件。

下文的注册别名是 Hermes 环境的记录。其他运行环境按当前会话暴露的工具清单调用，不要直接照搬别名。

## Hermes 别名与 server-native 名称

当前 Hermes 将工具注册为 `mcp__finnhub__<tool>`，并把工具名中的连字符转换成下划线。例如 server-native `get-quote` 对应 `mcp__finnhub__get_quote`。实际调用时以当前 Hermes 工具清单为准。

| Hermes 注册名 | MCP server-native 名 |
|---|---|
| `mcp__finnhub__get_exchange_symbols` | `get-exchange-symbols` |
| `mcp__finnhub__get_peers` | `get-peers` |
| `mcp__finnhub__get_news_pulse` | `get-news-pulse` |
| `mcp__finnhub__get_price_summary` | `get-price-summary` |
| `mcp__finnhub__get_financials_snapshot` | `get-financials-snapshot` |
| `mcp__finnhub__search_tools` | `search-tools` |
| `mcp__finnhub__get_calendar` | `get-calendar` |
| `mcp__finnhub__get_company_profile` | `get-company-profile` |
| `mcp__finnhub__get_insider_signal` | `get-insider-signal` |
| `mcp__finnhub__get_quote` | `get-quote` |
| `mcp__finnhub__search_symbol` | `search-symbol` |
| `mcp__finnhub__get_recommendations` | `get-recommendations` |

## Runtime tool descriptions and fields

### `search-tools` — `mcp__finnhub__search_tools`

- Required: `intent` (natural-language intent, max 200 characters).
- Optional: `view` = `summary|standard|full`; defaults to summary.
- Returns: `intent`, `matches[]` (name, title, score, category, premium, description), `total_matches`.
- Use only for capability discovery; do not call mechanically for known requests.

### `search-symbol` — `mcp__finnhub__search_symbol`

- Required: `query` (ticker, company name, ISIN, or CUSIP).
- Optional: `exchange`, `limit` (default 10, maximum 100), `view`, `fields` (sparse field projection array).
- Returns: `count`, `result[]`; each match can contain `symbol`, `display_symbol`, `description`, `type`, `confidence_score`, `is_exact_match`.

### `get-quote` — `mcp__finnhub__get_quote`

- Required: `symbol`.
- Optional: `view` exists in the input schema, but the tool description says there is no view variation; normally omit it.
- Returns: `symbol`, `current`, `change`, `percent_change`, `high`, `low`, `open`, `prev_close`, `timestamp_utc`.
- Quote cache tier is about 10 seconds; preserve the returned timestamp when calling it current.

### `get-company-profile` — `mcp__finnhub__get_company_profile`

- Required: `symbol`.
- Optional: `view` = `summary|standard|full`; defaults to summary.
- Returns: nullable `ticker`, `name`, `country`, `currency`, `exchange`, `ipo`, `industry`, `market_cap` (USD millions), `share_outstanding` (millions of shares); `logo`, `phone`, `weburl` are populated only in standard/full.
- Profile cache tier is about 24 hours. Response may not have an as-of timestamp; never call it real-time when no timestamp is supplied.

### `get-financials-snapshot` — `mcp__finnhub__get_financials_snapshot`

- Required: `symbol`.
- Optional: `view` = `summary|standard|full`; defaults to summary.
- Returns 10 curated nullable KPIs: `market_cap` (USD millions), `pe_ttm`, `pb_annual`, `eps_ttm`, `dividend_yield`, `week52_high`, `week52_low`, `week52_price_return_pct`, `beta`, `revenue_per_share_ttm`.
- `raw` is populated only in full. Do not infer missing KPIs or units not specified by the returned description.

### `get-price-summary` — `mcp__finnhub__get_price_summary`

- Required: `symbol`.
- Optional: `period` = `7d|30d|90d|1y` (default 30d), `view` = `summary|standard|full` (defaults to summary).
- Returns: `symbol`, `period`, `resolution`, `min`, `max`, `mean`, `return_pct`, `vol` (population standard deviation of closes), `latest{close,timestamp_utc}`, `candle_count`.
- `candles` is populated only in full. `1y` uses weekly resolution; 7d/30d/90d use daily resolution. This endpoint may be Premium-locked; check `premium`, `error_type`, and `error_message` before interpreting it as missing price data.

### `get-news-pulse` — `mcp__finnhub__get_news_pulse`

- Required: `symbol`.
- Optional: `view` = `summary|standard|full`; defaults to summary.
- No date-window parameters. Period is fixed at `7d`.
- Returns: `symbol`, `period`, nullable `sentiment_score`, `bullish_percent`, `bearish_percent`, nullable `sentiment_source`, `top_headlines[]` with `headline`, `url`, `source`, `datetime_utc`, `count`, `delta_vs_prev_week`.
- Summary/standard show the top 5 headlines; full shows all articles. Sentiment fields can be null or absent when `/news-sentiment` is unavailable or Premium-locked. Never invent sentiment values.

### `get-recommendations` — `mcp__finnhub__get_recommendations`

- Required: `symbol`.
- Optional: `view` = `summary|standard|full`; defaults to summary. No date/period input.
- Returns: latest `period`, `consensus`, `strong_buy`, `buy`, `hold`, `sell`, `strong_sell`, `total`, nullable `change_vs_prev`.
- `snapshots[]` is populated only in full. Consensus is a derived analyst opinion, not a guaranteed outcome.

### `get-calendar` — `mcp__finnhub__get_calendar`

- Required: `kind` = `earnings|ipo|economic`.
- Optional: `symbol` (only valid for earnings), `country` (only valid for economic), `from`, `to` (ISO `yyyy-MM-dd`), `view` = `summary|standard|full`; defaults are UTC today and the relevant maximum window.
- Maximum windows: 90 days for earnings/economic; 365 days for IPO.
- Returns: `kind`, `from`, `to`, nullable/echoed `symbol` and `country`, `total_count`; the kind-specific array is `earnings_events[]`, `ipo_events[]`, or `economic_events[]`.
- Earnings fields include `symbol`, `date`, `hour` (`bmo|amc|dmh|null`), `quarter`, `year`, `eps_actual`, `eps_estimate`, `revenue_actual`, `revenue_estimate` (nullable). Do not assume units when they are not present.
- IPO fields include `symbol`, `name`, `date`, `exchange`, `price`, `number_of_shares`, `total_shares_value`, `status`. Economic fields include `country`, `event_name`, `time_utc`, `impact`, `actual`, `estimate`, `prev`, `unit`.
- Summary caps at 10 events; standard at 25; full returns the complete window. The calendar cache uses the News tier.

### `get-insider-signal` — `mcp__finnhub__get_insider_signal`

- Required: `symbol`.
- Optional: `from`, `to` (ISO `yyyy-MM-dd`; defaults to trailing 30 days and maximum window is 90 days), `view` = `summary|standard|full`.
- Returns: `symbol`, `from`, `to`, `net_buy_sell_30d`, `notable_names`, `total_count`, nullable `latest` transaction. `transactions[]` is populated only in full.
- `latest` may include `name`, `change`, `share`, `transaction_date`, `filing_date`, `transaction_price`, `transaction_code`, `is_derivative`, `currency`.

### `get-peers` — `mcp__finnhub__get_peers`

- Required: `symbol`.
- Optional: `grouping` = `industry|subindustry|sector` (default industry), `view` = `summary|standard|full`.
- Returns: `peers[]`, `grouping`, `total_count`, `has_results`. Summary caps at 10 peers, standard at 25, full returns all.

### `get-exchange-symbols` — `mcp__finnhub__get_exchange_symbols`

- Required: `exchange` (1–8 letters; e.g. `US`, `L`, `T`).
- Optional: `view` = `summary|standard|full`.
- Returns: `exchange`, `total_count`, `type_breakdown`, optional `symbols[]` of `{symbol, display_symbol, description, type}`, and `has_results`.
- Summary returns count/breakdown only; standard includes at most 25 sample symbols; full at most 100. It is not a full exchange universe. Free Finnhub plans support `exchange='US'`; other exchanges may return Premium.

## Common MCP result envelope

The MCP tool descriptions document a JSON response envelope commonly containing:

- `is_success` (bool), `data` (payload or null), `error_message`, `error_type`.
- `view`, `next_actions`, `explanation`, `approx_tokens`.
- `rate_limit` (when observed: `remaining`, `reset_at`, recent throttled count), `premium` (bool), and optional `sentiment_source`.

Not every key is present on every success/error. Check the outer MCP error flag if exposed, then inspect the envelope's `is_success`; treat absent/null fields as unavailable, not as permission to infer them.

## Current access/error behavior

- A Premium-locked endpoint can return `is_success=false`, `error_type='PremiumRequired'`, an HTTP 403 explanation, and `premium=true` while the MCP call itself is not a protocol error. Stop retrying that endpoint; use other successful tools and disclose the gap.
- A 429 is rate limiting: stop additional calls, use obtained data, and report `rate_limit.remaining`/`reset_at` when available.
- Cache tiers observed in the tool descriptions: Quote about 10 seconds; News about 60 seconds; Financials about 1 hour; Profile about 24 hours; Exchanges about 7 days. These are cache tiers, not source timestamps or guarantees of data freshness.
- A successful envelope can still omit a nullable metric or sentiment. Never fill null/missing values from memory.
