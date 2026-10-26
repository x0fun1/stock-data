# Probability Policy

Policy version: `fixed_52w_5d_v1`. Production estimator: `quant_drift_shrinkage_v2`.

## One production path

Only Research A produces the published probability. B and C are shadow research
candidates: they are reported separately, never vote, never fill in for a missing
A and cannot change its number. D is a descriptive 5-session diagnostic.

The production number answers one narrow question: the modelled probability that
the split-adjusted close five exchange sessions after the decision session is
above the decision-session close. It is not a calibrated probability, not a
confidence score and not a trading signal.

## Eligibility flags

| Flag | Meaning |
|---|---|
| `computable` | A produced a probability or a reasoned abstention |
| `current_prediction_integrity_pass` | the current probability is reproducible from frozen bars and the fixed transform parameters |
| `oos_integrity_pass` | every non-abstained replay record is reproducible and labels matured at its origin |
| `production_probability_eligible` | policy window/full calendar coverage, a resolved target session, the declared price basis, current integrity, replay integrity and at least 12 **effective** replay samples all hold |
| `directional_edge_supported` | the full prefix replay has at least 30 matured decisions and the paired-improvement interval excludes zero on the positive side |
| `trading_signal_eligible` | always `false` |

A failure of any production condition sets `prob_up = null`. The report must then
show `上涨概率：不可用` with the reason code and the actual gap.

`directional_edge_supported` is **three-valued** through `directional_edge_state`:

- `positive_skill_with_estimated_interval_excluding_zero` — a supported edge;
- `negative_skill_with_estimated_interval_excluding_zero` — the model is reliably
  *worse* than the base rate and the report says so;
- `insufficient_evidence` — the interval contains zero.

A probability may be reportable while the edge is unsupported. Those are different
claims and must never be merged into one headline.

## Estimator (v2)

Research A shrinks the causal base rate toward a volatility-calibrated drift prior:

```
p0    = up-rate of every matured 5D label available at the decision close
sigma = sample stdev of daily log returns over at most 250 sessions ending at t
p_vol = Phi( mu * H / (sigma * sqrt(H)) )        mu = 6%/yr, H = 5
n_eff = matured_label_count / H
p     = (n_eff * p0 + k * p_vol) / (n_eff + k)   k = 50
```

Why: measured on 15 real series (2018-2026), the previous estimator put about 68%
of its weight on the sign of 20-session momentum, which carried no 5-day
directional information; Brier skill against the causal base rate was negative on
15 of 15 series (mean −1.00%). Its loss matched the analytic noise penalty of that
weight almost exactly, so it was a structural loss, not bad luck. Shrinking the
base rate instead recovers that loss and adds a small drift term; the result was
positive on 15/15 series and 6/6 sub-periods, yet still only about +0.9% skill.
Report it as "not worse than the naive baseline", never as predictive power.

`k` and `mu` are policy constants, not fitted parameters. Skill moved only
0.18pp as `mu` varied from 0% to 15%, which is why the gain is variance
reduction rather than a bet on the drift assumption.

ROC20 is still computed and frozen, but only as a disclosed state variable and
matching-sample context. `feature.used_for_probability = false` is recorded in
the transform and the audit rejects any row that claims otherwise. The 12-match
floor is disclosure only; it no longer gates publication.

## Evidence vocabulary

- **prefix walk-forward replay**: within one frozen 52-week snapshot, every
  decision session is predicted using only its own past. It is the primary
  evidence base because it exists today; labels overlap, so the effective sample
  size is `valid rows / 5` and the interval uses moving blocks of at least
  `horizon + 1` sessions.
- **phase sampling** (anchor 2000-01-03, `ordinal % 6 == 0`) is a computation
  throttle only. It never gates eligibility and, when the supplied calendar does
  not contain the anchor, it is reported as `unanchored` and every row is
  evaluated instead of inventing a phase from the window's left edge.
- **prospective forward record**: a ledger prediction created before its label
  could be observed. It corroborates; it is no longer the only admissible
  evidence. The 30-outcome floor is retained, but its power is published
  (`evidence_power`): at a true 60% hit rate a 30-outcome test passes only 29% of
  the time, so the floor cannot be the sole gate without making the system
  unfalsifiable.
- **effectiveness block**: Brier skill against the causal base rate *and* against
  a constant 0.5, AUC, the paired bootstrap interval and the effective sample
  size. Reproducibility and effectiveness are separate verdicts and neither may
  stand in for the other.

## Fixed window and target

The window is `(decision_session - 364 calendar days, decision_session]`. No bar
outside it may enter features, matching samples, the base rate, factor selection,
ML training or calibration. A request that supplies a different window is
rejected with `FIXED_WINDOW_CONFLICT`.

The forecast horizon is exactly 5 exchange sessions. `1D` and `20D` remain
available only for descriptive or standalone factor research and can never be
promoted to a forecast.

The 5th-session target must be resolved from the frozen calendar. If it is not,
the run is vetoed with `TARGET_SESSION_UNRESOLVED`: a probability against an
undetermined close is not a 5-day forecast.

## Price and label basis

The target uses the provider close that is split-adjusted and excludes cash
dividend reinvestment. A dividend-adjusted `Adj Close` series is a different
target and is rejected with `PRICE_BASIS_UNVERIFIED`.

Excluding dividends is a one-sided bias: a window spanning an ex-dividend date
loses the payout, so a near-flat window can flip from up to down. On real data
the flip rate was under 1% of labels (WMT highest at 0.59%), so it does not
justify switching targets, but it must be disclosed. `target_price_basis`
records `dividend_adjusted: false`, and `label_basis` reports the flip rate as
`declared_not_quantified` when the snapshot carries no dividend history.

## Calibration

Calibration is disabled by policy. `calibrated_probability` is always null and
`probability_label` is `uncalibrated_model_estimate`. No 0–100 confidence score
is published; data completeness, replay support, directional edge and prospective
evidence are shown separately. The ML shadow path applies the same rule in its
replay and its live entry; its Platt fit is retained only as a diagnostic value.

## Versioned ledger groups

`ledger_group_key` includes the model version. The v2 estimator therefore starts a
new group automatically; records written by `quant_causal_shrinkage_v1` stay in
their own `legacy_k12` group and are never pooled with v2 when scoring skill.