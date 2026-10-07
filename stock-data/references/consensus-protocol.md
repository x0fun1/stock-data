# Consensus and Adversarial Audit

## Consensus

Consensus reads A/B/C after all paths return. It requires valid snapshot identity, fatal data-integrity checks and contract-valid path probabilities. `mode=standard/strict` does not add OOS/calibration eligibility thresholds: weak/insufficient OOS and unavailable calibration lower confidence and set reporting to `DEGRADED`; they do not discard an otherwise valid probability. Detected leakage, invalid target construction or fatal data conflicts still veto. D never votes.

Eligible path estimates receive equal arithmetic weight; this is not a majority vote and the mean is **not** independently ensemble-calibrated. Keep `raw_probability`, `calibrated_probability` and `reportable_probability` distinct. The combined `calibrated_probability` is null unless a separately validated ensemble calibration exists. Report number/names of paths, mean, range/stddev, directional agreement ratio, disagreement grade and evidence-family diversity. The path range is not a statistical interval.

Confidence is separate from probability and never a veto by itself. The policy uses an explainable 0–100 penalty score, with labels high (80–100), medium (60–79), low (40–59), very low (<40). Report the causes, such as fallback calendar, unknown adjustment, single provider, OOS/calibration gaps, weak agreement or insufficient evidence. Dynamic reliability weights are not implemented.

If fewer than two paths return valid probabilities, label `single_path`; zero valid probabilities means unavailable. A single-path estimate may be reported as DEGRADED if it passes prediction integrity. Exclude an invalid/leaking path; explicit leakage detection or another fatal pipeline-integrity defect vetoes the combined report under current policy.

## Horizon semantics

With a resolved complete calendar, labels use confirmed exchange sessions. Under observation-date fallback, historical labels count `h` next OHLCV observations and the future horizon is estimated; report `horizon_type=estimated_observed_sessions` and lower confidence. Never call the fallback calendar-verified. Factor diagnostics follow the same frozen horizon semantics.

## Factor IC and forward-return diagnostic

Research D tests a fixed OHLCV factor set against 1D/5D/20D forward returns using chronological splits, non-overlapping labels and cutoffs learned only on the training segment. For a single ticker, label Spearman IC as time-series IC. The grouped return spread is a descriptive time-series statistic, not a long/short portfolio simulation. `factor_backtest` insufficiency is supporting-validation information and does not veto a probability absent detected leakage or invalid target construction.

The upstream cross-sectional Rank IC / portfolio contract remains unavailable until a dated multi-ticker factor panel, point-in-time membership, compatible price and security-mask matrices, calendar and benchmark are supplied. Transaction costs, turnover, benchmark-relative NAV and market impact are not inferred from close-to-close labels.

## Adversarial audit

This is stage E (Adversarial / Backtesting Bias Audit), implemented by `scripts/quant_research/audit.py` after consensus. Its output is `quant_result.json.adversarial_audit`, not a fifth researcher probability.

The audit checks snapshot identity/digest, path snapshot consistency, probability provenance/range, explicit feature/label leakage declarations, overlapping validation intervals, chronological holdout evidence, OOS Brier versus baseline, observed factor-candidate counts, PIT/future-date warnings, validation limitations, diversity and probability disagreement. Missing audit evidence is a confidence/reporting limitation; a declared failed leakage/target timing check is fatal. Timing checks rely on structured researcher declarations and are not a formal proof of every feature implementation.

Fatal veto conditions are blocked prediction eligibility, zero valid predictions, an explicitly non-forecast request, invalid snapshot identity, invalid probability contract, latest-price conflict, confirmed unadjusted corporate-action contamination, detected leakage or prediction failure. Calendar absence/fallback, adjustment unknown and weak OOS/missing calibration lower forecast confidence/report status but do not veto. PBO/DSR, transaction-cost and portfolio evidence are strategy-only warnings: they do not veto or downgrade probability-only reporting status, but strategy eligibility remains unvalidated. VETO suppresses every user-facing probability. Strategy eligibility is required before any profitability/strategy claim.

PBO, DSR, realistic costs/market impact and survivorship are `not_assessed` when the requisite strategy-trial return matrix, trial count, cost model, point-in-time universe or delisted-name history is absent. Candidate-factor counts describe observed search scope, not a complete count of strategy trials. Present PBO/DSR/cost values remain unverified without input and calculation receipts.

Quant forecast and factor-diagnostic paths use OHLCV only. After the Quant result and audit are frozen, a separate News/Sentiment stage reports timestamp-valid events, opinion tone and observed market reactions from the same frozen snapshot. It is not a forecast path and never votes in consensus or changes its probability. If article timing, coverage or evidence is insufficient, mark News `not_assessed`; a daily-bar association is not a causal finding. Only Final Synthesis reads both frozen results and preserves the original Quant estimate.

## Historical reliability heuristic

The initial version compares available OOS Brier scores to reported base-rate Brier baselines. This is a coarse path-health signal, not a stable track record. No persistent reliability history or regime-adjusted weighting is claimed.