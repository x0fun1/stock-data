# Snapshot Schema 1.3

冻结快照由 `quant_research.py freeze` 生成，包含：

- `manifest.json`：`snapshot_id`、ticker、market、horizon、as-of、请求、来源 digest、内容 digest、数据校验与来源状态。
- `<domain>.json` 与 `raw/<domain>.json`：`market`、`fundamentals`、`expectations`、`news`、`insider`、`options`、`short_volume`、`sec`、`macro`。
- 每个 domain 保留 `status`、`actual_source`、来源时间、抓取时间与实际数据；缺失证据保持 null。

新闻只保存标题、时间、来源、链接与摘要字段，不保存正文。所有时间转 UTC ISO；`published_at > as-of` 的条目丢弃并计数。历史 as-of 只保证发布时间不晚于 as-of，不认证当时可见性。

快照不含任何方向预测字段、概率、模型分数或前向台账。旧预测字段的读取不再支持。

## 行情字段

`market.data.bars[]` 使用 `date/open/high/low/close/volume`，严格按交易日升序、唯一、无未来日期。`history_window` 必须描述实际保留的 bar 范围；描述性走势窗口最多最近 52 周（约 250 个交易日）。

`session_calendar` 可选；缺失时只能使用观测日期，并披露未验证。价格基础必须明确；未知单位保持 null，不猜币种或交易单位。
