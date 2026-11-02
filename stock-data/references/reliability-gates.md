# Reliability Gates

The gates answer separate questions. No single numerical confidence score is published.

| Dimension | Meaning | Failure action |
|---|---|---|
| Collection | Queries and pagination completed, partially completed, failed or unavailable | Preserve errors and usable evidence; never turn an API failure into successful zero news |
| Window coverage | Configured sources/intervals have supported completion receipts | Mark `partial`/`unknown`; disclose gaps, not just article span |
| Evidence | Source, text, time, target association and version support the claim | Keep unconfirmed context but exclude unsupported major drivers |
| Assessment | Host output matches the frozen run and cites admitted evidence | Preserve collection, reject invalid output and leave analysis incomplete |
| Delivery | Actual outgoing text matches the validated report | Reject altered/missing facts, citations and status disclosures |

## Stage contract

`collect → prepare → analyze → report → validate-report` is the current news-first route. Quotes and OHLCV are not required. Analysis is offline after preparation; sources and scope changes require new frozen evidence. The source registry is a capability declaration, not proof of account access or current coverage.

## Data gates

The request has a timezone-aware cutoff and 30–60-day window (default 60). Source text published after the cutoff, unavailable historical revisions, unsafe URLs, unsupported identities and missing permissions cannot enter conclusions. Failed, partial and empty source responses remain distinct.

A short feed is partial even if every returned article is valid. A sparse feed can be complete for its configured queries when interval and pagination receipts establish that fact. Under 30 days must explicitly state the target was not reached.

## Evidence gates

- Event direction requires sufficient evidence; `neutral` does not mean missing data.
- Major drivers require substantive text, target linkage and no invalidating update; headline-only material stays unconfirmed.
- Macro/industry and ETF impact require dated actual exposure evidence.
- Selection uses only admitted major bullish/bearish events, prioritizes 7 days then 30 days, and permits null rather than forced ranking.
- The same admitted set supports event direction, dominant reasons and selection. The host cannot revive excluded records.
- Partial coverage or unassessed events constrain synthesis status even when a limited judgment is possible.

## Claim boundaries

Disclose missing text, source/account failures, revision precision, inconsistent financial periods/currencies, unconfirmed exposures and analysis omissions. No model-invented sources, dates or numbers may appear. Lint/schema success is not real-world data acceptance; report untested access honestly. See [audit-protocol.md](audit-protocol.md) and [news-first-workflow.md](news-first-workflow.md).