# Frozen Snapshot Schema

Current schema is **1.3**. Schema 1.0/1.1/1.2 remain readable for diagnosis and
legacy reproduction but are not prediction-eligible under the fixed policy.

## Identity and integrity

`manifest.json` is authoritative. It contains the schema version, `snapshot_id`,
ticker, market, horizon, normalized request and as-of time, source receipt
digest, normalized-content digest, per-domain normalized/raw hashes and
`manifest_sha256`. Hashes establish content integrity, not supplier
authentication. Existing directories are never overwritten; `raw/` keeps the
redacted source receipt and the top-level domain files keep the normalized view.

## Policy block

Forecast snapshots carry `manifest.policy` with:

- `policy_version` (`fixed_52w_5d_v1`), window length (364 calendar days),
  horizon (5 sessions), target/model/feature versions;
- `decision_session`, `target_session`, `window_start_exclusive`,
  `window_end_inclusive`;
- `expected_sessions`, `observed_sessions`, `missing_sessions`,
  `coverage_status`, `calendar_verified`;
- price basis (`split_adjusted`, dividends not reinvested) and its source.

`data.policy_window` records the same window plus `dropped_out_of_window_count`.
Any bar on or before the left-open boundary is rejected with
`OHLCV_OUT_OF_WINDOW`. A forecast with a missing/unverified calendar may freeze
for diagnosis but cannot publish a formal probability.

## Market data

Normalized daily bars contain date, OHLCV and the split-adjusted close. The
decision session is the latest exchange session whose close is at or before the
as-of instant; an intraday run uses the previous completed close. The target is
the fifth exchange session after the decision, not "five bars that happen to
exist".

The builder rejects missing/duplicate/unsorted/future dates, invalid OHLCV,
booleans, provider failure and contradictory identity/frequency/provenance/window
metadata. With a verified schedule the bar axis must match confirmed sessions;
missing sessions are `INSUFFICIENT_HISTORY_COVERAGE`. Without a schedule the
result is diagnostic only.

## Data quality

`data_quality` is `data_completeness_v2`: observed/expected sessions, coverage
ratio, prediction eligibility and issue list. `score`, `confidence_score` and
`confidence_penalties` are deprecated and null; no 0–100 confidence is published.

## News

The optional `news.json` domain is preserved inside the same content-addressed
snapshot. The post-Quant News layer reads only normalized articles, shared
identity/as-of metadata and frozen bars; it never sees Quant results before its
own result is frozen, and it cannot change Quant probability.

## Retention

Cleanup ownership is tracked in `runtime/runs/<run_id>/run_manifest.json`.
`cleanup-run` deletes only registered JSON whose hashes still match and keeps
`report.md`, `final_response.md` and a minimal `cleanup_receipt.txt`. The snapshot
and ledger are outside the run and are never deleted by run cleanup.