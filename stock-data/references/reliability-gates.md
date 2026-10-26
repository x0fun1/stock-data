# Reliability Gates

The gate answers separate questions. No single 0–100 score is published.

| Flag | Positive condition | Failure action |
|---|---|---|
| `computable` | A returned a probability or a reasoned abstention | report `上涨概率：不可用` with the reason |
| `current_prediction_integrity_pass` | current probability replays exactly from frozen bars and fixed parameters | veto the probability |
| `oos_integrity_pass` | every non-abstained replay row replays exactly; every label matured by its origin | veto the probability |
| `production_probability_eligible` | policy version, complete 52-week coverage, verified exchange calendar, **resolved 5th-session target**, split-adjusted basis, current integrity, replay integrity, ≥12 **effective** replay samples | `prob_up = null`; no formal probability |
| `directional_edge_supported` | ≥30 matured replay decisions and the paired-improvement interval excludes zero on the positive side (primary block ≥ horizon+1) | `false`; no edge claim |
| `directional_edge_state` | `positive…` / `negative…` / `insufficient_evidence` | the delivered line must say 已支持 / 无（劣于纯基率）/ 证据不足 |
| `trading_signal_eligible` | never in this version | always `false` |

The 12-match ROC20 floor is **not** an eligibility condition: the matching hit
rate carries no probability weight in v2 and is disclosed as context only.

## Data gates

`INSUFFICIENT_HISTORY_COVERAGE`, `PRICE_BASIS_UNVERIFIED`,
`FIXED_WINDOW_CONFLICT`, `OHLCV_OUT_OF_WINDOW`, `DECISION_SESSION_UNRESOLVED` and
`TARGET_SESSION_UNRESOLVED` prevent a formal probability. `CALENDAR_UNVERIFIED`
permits diagnostics only. A calendar fallback based on observed bars is not a
verified session axis and cannot certify the 5th-session target: publishing a
probability against an undetermined close is vetoed rather than disclosed.

Structural violations are rejected at freeze time. A blocked snapshot may still
exist for diagnosis, but it cannot publish a probability.

## Evidence gates

- `OOS_SUPPORT_INSUFFICIENT`: fewer than 12 effective replay samples
  (`valid rows / 5`). This replaced the phase-sampled 12-row count; the phase is
  a computation throttle and never gates eligibility.
- `NO_VALID_OOS_PROBABILITY`: no non-abstained replay probability.
- `OOS_INTEGRITY_FAILED`: at least one row failed transform/sample/label replay.
- `REPLAY_SUPPORT_INSUFFICIENT`: fewer than 30 matured decisions, so the interval
  cannot be estimated.
- `PROSPECTIVE_SUPPORT_INSUFFICIENT`: fewer than 30 prospective records. This is
  a corroboration warning, not the only admissible evidence; the forward floor's
  power curve is published with every run.
- `INTERVAL_CROSSES_ZERO` / `INTERVAL_UNESTIMABLE` / `INTERVAL_BELOW_ZERO`: the
  relative-skill claim is not supported, or is supported in the negative
  direction (the model is worse than the base rate and the report says so).

These bound claim language, not the arithmetic. A computable probability can be
reportable while the edge is unsupported; it must never be presented as a
supported direction.

## Failure-mode column

Every reason code carries `failure_mode ∈ {correctness, accounting, data,
reporting, presentation}`. Session-phase bookkeeping is `accounting`; a wrong
probability is `correctness`. Two findings in different modes must not share a
priority tier just because both are labelled P0.

## Claim boundaries

- Gross close-to-close returns; execution, spread, slippage, fees and market
  impact are not modelled.
- The 5D target excludes cash dividends; the one-sided label bias is disclosed,
  not corrected.
- Provider history is download-time revised, not point-in-time certified.
- No PBO, DSR, cross-sectional portfolio backtest or survivorship audit.
- News is independent evidence and never a re-scoring input.