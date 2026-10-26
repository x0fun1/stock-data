# Audit Protocol

Procedure version: `audit-protocol-1.0`. Applies to any audit or review of a
forecast produced by this Skill, and to the audit of the audit itself.

The rules below exist because a previous audit passed 40 self-checks, reported
zero lint findings and still certified a model that lost to the naive base rate on
15 of 15 real series. The failure was not in the arithmetic; it was in what the
audit agreed to look at.

## Mandatory shape of an audit report

Every audit report must contain, in this order:

1. **Conclusion with skill numbers** (`R1`). The first paragraph states Brier
   skill against the causal base rate and against a constant 0.5, the AUC, and
   the interval. An audit without an effectiveness section may not use the words
   "deliverable", "approved" or "pass".
2. **Two independent scores** (`R2`): reproducibility (can the number be rebuilt
   bit-for-bit?) and effectiveness (is the number better than a naive baseline?).
   They are orthogonal. A in one and D in the other is a valid outcome and must be
   shown as two separate rows, never averaged into one grade.
3. **Version anchor** (`R13`): `policy_version`, `model_version`,
   `feature_version`, `target_version`. Conclusions are valid only for those
   versions.
4. **Evidence source per conclusion** (`R8`): `real_market`, `synthetic`,
   `derived`, or `not_executed`. Synthetic data may support conclusions about
   *process* only, never about *effectiveness*.
5. **Failure-mode column** (`R7`) on every finding: `correctness`, `accounting`,
   `data`, `reporting`, `presentation`. Session-phase bookkeeping is `accounting`
   and must not be ranked with "the probability is computed wrong" (`correctness`).
6. **Effective sample size** (`R5`) next to every accuracy or skill claim:
   `n_overlapping`, `n_effective = n / horizon`, base-rate standard error, and
   the share of weight the estimator puts on its prior. Any claim resting on
   `n_effective < 30` may not use "proves", "confirms" or "validated".
7. **Power of any evidence floor** (`R6`). For a binomial floor, publish the pass
   probability at the plausible true rates. A 30-outcome floor at a true 60% hit
   rate passes 29% of the time; that number belongs in the report, not in a
   footnote.
8. **Calendar provenance** (`R4`): `source_supplied_schedule`, `library_verified`,
   `observed_ohlcv`, `synthetic_weekday`, or `not_executed`. A weekday count may
   never be described as a real exchange calendar, and `expected_sessions` may
   never be hardcoded.
9. **Label basis** (`R9`): split adjustment, dividend treatment, point-in-time
   availability lag, and the direction of any resulting label bias.
10. **Remediation mapping** (`R10`): every "structural limitation" sentence must
    carry an item number in the remediation list. A limitation with no item is a
    disclaimer, not a finding.
11. **Reproducibility package** (`R12`): scripts, data snapshot and random seeds
    sufficient for a third party to reproduce every number.
12. **Status vocabulary** (`R11`): `observed`, `synthetic`, `not_executed`.
    Do not write "real" about a source that was not executed or observed.

## Independent verification (`R3`)

The verification of a probability transform must not call the producer's
function. A shared implementation can only detect tampering with the record; a
wrong formula is reproduced identically on both sides and passes.

In this repository the auditor re-implements the transform in
`validation/probability_quality.py` (`_recompute_v2`, `_recompute_legacy_v1`)
and deliberately imports nothing from `researchers/`.

Verification is only credible if it fails on a mutated implementation. Three
mutations are mandatory and are executed by `selfcheck` (`T29`):

| Mutation | Expected detection |
|---|---|
| shrink weight `k` set to 0 | `PRIOR_STRENGTH_TAMPERED` |
| probability flipped (`p -> 1 - p`) | `PROBABILITY_TAMPERED` |
| base-rate label boundary moved one session | `BASE_RATE_LABEL_BOUNDARY_TAMPERED` |

A mutation that is not detected is a defect in the audit, not a curiosity.

## Effectiveness block (`R1`)

Every probability audit reports:

- `brier_model`, `brier_causal_base_rate`, `brier_constant_half`;
- `skill.vs_causal_base_rate` and `skill.vs_constant_half`;
- `auc` with tie handling;
- a moving-block bootstrap interval on the paired improvement, with the primary
  block length at least `horizon + 1`; a shorter block treats overlapping labels
  as independent and understates the interval;
- `effective_sample_size`, `base_rate_standard_error`;
- a three-state verdict: positive, negative or insufficient. **Negative must be
  reportable**: a model that is reliably worse than the base rate is a finding,
  not a missing value.

## Effective sample size (`R5`)

With 5-session labels on a 250-session window there are about 250 overlapping
rows but only about 50 independent equivalents. Every statement of the form
"15 of 21 wins" must be accompanied by the paired interval and by the base-rate
standard error; otherwise the binomial noise is presented as skill.

## Evidence floors (`R6`)

Any floor (`n` outcomes, "three positive intervals", "12 non-overlapping OOS
predictions") must be published with its power curve. A floor that no realistic
sample can pass does not protect quality; it makes the system unfalsifiable. The
current design therefore treats the full prefix replay as primary evidence,
keeps the prospective ledger as corroboration, and publishes the curve.

## Re-audit checklist

- [ ] conclusion quotes skill against both baselines
- [ ] reproducibility and effectiveness scored separately
- [ ] version anchor present
- [ ] calendar provenance named; session count not hardcoded
- [ ] effective sample size and prior weight disclosed
- [ ] power curve attached to every floor
- [ ] failure mode labelled on every finding
- [ ] label basis (dividends, splits, PIT lag) checked
- [ ] every limitation mapped to a remediation item
- [ ] mutation tests executed and reported
- [ ] package (scripts, data, seeds) attached