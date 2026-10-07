# Probability Policy

## Allowed quantitative sources

- Research A: historical conditional hit frequency among non-overlapping, matured labels that match the predeclared signal condition. Report sample count and Wilson interval.
- Research B: empirical future-return frequency in a score bucket fixed from a later calibration segment, with the selection segment and bucket counts reported.
- Research C: the native logistic model's `predict_proba`-equivalent sigmoid output; apply Platt scaling only when enough causal walk-forward predictions exist. Mark uncalibrated output explicitly.
- Consensus: equal-weight arithmetic mean of valid, provenance-bearing path probabilities. It is not a majority vote.

All sources require [reporting gates](reliability-gates.md). Strict excludes raw model probabilities and missing/insufficient OOS. Consensus is equal_weight_mean_not_ensemble_calibrated; path range is not a statistical interval. Excluded/vetoed numeric artifacts cannot be delivered as forecasts.

## Disallowed

- An LLM's subjective percentage, expected return or confidence.
- A value filled from an unavailable path or missing data.
- Treating `raw model score` as calibrated probability.
- Hiding sample size, calibration status, horizon, label rule, or holdout behavior.
- Showing precision beyond what the underlying sample and model justify.

When a probability is not supported, emit `null`, explain the missing condition, and continue with descriptive evidence only. Do not convert a factor score, sentiment label or directional majority into a probability without a documented historical mapping.
