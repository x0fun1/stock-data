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

## Separate flags

The consensus publishes `computable`, `current_prediction_integrity_pass`,
`oos_integrity_pass`, `production_probability_eligible`,
`directional_edge_supported` (with its three-valued `directional_edge_state`) and
`trading_signal_eligible`. Do not collapse them into one score and do not
describe a computable estimate as production-ready.

Thresholds are fixed:

- target session: the 5th session must be resolved from the frozen calendar,
  otherwise `TARGET_SESSION_UNRESOLVED` vetoes the probability;
- prefix replay: at least 12 **effective** samples, where effective =
  valid rows / 5 because 5-session labels overlap. The old phase-sampled count
  (anchor 2000-01-03, ordinal mod 6 == 0) is a throttle only and never gates
  eligibility; an unanchored phase evaluates every row;
- directional edge: at least 30 matured replay decisions and a paired-improvement
  interval excluding zero on the positive side. `negative_skill_...` is a
  first-class outcome and must be reported as "无（劣于纯基率）";
- prospective evidence: 30 truly prospective, non-overlapping, matured outcomes
  are required for forward corroboration, and the floor's power curve is
  published with the run;
- current matching sample: disclosed (12 same-sign matured origins, 6 sessions
  apart) but **not** an eligibility condition — the matching hit rate carries no
  probability weight in v2;
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
