# Point-in-Time Policy

## Primary research window

The forecast window is fixed: `(decision_session - 364 calendar days,
decision_session]`. Decision is the latest completed exchange session at the
as-of instant. The window does not slide with the run date, does not widen to
buy samples and has no warm-up bars outside it.

Features, matching samples, the base rate, factor selection, ML training and
calibration all read only data inside this window. A bar outside it is dropped
(and counted) or rejected; it is never used.

## Signal and target timing

For a decision at session `t`, features may use data available through that
close. The target is the close of the fifth exchange session after `t` divided
by the close at `t`, minus one, on the split-adjusted basis. It is not a natural
day approximation or an executable fill model. With a verified calendar, the
fifth session is resolved by session index; without one, no formal probability
is published.

## Availability and revisions

- Preserve source event time separately from fetch time.
- For text and fundamentals use publication/filing time, not period end.
- Provider price history is download-time revised. Snapshot manifests disclose
  `provider_revision_status` and `historical_availability_status`; bars do not
  claim a per-bar `available_at` unless supplied.
- No historical universe or index membership is inferred from today's data.
- No time decay or half-life is applied to the production estimator.

## Leakage controls

- Future returns are labels only; they never enter features.
- Research A: at decision `t`, a matching origin's label is eligible only when
  `origin + 5 <= t`; the base rate uses only labels matured by `t`. Every replay
  row rebuilds its own history from its boundary.
- Research B: factor selection uses only the chronological selection segment;
  the reported bucket probability uses only labels matured after that segment.
- Research C: every origin refits from labels matured by that origin inside the
  snapshot; the live entry and replay share the same function.
- Research D: 5-session labels only; training quantile cuts are fixed before the
  chronological holdout.
- If source timing, adjustment status or label boundary cannot be established,
  downgrade to diagnostic or reject; never silently repair lineage.