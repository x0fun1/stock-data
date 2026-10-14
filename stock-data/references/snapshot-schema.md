# Frozen Snapshot Schema

Current schema is **1.2**. [`reliability-gates.md`](reliability-gates.md) defines the separated prediction, reporting, and strategy policies. Missing nonfatal validation evidence degrades confidence; only fatal data/model-integrity conditions block prediction.

The 2.6 research runtime adds optional analysis-result fields (`oos_forecasts`, OOS probability-quality metrics, temporal Brier Skill diagnostics, factor IC inference, calibration training boundaries, `economic_viability` and runtime measurements). Later runtimes extend this with full-history walk-forward ledger metadata (`prediction_days`, `non_overlapping_prediction_count`, `effective_sample_size_estimate`, `historical_asof_ledger`), moving-block bootstrap skill intervals and the `ensemble_calibration` diagnostic. These are not required fields in `manifest.json` or the frozen snapshot; schema remains 1.2. New consumers treat absent result fields in older analyses as `not_assessed` and keep equal weighting. Existing 1.2 snapshots continue through the same loader and data gates; 1.0/1.1 snapshots remain readable for diagnostics but are not newly prediction-eligible. No existing status/reason-code enum or required snapshot field changed. Any future change to snapshot identity, input enums or required fields requires a separate schema version decision.

## Identity and integrity

`manifest.json` is the authoritative snapshot identity record. It contains:

- schema/version, `snapshot_id`, `ticker`, `market`, `horizon`, normalized request and as-of time;
- source receipt digest, normalized-content digest, normalized/raw per-domain hashes and `manifest_sha256`;
- actual source, source/fetch times, units/currency, adjustment/calendar evidence and fallback provenance;
- `data_quality.overall`, bounded score, explainable confidence score, issues and reason codes;
- `data_validation.prediction_eligibility` (`eligible`/`blocked`), `reporting_eligibility` (`pass`/`degraded`/`veto`), calendar/horizon resolution, latest-close evidence and fatal blockers.

The resolved schedule from an optional installed calendar library is frozen in `data_validation.resolved_calendar_evidence` with source/version and coverage; reload uses this pinned value instead of whichever library happens to be installed then. `snapshot_id` binds ticker, UTC creation time, horizon and a content-digest prefix. Existing directories are never overwritten. `raw/` contains redacted source receipts with action fields and credentials removed. Top-level domain files contain normalized adapter views. Researchers verify hashes before use. Hashes establish content integrity, not supplier authentication.

### Schema compatibility

Schema 1.0 and 1.1 can be inspected/reloaded for diagnostics, but are explicitly blocked from prediction under the current policy. Re-freeze current collection evidence under 1.2 before making forecasts; old artifacts are not silently reinterpreted as newly eligible.

## Artifact path boundary

`snapshot_id` must be a non-empty string consisting only of ASCII letters, digits, `.`, `_`, and `-`, and must not equal `.` or `..`. The loader validates this before reading domain files and separately verifies the existing content/digest suffix rules. This rejects separators on either platform, absolute/drive/UNC paths, alternate data-stream colons, whitespace, and control characters. A valid digest suffix alone does not make a name safe; hashes are integrity checks, not signatures authenticating an external snapshot.

Both snapshot freezing and analysis call the shared `scripts/quant_research/paths.py` guard before creating output directories. The guard revalidates the output name, resolves the selected root and candidate destination, and requires the resolved destination to be a direct child of that root. Existing symlinks/junctions pointing outside the root are rejected before writing; existing outputs remain subject to no-overwrite checks. A user-selected linked output root is treated as its resolved directory, and returned artifact paths are absolute.

These pre-write checks assume the output root and its directory entries are not concurrently replaced by another process. They do not provide race-proof isolation against a hostile process with write access to those directories.

## Market and close data

Normalized market data contains daily OHLCV bars, actual source, source/fetch timestamps, source symbol, currency/unit and history window. Optional `latest_price_observations[]` records independent same-session daily closes with `source`, ISO `session_date`, `price`, `currency`, `kind=daily_close`, and `adjustment` or `price_basis`; only distinct source names in the same session/currency and the same recognized OHLCV adjustment convention are usable for cross-source verification. If both basis fields are present they must agree. Observations with unknown or incompatible bases remain archived but do not support either matching or a price-conflict gate. Agreement within 0.3% can mark `cross_source_verified`; a material conflict among compatible closes vetoes. Provider names remain unauthenticated source claims. A supplied calendar confirms closure only for bar dates actually in its session rows; a missing row falls back to explicit source confirmation for a same-as-of-date bar.

The builder rejects missing/duplicate/unsorted/future dates, invalid OHLCV values, booleans, provider failure and contradictory identity/frequency/provenance/window metadata. A complete resolved calendar is compared to the bar axis; confirmed missing bars are fatal. Without a calendar, sorted unique OHLCV dates are an explicitly degraded observation fallback; no synthetic rows, prices or volumes are inserted. Weekend dates, severe unexplained gaps, stale data, unresolved latest observation or invalid timeline remain fatal. Daily labels are `h` observations forward when session completeness is not verified; output discloses `horizon_type=estimated_observed_sessions`.

Adjustment convention and corporate-action state are separate: `adjustment_status` may be unknown while `corporate_action_status` is unavailable. Unknown evidence alone is not contamination. Explicit `adjustment_evidence.action_coverage_confirmed: false` (or any present value other than boolean `true`) keeps coverage unknown with `CORPORATE_ACTION_UNKNOWN` and its existing confidence penalty; an empty action list cannot establish `clear`. Legacy evidence omitting the flag keeps the existing source/checked-through/actions coverage rule. Confirmed action records are still assessed even when complete coverage is unknown. A confirmed in-window split/reverse split/spin-off/major adjustment event with unadjusted prices is fatal; otherwise short-horizon paths may run with degraded confidence. Total-return/dividend/long-horizon paths must require appropriate adjustment evidence.

## Eligibility and confidence

Prediction eligibility answers whether computation can produce a valid forecast. Reporting eligibility answers whether to present it normally (`pass`), with limitations (`degraded`), or withhold it (`veto`). `forecast_eligible` remains a legacy alias for prediction eligibility only. PBO/DSR, transaction costs and portfolio backtests are strategy-level evidence, not probability gates.

The current confidence score is a rule-based explanation of data/validation limitations, not a correctness probability. `confidence < 40` never independently vetoes. OOS/calibration/backtest gaps are reported distinctly from a missing or invalid model probability.

## Point-in-time data and News

Current Snapshot records retain publication and filing timestamps. Historical tests may use such records only where the record's own availability time is no later than the simulated signal time. Current-only fundamentals, latest analyst recommendations and current index memberships cannot be projected backward into a historical feature matrix.

The optional `news.json` domain is preserved inside this same content-addressed snapshot. The post-Quant News layer may read only normalized `news.data.articles[]`, shared manifest request/as-of metadata and `market.data.bars`. It must not read researcher/report results before producing and hashing `news_result.json`. News cannot generate or alter Quant probability.

## Run artifact retention

The snapshot schema remains **1.2**; cleanup ownership is tracked outside the snapshot in `runtime/runs/<run_id>/run_manifest.json`. The opt-in `pipeline --cleanup-json` mode gives one collection, one frozen snapshot, and one analysis an isolated run root. After the prepared reply passes `validate-response`, `cleanup-run --run-dir` validates the same reply again, checks the registered run and snapshot identities, then deletes only registered JSON whose hashes still match. It keeps `report.md`, `final_response.md`, and a minimal `cleanup_receipt.txt` containing hashes, status, byte/file totals, and relative JSON target names.

`--dry-run` reports the registered targets without deleting them. Partial deletion is recorded and may be retried; failed/incomplete runs require explicit `--force-incomplete`. A named `--analysis-dir` cleanup removes JSON from that analysis only and leaves its snapshot untouched. Legacy shared outputs, external request/response files, unrelated runs, user files, and caches are outside the ownership boundary. The retained hashes and Markdown are not a substitute for the removed raw inputs or a reproducible research archive; use `--keep-json` for audit or replay work.
