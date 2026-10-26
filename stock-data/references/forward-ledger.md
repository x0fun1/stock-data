# Forward Ledger

Schema `ledger-3.0`. The ledger is append-only and lives outside the run
directory, so `cleanup-run` cannot delete forward records. Legacy 1.0 and 2.0
lines remain readable for audit but are never counted as prospective evidence.

## Grouping

Every prediction carries:

- `group_key` = ticker | market | policy version | model version | feature
  version | target version;
- `policy_version`, `model_version`, `feature_version`, `target_version`;
- `probability_transform`, `causal_base_rate` and `decision_sampled`;
- `sampling` (`status`: `anchored` / `unanchored`, `role`:
  `throttle_only_not_qualification`). A `decision_sampled = null` means the phase
  could not be anchored, **not** that the row was rejected.

Because the model version is part of `group_key`, v2 predictions never pool with
`quant_causal_shrinkage_v1` records; the older rows are reported as a separate
`legacy_k12` group and are not scored as evidence for the current model.

Only the production path A is recorded. Shadow probabilities are not ledger
predictions and cannot be scored as if they were.

## Prospective vs retrospective

`prospective_eligible` is true only when `recorded_at < label maturity`. A
historical snapshot imported later, or a prediction recorded after the fifth
session had closed, is retrospective; it is reported separately and excluded
from `prospective_records()`. Imported history can never become prospective.

The same `group_key` + decision session is recorded once. Re-running the same
decision returns `duplicate` instead of creating a second scoreable forecast,
even when the snapshot capture timestamp differs.

## Settlement

Settlement uses the fifth exchange session after the decision on the verified
calendar, not "five bars that happen to exist". A snapshot with incomplete
session coverage cannot settle a prediction. Settlement rows retain the decision
session, label session, entry/exit close, realised return, Brier score and the
causal base rate frozen at prediction time (or reconstructed at settlement with
an explicit marker).

## Reporting window

The main forward report evaluates only predictions whose decision session falls
in the most recent 364 calendar days and whose labels have matured. Older and
retrospective rows are shown separately for audit; they do not support a current
directional-edge claim.

## Minimum evidence

`directional_edge_supported` is decided primarily by the full prefix replay: at
least 30 matured decisions and a paired-improvement interval whose lower bound is
above zero, with the primary block length at least `horizon + 1` (6 sessions for
5D). Shorter blocks treat overlapping labels as independent and understate the
interval. The interval is descriptive, not a future coverage guarantee.

The 30-record prospective floor is retained as corroboration, but its power is
published with every run (`evidence_power`): at a true 60% hit rate a 30-outcome
test passes only 29% of the time. A floor that is very unlikely to be reached is
not a quality guarantee, so the forward ledger alone is no longer the only
admissible evidence. `prospective_records()` still excludes retrospective rows
from every forward claim.
