# Probability Policy

## Allowed quantitative sources

- Research A: historical conditional hit frequency among non-overlapping, matured labels matching the predeclared signal condition. Report sample count and Wilson interval.
- Research B: empirical future-return frequency in a score bucket fixed from a later calibration segment, with the selection segment and bucket counts reported.
- Research C: logistic model `predict_proba`-equivalent output; apply Platt scaling only when enough causal walk-forward predictions exist. Mark uncalibrated raw output explicitly.
- Consensus: equal-weight arithmetic mean of valid, provenance-bearing path estimates. It is not a majority vote or an independently calibrated ensemble.

All sources require [prediction-integrity and reporting checks](reliability-gates.md). Strict mode does not exclude raw probabilities or require a minimum OOS/calibration threshold for prediction eligibility. Insufficient OOS, calibration or strategy audit lowers confidence/reporting status; detected leakage, fatal data conflict or an absent valid prediction still vetoes. `forecast_eligible` means prediction-computable, not full research or strategy validation.

## Probability fields

Keep these separate:

- `raw_probability`: the model/path estimate before any calibration layer, when available;
- `calibrated_probability`: an explicitly calibrated path probability, if valid; the consensus ensemble value stays null without a separately validated ensemble calibrator;
- `reportable_probability`: value passed through the reporting gate; on fatal VETO it is null and hidden from user-facing output;
- `probability_basis` and `calibration_status`: identify exactly how the displayed number was formed.

A valid generated raw probability remains available when calibration is missing; report it with low confidence and `DEGRADED` status. Calibration absence is not a numerical failure. An equal-weight path mean must not be called calibrated simply because one contributing path used Platt scaling.

## Mandatory Quant → final-response fields

For a valid reportable Quant result, Final Synthesis and the user reply retain `prob_up`, `direction`, `confidence`, `agreement`, report status, numerical confidence score and probability basis; include `diversity` where present. These are copied from the same ticker/horizon/as-of `quant_result.json.consensus`, and are preserved in `synthesis.quant` and `agent_summary.sections.final_synthesis.quant`. News cannot change Quant probability; divergence can change integrated conviction/risk, not the numerical estimate.

The default chat response is concise Summary. The standard `report.md` shows validation, factors, OOS/calibration and audit scope. JSON/debug artifacts retain source proofs, reason codes, raw path outputs and manifests; do not expose these details as ordinary prose unless requested.

The relationship between Quant and News must be described as aligned/mildly aligned/neutral/conflicting/unavailable. Probability stays unchanged. Distinguish Quant confidence from integrated conviction and explain risk offsets.

Absent/invalid/vetoed probabilities remain null and the final response must say exactly `上涨概率：不可用`, explain the fatal reason, and mark the analysis incomplete/abstained. Percentage formatting is presentation only: JSON retains unrounded numeric values. No LLM-generated, estimated, smoothed, news-adjusted, or other-ticker probability is allowed.

Generic stock/ETF analysis, including brief analysis, uses the full workflow by default even when no probability was explicitly requested. Brevity changes presentation only. Explicit fact/history/specialist-only requests can retain their narrow route. Do not substitute a quote/fundamentals/news digest for generic analysis.

The runtime writes `final_response.md` after both stages freeze and synthesis completes. Preserve its literal identity, Quant fields, reporting status, calibration/unavailability, Quant-News relationship and probability note. Before sending the actual prepared text, run `validate-response --analysis-dir <report_dir> --response-file <prepared_reply.md>`. It verifies frozen digests, report binding, completed stage receipts and required literal lines; missing/conflicting labeled fields fail. It does not intercept host messages or certify every prose claim. On pre-artifact collection/validation failure, explicitly report unavailable probability and the incomplete stage instead of claiming completed research.

## Disallowed

- An LLM's subjective percentage, expected return or confidence.
- A number filled from an unavailable path or missing data.
- Treating a raw model score as a calibrated probability.
- Hiding sample size, calibration status, horizon, label rule or holdout behavior in standard/debug reports.
- Showing precision beyond what the sample/model justifies.

When a probability is unsupported, emit `null`, explain the missing condition and continue with descriptive evidence only. Do not convert a factor score, sentiment label or directional majority into probability without a documented historical mapping.