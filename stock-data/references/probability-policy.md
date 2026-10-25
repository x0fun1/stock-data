# Probability Policy

Policy version: `fixed_52w_5d_v1`.

## One production path

Only Research A produces the published probability. B and C are shadow research
candidates: they are reported separately, never vote, never fill in for a missing
A and cannot change its number. D is a descriptive 5-session diagnostic.

The production number answers one narrow question: the modelled probability that
the split-adjusted close five exchange sessions after the decision session is
above the decision-session close. It is not a calibrated probability, not a
confidence score and not a trading signal.

## Eligibility is split into five flags

| Flag | Meaning |
|---|---|
| `computable` | A produced a probability or a reasoned abstention |
| `current_prediction_integrity_pass` | the current probability is reproducible from frozen bars and the fixed transform parameters |
| `oos_integrity_pass` | every non-abstained OOS record is reproducible and labels matured at its origin |
| `production_probability_eligible` | policy window/full calendar coverage, price basis, current integrity, OOS integrity and at least 12 valid non-overlapping OOS predictions all hold |
| `directional_edge_supported` | at least 30 truly prospective, non-overlapping, matured outcomes and the paired improvement interval is positive under three block lengths |
| `trading_signal_eligible` | always `false` |

A failure of any production condition sets `prob_up = null`. The report must then
show `上涨概率：不可用` with the reason code and the actual gap. A `computable`
estimate that is not production-eligible is not displayed as a direction.

## Estimator

Research A is a causal empirical-shrinkage estimator:

- state: sign of the 20-session rate of change (`roc20`); zero counts as non-negative;
- matching origins: same-sign, label matured by the decision close, accepted
  chronologically at least 6 sessions apart;
- base rate: every daily 5-session label matured by the decision close, including
  overlapping labels, because the base rate is a conditional input rather than a
  sample count;
- probability: `(wins + 12 * base_rate) / (matches + 12)`, with at least 12
  matches required;
- no fitted calibrator, no Platt scaling and no ensemble calibration.

The complete `probability_transform` (version, matching origins, matching label
ends, base-rate label origins and boundary, wins, observations, prior strength,
gap, horizon and policy/model/target versions) is frozen with every prediction
and every OOS row. The audit recomputes all of it; a rewritten sample, moved
label boundary, changed parameter or changed probability is rejected.

## Evidence vocabulary

- **prefix walk-forward replay**: within one frozen 52-week snapshot, every
  decision session is predicted using only its own past. These rows are
  retrospective and overlapping; the fixed 6-session sample is a conservative
  evaluation policy, not proof of independence.
- **prospective forward record**: a ledger prediction created before its label
  could be observed. Only these records can support `directional_edge_supported`.
  Imported/backfilled history is retrospective and is never counted as forward
  evidence.
- **relative skill**: paired squared-error improvement against the causal base
  rate at the same origin. The interval comes from a moving-block bootstrap and
  is a descriptive retrospective estimate, not a future coverage guarantee.

## Fixed window

The window is `(decision_session - 364 calendar days, decision_session]`. No bar
outside it may enter features, matching samples, the base rate, factor selection,
ML training or calibration. A request that supplies a different window is
rejected with `FIXED_WINDOW_CONFLICT`.

The forecast horizon is exactly 5 exchange sessions. `1D` and `20D` remain
available only for descriptive or standalone factor research and can never be
promoted to a forecast.

## Price basis

The target uses the provider close that is split-adjusted and excludes cash
dividend reinvestment. A dividend-adjusted `Adj Close` series is a different
target and is rejected with `PRICE_BASIS_UNVERIFIED`. The snapshot records the
basis source and the historical availability/revision disclosure.

## Calibration

Calibration is disabled by policy. `calibrated_probability` is always null and
`probability_label` is `uncalibrated_model_estimate`. No 0–100 confidence score
is published; data completeness, OOS support, directional edge and prospective
evidence are shown separately.