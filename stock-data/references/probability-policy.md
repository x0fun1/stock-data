# Probability Policy

## Allowed quantitative sources

- Research A: historical conditional hit frequency among non-overlapping, matured labels that match the predeclared signal condition. Report sample count and Wilson interval.
- Research B: empirical future-return frequency in a score bucket fixed from a later calibration segment, with the selection segment and bucket counts reported.
- Research C: the native logistic model's `predict_proba`-equivalent sigmoid output; apply Platt scaling only when enough causal walk-forward predictions exist. Mark uncalibrated output explicitly.
- Consensus: equal-weight arithmetic mean of valid, provenance-bearing path probabilities. It is not a majority vote.

All sources require [reporting gates](reliability-gates.md). Strict excludes raw model probabilities and missing/insufficient OOS. Consensus is equal_weight_mean_not_ensemble_calibrated; path range is not a statistical interval. Excluded/vetoed numeric artifacts cannot be delivered as forecasts.

## Mandatory Quant → final-response fields

For a valid, reportable Quant result, Final Synthesis and the user reply retain `prob_up`, `direction`, `confidence`, `agreement`, and `diversity` when present, copied from the same ticker/horizon/as-of `quant_result.json.consensus`. Synthesis stores them in `synthesis.quant`; the bounded Agent summary repeats them in `sections.quant` and `sections.final_synthesis.quant`. Never substitute a direction adjective for an available numerical probability.

Display Quant P(up) explicitly in the Quant section and final conclusion. News cannot change it; divergence can change the synthesis confidence/risk assessment, not Quant's probability or metadata. The current confidence/agreement/diversity fields are categorical, not percentages. Preserve native values/scales and distinguish Quant confidence from overall confidence. Numeric examples are not defaults or model outputs.

Absent/invalid/vetoed probabilities remain null and must display exactly `上涨概率：不可用`; explain the reason rather than filling a value. Percentage formatting is presentation only: JSON retains the unrounded model value. No LLM-generated, estimated, smoothed, news-adjusted, or other-ticker probability is allowed.

## Disallowed

- An LLM's subjective percentage, expected return or confidence.
- A value filled from an unavailable path or missing data.
- Treating `raw model score` as calibrated probability.
- Hiding sample size, calibration status, horizon, label rule, or holdout behavior.
- Showing precision beyond what the underlying sample and model justify.

When a probability is not supported, emit `null`, explain the missing condition, and continue with descriptive evidence only. Do not convert a factor score, sentiment label or directional majority into a probability without a documented historical mapping.
