# Prediction Eligibility, Research Audit, and Reporting Gate

This reference defines the one centralized gate policy. Data-computation eligibility, forecast validation, strategy evidence, and user-facing reporting are separate concerns. A nonfatal limitation must never erase a probability that a valid Quant path actually produced.

## Six layers

1. **Acquisition** — retain normalized OHLCV, timestamps, units, source identity and any independent same-session close observations.
2. **Data integrity** — validate OHLCV structure, source/time identity, latest-observation freshness, calendar resolution, adjustment basis and corporate-action evidence.
3. **Quant prediction** — after fatal integrity checks pass, attempt all selected prediction-capable A/B/C paths; D remains a diagnostic. Continue despite nonfatal calendar, adjustment, OOS, calibration or strategy-audit limitations.
4. **Prediction validation** — assess chronological OOS support, calibration, leakage and factor evidence. Insufficient validation lowers confidence; *detected* leakage or invalid target construction vetoes the affected prediction.
5. **Research/strategy audit** — assess PBO, DSR, costs, turnover and portfolio backtest. These determine whether a probability may support a strategy/profitability claim; missing evidence does not cancel a single-horizon probability.
6. **Reporting gate** — return `pass`, `degraded`, or `veto`, with reason codes. News runs after Quant/audit and can change integrated conviction/context, never Quant probability.

## Eligibility objects

```yaml
prediction_eligibility:
  status: eligible | blocked
  blockers: [fatal reason codes]

reporting_eligibility:
  status: pass | degraded | veto
  warnings: [reason codes]

strategy_eligibility:
  status: validated | not_validated
  required_for_probability: false
  required_for_strategy_claim: true
```

`forecast_eligible` remains a compatibility alias for `prediction_eligibility.status == eligible`; it does not mean the report is high-confidence. `reporting_status` is the user-facing gate.

## VETO policy

Only explicit, prediction-invalidating cases veto a probability:

- insufficient usable OHLCV for the minimum predeclared feature window;
- severe missing sessions confirmed by a resolved calendar, unrecoverable order/duplicate/price corruption, or invalid source identity/time/units;
- stale data or a latest observation that cannot be identified;
- conflicting same-session same-currency closes from independent providers (price mismatch greater than 0.3%);
- confirmed in-window split, reverse split, spin-off or major adjustment discontinuity while the input price series remains unadjusted;
- detected look-ahead/target leakage or invalid target construction;
- no valid quantitative path generated a finite probability;
- an explicit request that does not authorize a directional prediction.

A malformed/suspicious auxiliary calendar or corporate-action evidence is not itself proof of contaminated OHLCV. If the OHLCV sequence is otherwise usable, discard that auxiliary evidence, use observation fallback where possible, mark the gap and lower confidence. Do not silently convert a fatal input conflict into a warning.

## Nonfatal by default

Calendar/adjustment/OOS/calibration/path-validation deficiencies produce `degraded` reporting and confidence penalties, not veto. Strategy-only gaps (PBO, DSR, costs, turnover, portfolio backtest) remain explicit warnings and set `strategy_eligibility=not_validated`; they do not downgrade a probability-only `reporting_status` or cancel a single-horizon estimate. Do not make or imply strategy-profitability claims without them.

`unknown != contaminated`; short-horizon RSI/MACD/momentum/ATR/volatility/volume and raw-direction paths may run without known adjustment convention, with a confidence penalty. Total-return, dividend and long-horizon-return work must require appropriate adjusted evidence. `factor_backtest` is supporting validation, not a probability gate, unless its audit detects leakage or an invalid target.

## Calendar resolution and horizon semantics

Resolution order:

1. complete source-supplied exchange schedule (`source_supplied_schedule`);
2. schedule from an already installed `exchange_calendars` or `pandas_market_calendars` (`library_verified`), pinned in the frozen manifest with source/version and coverage;
3. ordered unique dates in normalized daily OHLCV (`observation_fallback`);
4. invalid if dates are corrupt or clearly anomalous.

Do not install packages or access the network to resolve a calendar. The library schedule is frozen with the snapshot; reload must not change based on a later package install. A complete verified calendar is compared to the OHLCV axis; a confirmed missing bar is fatal. With observation fallback, historical labels are explicitly `h` observed bars forward, not proven exchange sessions; future horizon is `estimated_observed_sessions`, confidence is lowered, and holiday/session completeness remains unknown. Weekend observations and unexplained >14-calendar-day gaps are fatal sanity failures. Missing official calendar alone is never a veto.

## Latest close and adjustment assessment

Cross-source evidence uses only normalized same-session daily-close observations with matching currency. It reports `cross_source_verified` when at least two distinct providers agree within 0.3%; one valid provider is `provider_verified`; calendar-backed close evidence can be `calendar_verified`. A same-session conflict is fatal. Calendar availability is not a prerequisite for price-source matching. Provider names are source claims, not cryptographic authentication.

Keep distinct:

```yaml
adjustment_status: verified | provider_declared | unknown
corporate_action_status: clear | recent_action_detected | unavailable
```

Only `recent_action_detected + unadjusted prices` is a corporate-action contamination veto. Evidence not covering the latest bar remains unavailable, not “clear.”

## Probability, confidence and output

Keep separate:

```yaml
prediction:
  status: available | unavailable
  raw_probability: 0.0-1.0 | null
  calibrated_probability: 0.0-1.0 | null
  reportable_probability: 0.0-1.0 | null
  probability_basis: path/ensemble method
  calibration_status: calibrated | partial | unavailable
  reporting_status: pass | degraded | veto
```

An equal-weight path mean is not an independently calibrated ensemble. `calibrated_probability` stays null until a distinct, valid ensemble calibration exists; report the reportable mean and its basis clearly. On a fatal VETO, withhold raw/reportable probabilities from the user-facing response and preserve diagnostics only in the debug artifact. News cannot create or adjust a probability.

Confidence is an explainable score, not a gate: start at 100; deduct for calendar fallback (-10), unknown adjustment (-10), single provider (-10), insufficient/weak OOS (-20), uncalibrated probability (-20), low evidence-family agreement (-10), insufficient history (-20), then clamp to 0–100. Labels: 80–100 high, 60–79 medium, 40–59 low, below 40 very low. A low score never independently vetoes. Report key causes, not only the label.

## Reason codes

Code—not prose matching—controls policy. The central catalog is `scripts/quant_research/gate_policy.py`; unknown codes fail closed. Core examples:

| Code | Severity | Scope | Veto prediction? |
|---|---|---|---|
| `INSUFFICIENT_OHLCV`, `SEVERE_MISSING_BARS`, `INVALID_TIME_ORDER`, `INVALID_PRICE_VALUES` | fatal | prediction/data | yes |
| `LATEST_PRICE_CONFLICT`, `LATEST_OBSERVATION_UNRESOLVED` | fatal | data | yes |
| `CORPORATE_ACTION_CONTAMINATION`, `LEAKAGE_DETECTED`, `PREDICTION_FAILED` | fatal | prediction | yes |
| `CALENDAR_OFFICIAL_MISSING`, `CALENDAR_FALLBACK_USED`, `ADJUSTMENT_UNKNOWN` | degraded | data | no |
| `OOS_INSUFFICIENT`, `CALIBRATION_MISSING`, `FACTOR_BACKTEST_INSUFFICIENT` | degraded/warning | validation | no |
| `PBO_NOT_ASSESSED`, `DSR_NOT_ASSESSED`, `TRANSACTION_COST_NOT_ASSESSED`, `PORTFOLIO_BACKTEST_MISSING` | warning | strategy | no |
| `LATEST_CLOSE_CROSS_VERIFIED` | info | data | no |

## Report levels

- **Summary (default chat):** probability, direction, confidence/score, `PASS/DEGRADED/VETO`, core evidence, concise limitations, news state/events, integrated risk. No raw manifests or gate internals.
- **Standard (`report.md`):** data quality, OOS/calibration, factors, audit scope, corporate actions, news and cross-signal detail.
- **Debug (`report.json` and raw diagnostics):** source/calendar evidence, reason codes, artifact digests, capture receipts, per-path output and gate details.

`aggregate_gate_telemetry()` records `prediction_availability_rate`, `abstain_rate`, `degraded_rate`, and `pass_rate` over caller-supplied analysis rows. The per-analysis report marks its sample count as one and explicitly does not claim a batch reliability statistic. Aggregate a meaningful fixed universe (for example, liquid US large caps) before applying any operational abstain-rate threshold; do not infer system-wide performance from one symbol or a synthetic test.

## Required regression cases

1. Complete OHLCV/calendar/known adjustment → PASS, probability available.
2. Complete OHLCV + calendar fallback + unknown adjustment → DEGRADED, Quant runs and probability available.
3. PBO/DSR/portfolio evidence absent → probability remains available; strategy eligibility is not validated.
4. Same-session independent closes agree within tolerance with official calendar absent → `cross_source_verified`.
5. Confirmed split/spin-off + unadjusted OHLCV → VETO.
6. Detected look-ahead leakage → VETO.
7. Valid model output with calibration unavailable → preserve raw probability, lower confidence.
8. Invalid/latest price conflict → VETO.
9. Low confidence alone → still report a degraded probability, never VETO.
10. Strict analysis mode with OOS/calibration gaps → still run Quant; mark reporting DEGRADED.