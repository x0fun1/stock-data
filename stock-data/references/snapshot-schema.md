# Frozen Snapshot Schema

Current schema is **news-1**. The authoritative request, assessment and import contracts are emitted by `news_research.py schema --kind request|assessment|import`. Prior-version research outputs are not compatible evidence imports.

## Identity and integrity

Each run has `snapshot.json` and `manifest.json`. The manifest records run identity, creation time, input digest, snapshot filename and replay status. The snapshot includes normalized request, records/evidence/events, financial/exposure context, coverage and limitations. Hashes establish content integrity, not supplier authentication.

A new run uses its own directory. `prepare` reads the frozen snapshot and emits `prepared.json`; the host writes the assessment outside the evidence snapshot. `analyze` validates it before storing results and rendering `report.md`. `validate-report` checks the actual outgoing response and records delivery identity. Paths must remain contained; symlinks, tampering and unregistered artifacts must not bypass safety checks.

## Request and evidence

The request binds symbol, US/HK market, currency, stock/ETF type, timezone-aware `as_of`, explicit sources, budget and policy/prompt/model identity. The news window is `(as_of-lookback_days, as_of]`, default 60 calendar days and allowed 30–60; recent major selection starts at 7 days.

Quotes and OHLCV are optional background, never required to freeze or complete sufficiently evidenced news research. Records preserve source identity, safe links, publication/update/fetch precision, text scope, permissions and content versions. Articles, evidence and events are separate objects so reposts do not multiply events or reset recency.

## Data quality

Coverage records successful and failed query intervals, pagination completion, truncation and budget omissions. Collection status, window coverage and analysis sufficiency are independent. `complete_for_configured_sources` means only the configured source/query scope, not the whole internet. A host assessment cannot override an excluded record or claim missing coverage was complete.

## News

The prepared data includes frozen evidence and analysis batches, plus an omitted queue when the analysis budget is reached. Host prose cites registered IDs; source titles, URLs, dates and numbers are not authored by the model. A changed input digest, policy, prompt, model, evidence or context invalidates prior assessment reuse.

## Retention

Shared SQLite evidence and coverage records are separate from per-run artifacts. Subject to content permission, retain at least 120 days of required evidence; shorter license retention takes precedence and must disclose loss of local replayability. Do not clear every JSON file after delivery.

`gc` defaults to dry-run. Preview `gc --output-root runtime --dry-run --retention-days 120`; actual authorized cleanup requires explicit `--apply`. The cleanup implementation handles only known owned artifacts and permitted expirations, never arbitrary external paths or the shared database itself. See [news-first-workflow.md](news-first-workflow.md) for the operational flow.