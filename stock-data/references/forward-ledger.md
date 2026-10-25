# Forward Ledger

Schema `ledger-3.0`. The ledger is append-only and lives outside the run
directory, so `cleanup-run` cannot delete forward records. Legacy 1.0 and 2.0
lines remain readable for audit but are never counted as prospective evidence.

## Grouping

Every prediction carries:

- `group_key` = ticker | market | policy version | model version | feature
  version | target version;
- `policy_version`, `model_version`, `feature_version`, `target_version`;
- `probability_transform`, `causal_base_rate` and `decision_sampled`.

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

`directional_edge_supported` needs at least 30 prospective non-overlapping
matured records and a positive paired-improvement interval for block lengths 2,
1 and 3. The sampled records are correlated; the interval is descriptive, not a
future coverage guarantee.