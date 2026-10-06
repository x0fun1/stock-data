# Consensus and Adversarial Audit

## Consensus

Consensus reads the A/B/C forecast result files only after all paths finish. Research D (`factor_backtest`) is a diagnostic and is excluded even if a malformed result contains a probability. Valid numeric forecast outputs with quantitative provenance receive equal weight in the MVP. Report:

- number and names of available paths;
- mean probability, range and standard deviation;
- directional agreement ratio, without replacing probability magnitude with a vote;
- disagreement grade: high (`range <= 0.05`), medium (`<= 0.15`), high disagreement (`<= 0.30`), or extreme disagreement (`> 0.30`);
- evidence-family diversity and confidence category.

Confidence is a separate categorical assessment. The local heuristic combines agreement, validation evidence, snapshot quality and distinct evidence families. It is intentionally conservative and must be shown with its components. Dynamic reliability weights are not implemented.

If fewer than two paths return valid probabilities, label the result `single_path` or `unavailable`; never call it ensemble consensus. If two paths succeed, report a degraded 2-path result and missing researcher.

## Factor IC and forward-return diagnostic

Research D tests a fixed OHLCV factor set against 1D/5D/20D forward returns using a chronological 70/30 split, non-overlapping labels and quantile cutoffs learned only on the training segment. For a single ticker, label Spearman IC as time-series IC. The grouped return spread is a descriptive time-series statistic, not a long/short portfolio simulation. The report must show the requested-horizon holdout observations and missing sample thresholds; `report.json` retains all horizons.

The upstream cross-sectional Rank IC / portfolio contract remains unavailable until a dated multi-ticker factor panel, point-in-time membership, compatible price and security-mask matrices, calendar and benchmark are supplied. Transaction costs, turnover, benchmark-relative NAV and market impact are not inferred from close-to-close labels.

## Adversarial audit

The audit does not generate a new forecast. It checks snapshot identity and digest, researcher snapshot consistency, probability provenance/range, invalid status, residual train/test interval overlap, declared feature/label timing audits, chronological holdout evidence, OOS Brier versus the base-rate baseline, observed factor-candidate counts, PIT/future-date warnings, sample/validation limitations, diversity and probability disagreement. Timing checks rely on structured researcher audit declarations; they are not a formal proof of every feature implementation.

Veto conditions include invalid/mismatched snapshot identity, a researcher-declared invalid result, a probability without quantitative provenance, an out-of-range probability, or nonzero retained interval overlap in ML validation. A veto suppresses the normal directional conclusion. Low sample, partial domain coverage, stale data, poor calibration and weak diversity create warnings and reduce confidence.

The anti-overfit audit reports PBO, DSR, realistic costs/market impact, and survivorship as `not_assessed` when the requisite strategy-trial return matrix, trial count, cost model, point-in-time universe or delisted-name history is absent. A count of candidate factors is reported as observed search scope; it is not treated as a complete count of strategy trials. Present PBO/DSR/cost values are labeled unverified unless their inputs and calculation receipts are provided.

Quant forecast and factor-diagnostic paths continue to use OHLCV only. After the Quant result and audit are frozen, a separate News/Sentiment stage can report timestamp-valid events, opinion tone, and observed market reactions from the same frozen snapshot. It is not a forecast path and never votes in consensus. If article timing, coverage, or evidence is insufficient, mark the affected News field `not_assessed`; a daily-bar association is not a causal finding. Only Final Synthesis reads both frozen results, and it must preserve the original Quant probability without blending.

## Historical reliability heuristic

The first version compares available OOS Brier scores to their reported base-rate Brier baselines. This is a coarse path-health signal, not a stable track record. No persistent reliability history or regime-adjusted weighting is claimed.
