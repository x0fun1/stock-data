# Reliability Gates

The gate answers five separate questions. No single 0–100 score is published.

| Flag | Positive condition | Failure action |
|---|---|---|
| `computable` | A returned a probability or a reasoned abstention | report `上涨概率：不可用` with the reason |
| `current_prediction_integrity_pass` | current probability replays exactly from frozen bars and fixed parameters | veto the probability |
| `oos_integrity_pass` | every non-abstained OOS row replays exactly; every label matured by its origin | veto the probability |
| `production_probability_eligible` | policy version, complete 52-week coverage, verified exchange calendar, split-adjusted basis, current integrity, OOS integrity, ≥12 valid non-overlapping OOS predictions, ≥12 current matches | `prob_up = null`; no formal probability |
| `directional_edge_supported` | ≥30 truly prospective non-overlapping matured outcomes and the paired improvement interval is positive for block lengths 2, 1 and 3 | `false`; no edge claim |
| `trading_signal_eligible` | never in this version | always `false` |

## Data gates

`INSUFFICIENT_HISTORY_COVERAGE`, `PRICE_BASIS_UNVERIFIED`,
`FIXED_WINDOW_CONFLICT`, `OHLCV_OUT_OF_WINDOW` and `DECISION_SESSION_UNRESOLVED`
prevent a formal probability. `CALENDAR_UNVERIFIED` permits diagnostics only.
A calendar fallback based on observed bars is not a verified session axis and
cannot certify the 5th-session target.

Structural violations are rejected at freeze time. A blocked snapshot may still
exist for diagnosis, but it cannot publish a probability.

## Evidence gates

- `OOS_SUPPORT_INSUFFICIENT`: fewer than 12 valid non-overlapping prefix
  predictions.
- `NO_VALID_OOS_PROBABILITY`: no non-abstained OOS probability.
- `OOS_INTEGRITY_FAILED`: at least one row failed transform/sample/label replay.
- `PROSPECTIVE_SUPPORT_INSUFFICIENT`: fewer than 30 prospective records.
- `DIRECTIONAL_EDGE_UNPROVEN` / `INTERVAL_CROSSES_ZERO` / `INTERVAL_UNESTIMABLE`:
  the relative-skill claim is not supported.

These bound claim language, not the arithmetic. A computable estimate with
insufficient evidence is `prob_up = null` under this policy; it is never shown as
a lower-evidence direction.

## Claim boundaries

- Gross close-to-close returns; execution, spread, slippage, fees and market
  impact are not modelled.
- Provider history is download-time revised, not point-in-time certified.
- No PBO, DSR, cross-sectional portfolio backtest or survivorship audit.
- News is independent evidence and never a re-scoring input.