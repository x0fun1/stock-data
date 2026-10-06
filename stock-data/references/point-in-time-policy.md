# Point-in-Time Policy

## Primary research window

- The default direction-research window is the latest three calendar years ending at the latest market session whose close is confirmed at the snapshot as-of time.
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
- Market reaction mapping uses the first frozen exchange session whose close follows publication (same local session for news no later than the nominal 16:00 close). Daily returns, gaps, volume z-scores, and volatility changes are observed associations, not causal estimates. Sector-relative return is unavailable without a compatible frozen benchmark. If News coverage/timing or reaction bars are inadequate, mark that part not assessed.
- No historical point-in-time news archive is fabricated from a short current-news feed. A three-calendar-year Quant price window does not imply three years of News coverage; report source coverage separately.

## Leakage controls

- Future return labels are generated only after feature construction and never enter feature values.
- Research A uses prior, matured, non-overlapping labels for conditional frequencies.
- Research B selects signs and scales in an earlier chronological segment, maps scores in a later segment, and reports an untouched later holdout separately.
- Research C uses explicit information intervals, closed-boundary purge, embargo, walk-forward validation, fold-local scaling, Platt calibration on causal out-of-fold scores, and a chronological holdout.
- Research D computes OHLCV factors only through close[t], forms forward returns only as labels, learns training quantile cutoffs before the chronological holdout, and uses non-overlapping label starts. With one ticker, its Spearman statistic is time-series IC rather than cross-sectional Rank IC.
- If source timing, adjustment status, or label boundary cannot be established, downgrade or reject the affected result. Do not silently repair lineage.
