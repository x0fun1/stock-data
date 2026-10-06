# Frozen Snapshot Schema

## Identity

`manifest.json` is the authoritative identity record. The freezer writes:

- `schema_version`
- `snapshot_id`, `ticker`, `market`, `horizon`, normalized `request`
- `asof_timestamp`, `snapshot_created_at`
- `source_payload_sha256` over the complete collection receipt
- `snapshot_content_sha256`, incorporating the normalized request, as-of time and domains
- `domain_sha256s` and `raw_domain_sha256s`, covering every normalized and raw domain file
- `market_bar_count`
- `data_domains` availability states
- `sources` with actual source, source time, fetch time, units/currency and fallback evidence
- `data_quality.overall`, `data_quality.score`, and quantified warnings

`snapshot_id` is ticker + UTC creation time + horizon + a prefix of the normalized content digest. Existing directories are never overwritten. The `raw/` directory contains the original receipt per domain; top-level domain files contain the normalized adapter view. Every researcher verifies these hashes before using a snapshot and rejects modified or incomplete files.

The optional `news.json` domain is preserved inside this same content-addressed snapshot. The post-Quant News layer may read only normalized `news.data.articles[]`, the shared manifest request/as-of metadata, and `market.data.bars`. It must not read any researcher/report result before producing and hashing `news_result.json`.

The optional `news.json` domain is preserved inside this same content-addressed snapshot. The post-Quant News layer may read only normalized `news.data.articles[]`, the shared manifest request/as-of metadata, and `market.data.bars`. It must not read any researcher/report result before producing and hashing `news_result.json`.

## Market validation

Before freezing, the builder rejects missing bars, duplicate or unsorted session dates, post-as-of bars, non-finite or non-positive OHLC values, negative volume, and inconsistent high/low values. Missing adjustment convention and thin histories are retained as warnings. The builder never fills missing prices or volume.

## Data quality

The current score is a transparent availability/history heuristic: 55% history depth capped at 504 bars and 45% fraction of available domains, less 0.03 per fallback (capped at 0.15), less 0.15 when the latest market bar is more than seven calendar days old, then multiplied by 0.8 when the adjustment convention is unknown. It is not a probability that the data is correct. Report the score and its components alongside underlying issues; do not interpret it as a statistical confidence interval. `mode: strict` rejects an unknown adjustment convention.

## Point-in-time data

Current Snapshot records retain their publication and filing timestamps. Historical tests may use such records only where the record's own availability time is no later than the simulated signal time. Current-only fundamentals, latest analyst recommendations and current index memberships cannot be projected backward into a historical feature matrix.
