# Research B selection-rule comparison

- pool: 3 symbols (2 equities, 1 ETFs), shared calendar
- horizon: 5D; block length: 6 sessions
- scored pairs: arm A 18, arm B 18
- Brier arm A (spaced point estimate): 0.27565814613377826
- Brier arm B (overlapping block): 0.28134641161012747
- mean per-session delta (B - A): 0.005688265476349233
- session-block 95% interval: None (insufficient_samples)
- **verdict: indeterminate**

Positive delta means the new rule scored worse. The interval is the decision input;
the point estimate is reported for completeness only.

## Preregistration

```json
{
  "arms": {
    "A": "non_overlapping_point",
    "B": "overlapping_block"
  },
  "bootstrap": {
    "block_length": "horizon + 1",
    "replicates": 500,
    "seed": 0,
    "unit": "session_block"
  },
  "coverage": "reported, never used to discard hard sessions from the score comparison",
  "decision": {
    "A_better": "interval entirely above zero",
    "B_better": "interval entirely below zero",
    "indistinguishable": "interval contains zero (the expected outcome on this sample)"
  },
  "default_if_indistinguishable": "keep the incumbent rule",
  "horizon": "5D",
  "pairing": "decision_session, only sessions where both arms produced a prediction",
  "primary_metric": "mean_per_session_brier_delta_B_minus_A",
  "question": "Does dependency-aware factor selection (overlapping labels with a moving-block interval plus a direction-stability requirement) change out-of-sample Brier versus the spaced point-estimate rule?"
}
```
