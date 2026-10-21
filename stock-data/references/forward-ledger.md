# Forward Prediction Ledger

The ledger is the only mechanism in this Skill that can produce a genuinely independent
out-of-sample record: every frozen prediction is written before its label exists, and a
later settlement event scores it against realized closes. Backtests cannot substitute for
this, because their model and feature choices are already informed by the same history.

## Storage

- Append-only JSONL, one event per line, UTF-8.
- Lives **outside** the run directory. `cleanup-run` deletes run JSON; the ledger must
  survive that cleanup.
- Default location is a host choice (for example `runtime/ledger/predictions.jsonl`); every
  command takes `--ledger <path>` explicitly.
- No command edits or deletes a line. A settlement is a second event referencing the
  prediction id.

## Events

### `prediction`

Written once per frozen analysis when `--ledger` is supplied to `analyze`/`pipeline`, or by
`ledger-record`.

| Field | Meaning |
|---|---|
| `prediction_id` | sha256 of ticker, market, horizon, as-of, snapshot content hash, engine version and probability |
| `ticker`, `market`, `horizon`, `horizon_sessions` | identity |
| `asof_timestamp`, `snapshot_id`, `snapshot_content_sha256` | frozen inputs |
| `decision_session`, `decision_at` | the confirmed close the forecast used |
| `label_basis` | always `close_to_close` |
| `entry_basis` | `not_modelled` |
| `prob_up`, `raw_probability`, `calibrated_probability`, `probability_basis` | reported forecast |
| `direction`, `confidence`, `confidence_score`, `predictive_evidence_grade` | reported diagnostics |
| `eligibility` | probability/direction/trading booleans |
| `engine_version` | runtime version that produced the forecast |
| `status` | `pending`, or `abstained` when no probability was reportable |

`prediction_id` is deterministic: re-running the same snapshot with the same engine version
and probability is a duplicate and writes nothing. A different engine version or a changed
probability is a different prediction and is recorded separately.

### `settlement`

Written by `ledger-settle` when a later snapshot contains the label session.

| Field | Meaning |
|---|---|
| `prediction_id` | the prediction being scored |
| `label_session` | the session whose close realized the label |
| `realized_return` | `close[label] / close[decision] - 1` |
| `hit` | `1` if `realized_return > 0`, else `0` |
| `brier` | `(prob_up - hit)^2` |
| `source_snapshot_id` | the snapshot that supplied the label |

Settlement is idempotent: an existing settlement for the prediction id is never appended
again. A prediction whose label is not yet in the snapshot is reported as
`label_not_matured`, not treated as a failure.

## Commands

```bash
# Record after an analysis (or pass --ledger to analyze/pipeline directly)
quant_research.py ledger-record --analysis-dir <analysis_dir> --ledger <ledger.jsonl>

# Settle against a later frozen snapshot
quant_research.py ledger-settle --ledger <ledger.jsonl> --snapshot-dir <snapshot_dir> \
  [--ticker INTC] [--horizon 5D]

# Score realized predictions
quant_research.py ledger-audit --ledger <ledger.jsonl>
```

`ledger-audit` reports unique/settled/pending/abstained counts, duplicate settlements,
duplicate prediction dates, overall Brier / base-rate Brier / skill / hit rate, and the same
metrics by horizon and engine version. A positive skill in this report is the first result
that can be called forward evidence; a Brier below the base-rate Brier is not implied.

## Rules

- Record **before** delivery or cleanup; the ledger is not a place to backfill history.
- Never merge settlement events into the prediction line.
- Never score a prediction whose label session is after the snapshot's last bar.
- An abstention is recorded; excluding it would hide coverage loss.
- The ledger is not a trading record: `entry_basis` stays `not_modelled` and no costs are
  applied.