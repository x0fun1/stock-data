# Point-in-Time Policy

## Primary research window

The news window is `(as_of - lookback_days, as_of]`, default 60 calendar days and configurable from 30 to 60. `as_of` must include a timezone and is not recomputed during later stages. No quote, OHLCV or exchange-session history is required.

Recent major selection starts with the latest 7 calendar days, expands to 30 days only when no eligible candidate exists, and otherwise returns none. Original publication and substantive update dates determine recency; a repost does not reset an old event.

## Event timing

Preserve publication, update, occurrence, effective, scheduled and fetch times independently. Date-only precision cannot be upgraded to an invented timestamp. A future scheduled event can be discussed as pending, never as already completed. Still-effective older context keeps its original date and is not passed off as new news.

## Availability and revisions

- Preserve source event time separately from fetch time.
- For text and fundamentals use publication/filing time, not period end.
- If a body was revised after the cutoff and the earlier version is unavailable, exclude the revised text from the earlier judgment and disclose the gap.
- Financial periods and observation dates do not establish publication time. Keep filing/accession and initial versus revised releases distinct.
- Present-day company data or ETF holdings cannot establish historical exposure without dated evidence.
- Optional provider price history is download-time revised unless an actual historical vintage exists; it does not certify historical news availability.

## Evidence controls

- The host reads frozen evidence only; a new fact or changed source creates a new input identity and requires reassessment.
- Source/query coverage is established by receipts, pagination and explicit failure intervals, not article count or publication span.
- Preserve superseded/contradictory versions and disclosure precision; do not silently repair chronology.
- Missing source or timing evidence cannot be filled from model memory or fetch time. Keep the limitation visible and use only admitted evidence in conclusions.

See [news-first-workflow.md](news-first-workflow.md) and [snapshot-schema.md](snapshot-schema.md).