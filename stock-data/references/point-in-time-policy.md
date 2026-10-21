# Point-in-Time Policy

## Primary research window

- The default direction-research window is the latest one year ending at the latest market session whose close is confirmed at the snapshot as-of time.
- Preserve any longer provider response in the raw envelope, but keep the selected primary window in normalized daily bars and record its actual start and end dates.
- Honor an explicit user-selected period. Do not silently widen a short window to satisfy calibration or holdout minimums; report the affected path as partial or insufficient with a null probability.
- A longer-history sensitivity analysis is a separate run with its own snapshot and results. Do not blend its probabilities into the primary window's consensus.

## Signal and target timing

For a daily close signal at session `t`, features may use data available through that close. The target is `close[t+h] / close[t] - 1`, where `h` is 1, 5 or 20 future exchange sessions. It is not a natural-day approximation or an executable fill model.

## Availability and revisions

- Preserve the source's event time separately from gateway fetch time.
- For text and fundamentals, use publication or filing/availability time. The economic period end alone does not establish that data was known then.
- Preserve SEC `filed_at`, accession, units, period, and revision/vintage when supplied.
- Latest-only financials, analyst aggregates and recommendations can describe the current state but cannot be used to construct historical values without a vintage history.
- News without a reliable publication timestamp is excluded from a strict historical test.
- No historical index/universe features are inferred from today's membership.
- Research A/B/C/D numeric forecast and factor-diagnostic paths continue to use OHLCV only. After Quant is complete and frozen, the separate News layer can analyze only articles with timezone-aware publication/update timestamps no later than the shared snapshot as-of. It does not extend the selected OHLCV window or fetch again.
- The News layer uses a source-reported score only when present; deterministic event/opinion screening is marked heuristic and uncalibrated. It deduplicates articles into canonical events, counts opinion clusters separately, and computes recency in observed exchange sessions. The default half-life is the requested forecast horizon in sessions; `--news-policy` may configure `half_life_sessions_by_event_type`.
- Market reaction uses supplied close times when available; nominal 16:00 is an unverified fallback. Intraday news is whole-day association without a post-publication opening gap. Divergence does not establish sell-the-news/priced-in causality. Benchmark-relative return and immature windows remain unavailable.
- No historical point-in-time news archive is fabricated from a short current-news feed. A one-year Quant price window does not imply one year of News coverage; report source coverage separately.

## Leakage controls

- Future return labels are generated only after feature construction and never enter feature values.
- Research A uses prior, matured, non-overlapping labels for conditional frequencies. At decision session `t`, a label from `i` is available only when its `label_end = i + h <= t`; each walk-forward prediction rebuilds its matching history from that boundary. The full-history ledger records one prediction per matured decision session; adjacent horizon labels overlap, so conservative non-overlapping counts and effective-size estimates are disclosed and block resampling is used for skill intervals. The current implementation has no Bayesian prior or time decay.
- Research B selects signs and scales in an earlier chronological segment, maps scores in a later segment, and reports an untouched later holdout separately.
- B decay diagnostics require label_end strictly before holdout_start; current signed contributions are separate from training IC signs.
- Research C uses explicit information intervals and a production-consistent replay. At each decision the model is refitted at an absolute 20-session anchor using labels that had matured at that anchor, with training decisions capped at the trailing 252 sessions. The replay scores non-overlapping H+1 decisions only, and each row is produced by the same `predict_at_origin()` function the live entry uses. Calibration logits come only from matured, non-overlapping replay rows whose label boundary is strictly before the decision; when fewer than 40 such rows exist, the raw logistic probability is published and the calibrator is shadow-only. There is no frozen pre-holdout mapping and no final-holdout transfer step in the published path.
- Research D computes OHLCV factors only through close[t], forms forward returns only as labels, learns training quantile cutoffs before the chronological holdout, and uses non-overlapping label starts. With one ticker, its Spearman statistic is time-series IC rather than cross-sectional Rank IC.
- If source timing, adjustment status, or label boundary cannot be established, downgrade or reject the affected result. Do not silently repair lineage.
- [Reliability gates](reliability-gates.md) define calendar/close/freshness and adjustment. Publication, occurrence, announcement and scheduled times remain separate; a new article cannot reset an old event's age.
