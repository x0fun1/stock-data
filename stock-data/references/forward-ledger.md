# Forward Prediction Ledger

The ledger is the only mechanism in this Skill that can produce a genuinely independent
out-of-sample record: a prospective prediction is written before its label exists, and a
later settlement event scores it against realized closes. Backtests cannot substitute for
this, because their model and feature choices are already informed by the same history.

Schema 2.0 separates three ideas that the old ledger conflated: whether a record is really
prospective, what the realised base rate means, and which baseline the Brier score is
compared against.

## Storage

- Append-only JSONL, one event per line, UTF-8, `schema_version = "ledger-2.0"`.
- Lives **outside** the run directory. `cleanup-run` deletes run JSON; the ledger must
  survive that cleanup.
- Default location is a host choice (for example `runtime/ledger/predictions.jsonl`); every
  command takes `--ledger <path>` explicitly.
- No command edits or deletes a line. A settlement is a second event referencing the
  prediction id. A mistaken settlement is corrected by a new correction event in a later
  schema, never by rewriting the old line.
- Schema 1.0 lines remain readable for audit, but they are reported under `legacy_schema`
  and never enter the forward score: the file cannot prove they were written before their
  labels matured.

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
| `prob_up`, `raw_probability`, `calibrated_probability`, `shadow_calibrated_probability` | reported forecast and the shadow calibrator |
| `probability_basis`, `calibration_status` | exactly how the published number was formed |
| `direction`, `confidence`, `confidence_score`, `predictive_evidence_grade` | reported diagnostics |
| `eligibility` | probability/direction/trading booleans |
| `prospective_eligible` | `true` only when the record was written before the label could be observed |
| `forward_eligibility` | rule, label maturity time, recording lag, reason |
| `import_reason` | why a non-prospective record was accepted; it stays retrospective |
| `model_provenance` | config hash, path probabilities/sources/weights, feature sets, training boundaries, calibration states, validation fingerprints, model versions |
| `baseline_probability`, `baseline_method`, `baseline_history_labels`, `baseline_frozen_at` | causal expanding up-rate frozen when the snapshot is available at recording time (`prediction`), otherwise computed at settlement |
| `engine_version` | runtime version that produced the forecast |
| `status` | `pending`, or `abstained` when no probability was reportable |

`prospective_eligible` is decided at recording time from `decision_at` (or the snapshot
as-of): a record is prospective when

```
recorded_at < decision_at + ceil(horizon_sessions * 7 / 5) calendar days
```

The calendar rule is a conservative proxy for the trading calendar (five sessions span
seven calendar days). A snapshot analyzed months after its close cannot become forward
evidence by being appended to the ledger; `ledger-record --import-reason <text>` documents
the backfill and keeps it out of the forward score.

`prediction_id` is deterministic: re-running the same snapshot with the same engine version
and probability is a duplicate and writes nothing. A different engine version or a changed
probability is a different prediction and is recorded separately.

### `settlement`

Written by `ledger-settle` when a later snapshot contains the label session. The snapshot
manifest ticker and market are the authoritative identity; `--ticker` is only a cross-check
and a mismatch is an error. Bars for one instrument can never settle a prediction for
another: mismatches are reported as `ticker_mismatch` / `market_mismatch` and left pending.

| Field | Meaning |
|---|---|
| `prediction_id` | the prediction being scored |
| `bars_ticker`, `bars_market` | identity of the bars that produced the label |
| `bars_first_session`, `bars_last_session`, `bars_session_count` | session-calendar identity of the settlement snapshot |
| `label_session` | the session whose close realized the label |
| `realized_return` | `close[label] / close[decision] - 1` |
| `hit` | `1` if `realized_return > 0`, else `0` |
| `brier` | `(prob_up - hit)^2` |
| `baseline_probability` | causal expanding up-rate using only labels matured by the decision session; the prediction-time value is reused when it was frozen |
| `baseline_brier` | `(baseline_probability - hit)^2` |
| `baseline_method` | `causal_expanding_up_rate_matured_labels` |
| `baseline_history_labels` | how many matured labels fed the baseline |
| `source_snapshot_id`, `source_snapshot_sha256` | the snapshot that supplied the label |

Settlement is idempotent: an existing settlement for the prediction id is never appended
again. A prediction whose label is not yet in the snapshot is reported as
`label_not_matured`, not treated as a failure.

## Commands

```bash
# Record after an analysis (or pass --ledger to analyze/pipeline directly)
quant_research.py ledger-record --analysis-dir <analysis_dir> --ledger <ledger.jsonl> \
  [--snapshot-dir <snapshot_dir>] [--import-reason "backfilled 2025 archive"]

# Settle against a later frozen snapshot
quant_research.py ledger-settle --ledger <ledger.jsonl> --snapshot-dir <snapshot_dir> \
  [--ticker INTC] [--horizon 5D]

# Score realized predictions
quant_research.py ledger-audit --ledger <ledger.jsonl>
```

`ledger-audit` reports unique/settled/pending/abstained counts, duplicate settlements,
duplicate prediction dates, identity conflicts, settlement coverage, and separate metric
blocks for `prospective`, `retrospective`, `legacy_schema` and `all_settled_descriptive`.
`overall` is the prospective block; it must not be quoted as the score of all rows.

Within each block:

- `observed_up_rate` is the realised label base rate (`mean(hit)`), **not** a model hit rate.
- `direction_accuracy` is `P(predicted side == realised side)` with `p = 0.5` excluded as a
  tie; `direction_samples` and `direction_ties` make the denominator explicit.
- `balanced_accuracy` averages up-class and down-class recall when both classes exist.
- `always_up_accuracy` and `direction_accuracy_delta_vs_always_up` give the trivial baseline.
- `causal_baseline_brier` and `brier_skill_vs_causal_baseline` compare against the frozen
  per-prediction causal baseline. `ex_post_climatology` is descriptive only and must never
  be used as a deployable baseline.

A positive skill in the prospective block is the first result that can be called forward
evidence. A Brier below the ex-post base-rate Brier is not implied and is not evidence.

## Rules

- Record **before** delivery or cleanup; the ledger is not a place to backfill history
  silently. Use `--import-reason` and accept the retrospective label.
- Never merge settlement events into the prediction line.
- Never score a prediction whose label session is after the snapshot's last bar.
- Never settle with bars whose ticker/market differs from the prediction's snapshot.
- An abstention is recorded; excluding it would hide coverage loss.
- The ledger is not a trading record: `entry_basis` stays `not_modelled` and no costs are
  applied.