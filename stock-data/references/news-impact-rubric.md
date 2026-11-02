# News Impact Rubric

Qualitative rules for `news-1` event assessments and the unique major-event selection. The host explains evidence-backed impact; Python checks identity, timing, admissibility and citations. See [news-analysis.md](news-analysis.md).

Historical taxonomy review included the MIT-licensed `market-news-analyst` skill (<https://github.com/tradermonty/claude-trading-skills>, Copyright (c) 2026 TraderMonty). This is attribution, not a claim of importing upstream code or verifying its current license. See [upstream-provenance.md](upstream-provenance.md).

## Direction and importance

- `bullish` / `bearish`: identify the affected target and channel, supported scale and persistence, and unresolved conditions.
- `neutral`: evidence supports limited effect or explainable opposing effects. Missing evidence is null with a reason, never neutral.
- `major` / `routine`: qualitative importance with reasons. A dramatic title, a famous publisher or many reposts cannot establish materiality.
- Conflicting facts remain explicit; do not average them. An unresolved material contradiction may prevent any directional conclusion.

Do not use numerical sentiment, confidence percentages, stock-price reaction, abnormal volume, volatility patterns, continuation, decay weights or priced-in inference. No OHLCV is required. Necessary financial arithmetic describes disclosed facts, not an impact scoring formula.

## Major-event admission and selection

A candidate must be knowable at `as_of`, have a reliable original source and enough permitted text, link concretely to the target, qualify as major bullish/bearish, and not be invalidated by a correction, denial or withdrawal. Headline-only rumors remain unconfirmed candidates, not selected drivers.

First search the latest 7 calendar days for new events or substantive updates. If none qualifies, expand to 30 days and disclose that expansion. Otherwise return no selected event and a clear reason. Days 31–60 may remain background; reposting without new facts does not renew an event.

Compare substantiated impact scale/range, persistence, actual exposure, evidence quality and genuinely new information, in that order of explanation rather than with a numerical score. If reliable prioritization is impossible, disclose uncertainty and select none instead of inventing precision.

## Transmission and scope

A major event may be company, industry or macro. Show `published fact → affected market/business → transmission channel → evidenced target exposure → effect and opposing factors`. Relevant channels include demand, input costs, financing, foreign exchange, capital expenditure and valuation context. Without exposure evidence, retain market context rather than claiming a company-specific effect. A common macro event can have opposite effects on different companies.

## Source quality

Prefer original official documents and substantive text; preserve independent reporting lineage and source identity. Distinguish primary filings/releases, independent reporting, analysis/opinion and aggregators. Publisher names alone do not authenticate a claim, and syndication is not independent confirmation. An unknown publisher is not upgraded because its name contains a familiar abbreviation.

## Report and interpretation boundaries

The report renders source dates and URLs from registered evidence, and explains the selection reason, overall impact, strongest contrary evidence and what would change the judgment. A recent positive event need not determine the overall direction if a stronger continuing negative background is evidenced; explain the comparison.

Recency is not importance. Source reputation is not fact verification. Absence of text or company exposure is a limitation, not evidence of small impact. Source-backed publication timing may be described as a fact without inventing exchange times or predicting a subsequent price move.

Keep evidence and coverage records under the [retention policy](news-first-workflow.md). Do not remove all JSON after delivery. Actual account coverage and real-world verification remain separate from offline schema and report checks.
