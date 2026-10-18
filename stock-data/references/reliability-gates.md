# Prediction Eligibility, Research Audit, and Reporting Gate

This reference defines the one centralized gate policy. Data-computation eligibility, forecast validation, strategy evidence, and user-facing reporting are separate concerns. A nonfatal limitation must never erase a probability that a valid Quant path actually produced.

## Six layers

1. **Acquisition** — retain normalized OHLCV, timestamps, units, source identity and any independent same-session close observations.
2. **Data integrity** — validate OHLCV structure, source/time identity, latest-observation freshness, calendar resolution, adjustment basis and corporate-action evidence.
3. **Quant prediction** — after fatal integrity checks pass, attempt all selected prediction-capable A/B/C paths; D remains a diagnostic. Continue despite nonfatal calendar, adjustment, OOS, calibration or strategy-audit limitations.
4. **Prediction validation** — assess chronological OOS support, calibration, leakage and factor evidence; self-reported OOS metrics are cross-checked against values recomputed from the same validated rows. Insufficient validation lowers confidence; *detected* leakage or invalid target construction vetoes the affected prediction.
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
- severe missing sessions confirmed by a resolved calendar, unrecoverable order/duplicate/price corruption, or contradicting source identity/time/unit values;
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

A daily bar's source timestamp labels the period it covers, so providers stamp either the session close or the period start (for example a 13:30Z open for a 20:00Z close). The latest observation stays resolved when the label falls inside the bar's own one-day coverage window; a fetch time before the session close, or a bar older than that window, still triggers `LATEST_OBSERVATION_UNRESOLVED`. Only a timestamp that reconciles with the session may be promoted to `calendar_verified`; the check never upgrades a bar whose label is missing or conflicts.

Missing `currency`/`unit` provenance (for example a Yahoo bar whose trade unit the source never confirms) is a degraded confidence marker (`MARKET_UNITS_UNVERIFIED`), not a veto; do not infer the unit from the currency. A malformed or contradicting `currency`/`unit` value remains a source-integrity failure (`MARKET_UNITS_CONFLICT`, fatal veto).

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

An equal-weight path mean is not automatically an independently calibrated ensemble. `calibrated_probability` stays null until a distinct, valid ensemble calibration exists; when the chronological-holdout-validated ensemble calibrator passes, `calibrated_probability` and `reportable_probability` carry the calibrated value, `raw_probability` keeps the equal-weight mean, and the calibration basis is reported. Otherwise report the reportable mean and its basis clearly. On a fatal VETO, withhold raw/reportable probabilities from the user-facing response and preserve diagnostics only in the debug artifact. News cannot create or adjust a probability.

Confidence is an explainable heuristic score, not a gate, and is computed in two documented stages. The frozen snapshot's data-quality score starts at 100 and deducts a proportional history-depth penalty relative to a one-year session base (252 sessions; up to -20) plus fixed penalties for calendar fallback (-10), unknown adjustment (-10), unconfirmed corporate actions (-5), a single-provider latest close (-5), unreconciled extreme moves (-10) and unverified market units (-5); a blocked prediction clamps it to at most 39. The consensus stage then blends five dimensions into the reported score with fixed weights: data integrity 0.20, OOS validation support 0.25, policy-replay skill 0.25, calibration 0.15, consensus robustness 0.15. Each dimension scores 0–100, so closed gaps earn full credit: OOS support maps per-path status to 100/80/45/20 points (strong/acceptable/weak/insufficient) and averages over forecast paths, with thresholds scaled to the widest non-overlapping ledger the window actually holds: `strong` is ~90% of that budget (floor 20), `acceptable` ~55% and `weak` ~30% of the target, each capped at the budget so no tier is unreachable, while a ledger longer than the nominal 252-session window never lowers the bar. Scaling applies only once that ledger covers at least three quarters of the nominal window, which is the holiday and label-tail shortfall of a one-year study; a materially shorter study (for example a recent listing that cannot supply a year of history) keeps the nominal tiers and reports insufficient support, because the relative standard would otherwise award `strong` on a handful of samples. Per-path grading and the composite read the same ledger ceiling (nominal 38/20/11 at 5D; a 52-week ledger of 220 sessions gives 33/18/9); policy replay scores 100 for a positive skill with an interval excluding zero, 65 when the interval is unestimated or includes zero, 20 when evidence is insufficient to grade, 15 for a nonpositive point estimate, and 10 for an interval entirely below the base rate; calibration scores 40 + 45×(calibrated path fraction) + 15 when the ensemble calibrator improved the subsequent-ledger check; robustness blends path agreement (100/80/45/20) and evidence-family diversity (100/85/60) at 0.75/0.25. Caps keep labels honest: a single usable path caps the score at 74, an interval entirely below the base rate at 64, and a nonpositive point estimate at 74. Leakage gaps (-15) and invalid OOS evidence (-10) reduce the OOS dimension. Exact point values live in `snapshot.py` and `consensus.py`. Labels: 80–100 high, 60–79 medium, 40–59 low, below 40 very low; `confidence_score_kind` marks this as heuristic research support, not predictive accuracy. A low score never independently vetoes. Report key causes, not only the label.

## Reason codes

Code—not prose matching—controls policy. The central catalog is `scripts/quant_research/gate_policy.py`; unknown codes fail closed. Core examples:

| Code | Severity | Scope | Veto prediction? |
|---|---|---|---|
| `INSUFFICIENT_OHLCV`, `SEVERE_MISSING_BARS`, `INVALID_TIME_ORDER`, `INVALID_PRICE_VALUES` | fatal | prediction/data | yes |
| `LATEST_PRICE_CONFLICT`, `MARKET_UNITS_CONFLICT`, `LATEST_OBSERVATION_UNRESOLVED` | fatal | data | yes |
| `CORPORATE_ACTION_CONTAMINATION`, `LEAKAGE_DETECTED`, `PREDICTION_FAILED` | fatal | prediction | yes |
| `CALENDAR_OFFICIAL_MISSING`, `CALENDAR_FALLBACK_USED`, `ADJUSTMENT_UNKNOWN`, `MARKET_UNITS_UNVERIFIED` | degraded | data | no |
| `OOS_INSUFFICIENT`, `OOS_SELF_REPORT_MISMATCH`, `CALIBRATION_MISSING`, `FACTOR_BACKTEST_INSUFFICIENT` | degraded/warning | validation | no |
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

These cases are the acceptance checklist for this runtime; the slim package does not ship an in-repo test suite, so they are exercised by separately maintained offline regression probes rather than repository tests.
