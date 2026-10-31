# Research B candidate comparison (pre-registered)

- pool: 3 symbols, horizon 5D, block length 6 sessions
- reference arm: production (legacy selection, three buckets)

| question | candidate | sessions | mean delta (Brier) | 95% interval | verdict |
| --- | --- | --- | --- | --- | --- |
| Q1_selection | restricted_selection_three | 2 | -0.0688 | n/a | indeterminate |
| Q2_bucket | legacy_two_bucket | 2 | +0.0302 | n/a | indeterminate |

Interval needs at least 24 scorable sessions; below that the verdict is indeterminate by rule.

Positive delta means the candidate scored worse than production. Production is unchanged either way.

## Preregistration

```json
{
  "arms": {
    "legacy_two_bucket": {
      "bucket_mode": "two",
      "selection_profile": "legacy"
    },
    "production_legacy_three": {
      "bucket_mode": "three",
      "selection_profile": "legacy"
    },
    "restricted_selection_three": {
      "bucket_mode": "three",
      "selection_profile": "restricted"
    },
    "restricted_two_bucket": {
      "bucket_mode": "two",
      "selection_profile": "restricted"
    }
  },
  "bootstrap": {
    "block_length": "horizon + 1",
    "replicates": 500,
    "seed": 0,
    "unit": "session_block"
  },
  "decision": {
    "candidate_better": "whole interval below zero",
    "candidate_worse": "whole interval above zero",
    "indeterminate": "interval contains zero, or too few scorable sessions"
  },
  "metric": "mean_paired_per_session_brier_delta_candidate_minus_reference",
  "minimum_scorable_sessions_for_interval": 24,
  "protocol": "research-b-candidates-v1",
  "questions": {
    "Q1_selection": [
      "production_legacy_three",
      "restricted_selection_three"
    ],
    "Q2_bucket": [
      "production_legacy_three",
      "legacy_two_bucket"
    ]
  },
  "reference_arm": "production_legacy_three",
  "registered": "2026-10-19",
  "switch_policy": "no switch this run; a switch requires a pre-registered re-run that clears the rule",
  "thresholds_are_not_adjusted_after_seeing_results": true
}
```
