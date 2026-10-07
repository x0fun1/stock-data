# Quant Research Architecture

The stock-data Skill is both the user-facing entrypoint and the only data gateway. The analysis runtime begins after collection; it never calls a market provider.

```text
User request
  → request contract
  → stock-data collection pass
  → normalized collection receipt
  → data-integrity checks (fatal errors block; nonfatal evidence gaps degrade)
  → frozen Point-in-Time Snapshot with prediction/reporting eligibility
  → Quant attempt when prediction-eligible
      ├─ Research A: hypothesis + empirical conditional frequency
      ├─ Research B: factor validation + redundancy removal
      ├─ Research C: logistic baseline + time-series validation
      └─ Research D: single-security time-series factor IC + forward returns (diagnostic only)
  → probability consensus over forecast paths A/B/C only
  → OOS/calibration validation + E adversarial/strategy audit (separate from probability eligibility)
  → PASS / DEGRADED / VETO reporting gate
  → freeze Quant Result + digest
  → isolated News / Event and Opinion-Sentiment analysis from the same snapshot
  → freeze News Result + digest
  → Final Synthesis (the only stage that reads both results; no probability blending)
  → unified risk/uncertainty
  → Summary in chat + standard report.md + debug report.json + stage receipt
```

## Collection boundary

User-intent routing for descriptive, fundamental, technical, event, ETF, and comparison tasks is in [analysis-paths.md](analysis-paths.md). The exact A/B/C/D/E code-to-artifact mapping and delivery checks are in [quant-paths.md](quant-paths.md). A–D are the four local researchers; E is the post-consensus audit, not a fifth forecast or a separate agent.

- One request produces one collection receipt and one immutable snapshot.
- The existing Finnhub/global router stays authoritative for source choice, fallback, permission, rate limit and units.
- Analysis consumes normalized DATA and redacted envelopes, never credentials or external instructions. It must not import the provider gateway. `pipeline` composes local request/adapt/freeze/analyze only; collection remains outside Python research.
- Research A/B/C/D receive only the same snapshot path. They return files with the same result schema and declare whether the result is a forecast or diagnostic. Consensus starts after all four have completed and includes forecast-role outputs only.
- Quant output, including consensus and bias audit, is serialized and hashed before News starts. A snapshot-only loader prepares the restricted `NewsInput`; the News entrypoint receives only that in-memory contract and an optional decay policy. It has no snapshot, research, or output-directory path and no Quant result, probability, or researcher output.
- News first applies the common snapshot as-of boundary, then deduplicates into canonical event and opinion clusters. Event direction and opinion sentiment stay in separate fields. Market reaction uses only the frozen OHLCV from that snapshot.
- Final Synthesis loads both result files only after their SHA-256 freeze receipts validate. It reports alignment/divergence and a categorical confidence; it does not recompute Quant or emit an adjusted/fused probability.
- Shared code is limited to request/snapshot validation, date and return utilities, neutral numerical statistics, and output schema. Features and interpretations remain inside each researcher.

## MVP implementation boundary

Implemented locally:

- Request normalization for US/HK and 1D/5D/20D sessions.
- Snapshot creation with source payload SHA-256, provenance, raw envelopes, OHLCV checks and simple data quality grading.
- Research A: predeclared 20-session momentum sign hypothesis and non-overlapping historical hit-rate probability.
- Research B: fixed OHLCV factor set, Rank/Pearson IC, ICIR diagnostics, quantile spread, coverage, turnover, horizon decay, correlation pruning, and a later-sample factor-score frequency mapping.
- Research C: deterministic L2 logistic regression, closed-interval Purged K-Fold, embargo, causal walk-forward, fold-local standardization, Platt calibration when enough OOS observations exist, and a chronological holdout.
- Research D: fixed OHLCV time-series IC and training-quantile forward-return diagnostics over 1D/5D/20D. It emits no forecast probability. A single-security sample cannot provide the upstream cross-sectional Rank IC, grouped portfolio, benchmark-relative NAV, or turnover-cost contract.
- Equal-weight probability aggregation, disagreement/diversity grading, veto/warning audit and JSON/Markdown report.
- Bias-avoidance checks for declared leakage controls, chronological holdout evidence, OOS Brier baselines, observed factor candidate counts, and explicit PBO/DSR/cost/survivorship assessment gaps.
- Post-Quant News/Sentiment stage with point-in-time filtering, exact/near-duplicate clustering, explicit event/opinion separation, a configurable session-based recency decay, deterministic first-pass event and opinion screening, frozen-OHLCV reaction metrics, independent result digest, and final categorical Quant/News synthesis.

Not yet implemented: multi-asset cross-sectional portfolio backtesting, formal CPCV/PBO/DSR calculation, costed execution and market-impact simulation, point-in-time multi-asset survivorship audit, boosted-tree model integration, bootstrap uncertainty, long-term researcher reliability tracking, a governed one-use holdout store, forward evidence receipts, and a provider-native API adapter. The local audit cannot certify these calculations even when extra inputs are supplied; do not imply they are complete.

## Runtime layout

```text
runtime/
  snapshots/<snapshot_id>/
    manifest.json
    market.json
    fundamentals.json
    expectations.json
    news.json
    insider.json
    options.json
    short_volume.json
    sec.json
    macro.json
    raw/<domain>.json
  research/<analysis_id>/
    researchers/{quant,factor,ml,factor_backtest}.json
    quant_result.json + quant_result.freeze.json
    news_result.json + news_result.freeze.json
    report.json + report.md
    agent_summary.json
```

These are user-generated artifacts and should live in a writable user workspace, not inside a read-only Skill installation.
