# Consensus Protocol

## Single path, no ensemble

`consensus.aggregation_method` is `single_path` and
`production_path_count` is 1. The production probability is exactly the
eligible Research A estimate. A/B/C equal-weight means, Brier-skill weights,
ensemble Platt calibrators and heuristic confidence scores are removed.

| Path | Role | Effect on the published number |
|---|---|---|
| A `quant` | production | the published probability, when eligible |
| B `factor` | shadow | displayed separately; no vote, no fallback |
| C `ml` | shadow | displayed separately; no vote, no fallback |
| D `factor_backtest` | diagnostic | descriptive IC only; never a probability |

If A fails, abstains or is ineligible, `prob_up` stays null even when B/C have
numbers. A shadow path can never replace A.

## Five separate flags

The consensus publishes `computable`, `current_prediction_integrity_pass`,
`oos_integrity_pass`, `production_probability_eligible`,
`directional_edge_supported` and `trading_signal_eligible`. Do not collapse them
into one score and do not describe a computable estimate as production-ready.

Thresholds are fixed:

- current matching sample: at least 12 same-sign matured origins, 6 sessions apart;
- prefix replay: at least 12 valid non-overlapping predictions from the fixed
  exchange-session phase (anchor 2000-01-03, ordinal mod 6 == 0);
- prospective evidence: at least 30 truly prospective, non-overlapping, matured
  outcomes before any directional-edge claim;
- no sample-size scaling: a shorter listing or incomplete window reports the
  actual gap instead of borrowing a lower relative standard.

## Shadow isolation

Shadow results are copied into `shadow_predictions` with
`production_probability: false`. Their status, probability and model version may
be displayed, but they do not enter `prob_up`, `model_lean`,
`production_probability_eligible`, `directional_edge_supported` or any report
status. A shadow crash is isolated and never blocks A.

## What is not published

- no `confidence_score` (field is null and deprecated);
- no `predictive_evidence_grade` 0–100 substitute;
- no ensemble probability range presented as a confidence interval;
- no probability blending between Quant and News.

## Evidence blocks

`consensus.evidence` reports replay type, replay valid/non-overlapping counts,
abstentions, coverage, prospective count, required thresholds, calibration
status and the paired relative-skill intervals. These are the only inputs a
narrator may use for evidence language.