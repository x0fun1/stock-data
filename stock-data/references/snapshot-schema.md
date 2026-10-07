# Frozen Snapshot Schema

Current schema is **1.1**. [Reliability gates](reliability-gates.md) define source-calendar, identity, frequency, freshness and adjustment evidence. Missing evidence blocks forecasting; legacy 1.0 remains readable without new reporting eligibility.

## Identity

`manifest.json` is the authoritative identity record. The freezer writes:

- `schema_version`
- `snapshot_id`, `ticker`, `market`, `horizon`, normalized `request`
- `asof_timestamp`, `snapshot_created_at`
- `source_payload_sha256` over the redacted collection receipt
- `snapshot_content_sha256`, incorporating the normalized request, as-of time and domains
- `domain_sha256s` and `raw_domain_sha256s`, covering every normalized and raw domain file
- `market_bar_count`
- `data_domains` availability states
- `sources` with actual source, source time, fetch time, units/currency and fallback evidence
- `data_quality.overall`, `data_quality.score`, and quantified warnings
- `manifest_sha256` binds metadata; gates/quality/sources are recalculated from verified domains on load
- `data_validation.forecast_eligible`, blockers, latest confirmed close and source-authentication limits

`snapshot_id` is ticker + UTC creation time + horizon + a prefix of the normalized content digest. Existing directories are never overwritten. `raw/` contains redacted source receipts with action fields and credentials removed. Top-level files contain normalized adapter views. Every researcher verifies hashes before use. Hashes do not authenticate a supplier.

## Artifact path boundary

`snapshot_id` must be a non-empty string consisting only of ASCII letters, digits, `.`, `_`, and `-`, and must not equal `.` or `..`. The loader validates this before reading domain files and separately verifies the existing content/digest suffix rules. This rejects separators on either platform, absolute/drive/UNC paths, alternate data-stream colons, whitespace, and control characters. A valid digest suffix alone does not make a name safe; hashes are integrity checks, not signatures authenticating an external snapshot.

Both snapshot freezing and analysis call the shared `scripts/quant_research/paths.py` guard before creating output directories. The guard revalidates the output name, resolves the selected root and candidate destination, and requires the resolved destination to be a direct child of that root. Existing symlinks/junctions pointing outside the root are rejected before writing; existing outputs remain subject to no-overwrite checks. A user-selected linked output root is treated as its resolved directory, and returned artifact paths are absolute.

These pre-write checks assume the output root and its directory entries are not concurrently replaced by another process. They do not provide race-proof isolation against a hostile process with write access to those directories.

The optional `news.json` domain is preserved inside this same content-addressed snapshot. The post-Quant News layer may read only normalized `news.data.articles[]`, the shared manifest request/as-of metadata, and `market.data.bars`. It must not read any researcher/report result before producing and hashing `news_result.json`.

## Market validation

The builder rejects missing bars, duplicate/unsorted/future dates, invalid OHLCV, booleans, provider failure and contradictory symbol/frequency/provenance/window metadata. Missing source evidence and session gaps block forecasting. It never fills prices/volume or compresses missing sessions; actual window/freshness is recalculated after excluding a forming bar using source close timestamps.

## Data quality

The score is 70% history depth capped at 504 bars plus 30% source-evidence eligibility. OHLCV requires only market; optional-domain absence no longer mechanically lowers its score. This is a heuristic, not correctness probability or statistical interval. Reporting eligibility is a separate gate. Unknown adjustment blocks forecasting in both modes; strict also rejects its snapshot as before.

## Point-in-time data

Current Snapshot records retain their publication and filing timestamps. Historical tests may use such records only where the record's own availability time is no later than the simulated signal time. Current-only fundamentals, latest analyst recommendations and current index memberships cannot be projected backward into a historical feature matrix.
