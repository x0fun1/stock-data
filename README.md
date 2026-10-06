# Stock Data & Quant Research

An installable `stock-data/` Skill that combines market-data collection with auditable quantitative analysis and a post-Quant News/Sentiment layer. Existing Finnhub MCP/global-stock-data routing remains the sole gateway. Direction research freezes one data receipt, evaluates three independent forecast paths plus a diagnostic-only factor IC path, completes consensus and bias-avoidance review, then freezes a separate News result and performs categorical evidence synthesis.

## What is included

```text
stock-data/
  SKILL.md
  scripts/
    global_stock_data.py             existing data gateway fallback
    quant_research.py                snapshot/research CLI
    quant_research/                  snapshot, independent paths, validation, reports
  references/                        source routing, schemas, PIT/probability/news policies
source-material/                     archived upstream source materials
tests/                               offline gateway and News-layer contract tests
UPSTREAM_REVIEW.md                   implementation research and licensing decisions
```

## Research workflow

For a direction or probability request, use the Skill's data route once, normalize the gateway results to the collection receipt in `stock-data/references/collection-adapter.md`, then run:

```text
python stock-data/scripts/quant_research.py request --input request.json --output normalized-request.json
python stock-data/scripts/quant_research.py adapt --request normalized-request.json --responses gateway-responses.json --output collected.json
python stock-data/scripts/quant_research.py freeze --input collected.json --output-root runtime/snapshots
python stock-data/scripts/quant_research.py analyze --snapshot-dir runtime/snapshots/<snapshot_id> --output-root runtime/research
```

The freezer preserves raw gateway envelopes and provenance. Researchers consume the same snapshot and emit structured probabilities only when their sample and validation conditions are met. The analysis script makes no network calls.

The default directional research window is the latest three calendar years through the latest confirmed closed session. Longer source history stays in the raw envelope; the primary snapshot uses the selected window. If a path lacks enough observations, it reports insufficient data instead of silently widening the period.

## Current implementation scope

- **Research A:** a declared 20-session momentum-sign hypothesis with non-overlapping historical conditional frequencies and a chronological evaluation slice.
- **Research B:** local OHLCV factor diagnostics across 1D/5D/20D, correlation pruning, later-sample score calibration, and holdout reporting.
- **Research C:** deterministic L2 Logistic Regression with closed-interval Purged K-Fold, Embargo, causal walk-forward, optional Platt calibration, and a chronological holdout.
- **Research D:** single-security time-series IC and 1D/5D/20D forward-return diagnostics with a chronological holdout. It does not emit a probability or enter consensus. The full upstream cross-sectional backtest requires a multi-ticker factor panel, point-in-time universe, benchmark, and price/mask matrices.
- **Consensus/audit:** equal-weight forecast probabilities, disagreement and evidence-family diversity, plus explicit look-ahead, chronological OOS, Brier baseline, multiple-testing, PBO/DSR, cost, and survivorship checks. Missing trial histories, cost inputs, or point-in-time universe data are marked not assessed.
- **News/Sentiment:** runs only after the complete Quant result and bias audit are frozen. The NewsInput contains timestamped normalized articles and frozen OHLCV, but no Quant fields. It filters by `asof_timestamp`, deduplicates canonical events, separates event facts from analyst/social opinions, applies configurable session decay, and measures next-session price/gap/volume/volatility reaction from the same frozen bars.
- **Final synthesis:** reads the two digest-verified results, reports agreement/divergence and categorical confidence, preserves the Quant P(up) unchanged, and does not manufacture a News or blended probability. Missing News coverage or timing is marked not assessed.

Cross-sectional portfolio backtesting, costed execution/market impact, formal CPCV/PBO/DSR calculations, multi-asset survivorship audit, boosted models, bootstrap uncertainty, long-term path reliability, governed holdout receipts, and forward evidence are not implemented. The bias audit makes these input and calculation gaps explicit. No real-data output is included or fabricated. The native analysis uses Python standard library only; the existing global data fallback still needs Python 3.10+, `requests`, and network access as documented in the Skill references.

News is a separate descriptive layer, not a quantitative forecast feature. A short current-news source is not a three-year archive; coverage is reported separately from the default three-calendar-year OHLCV window. Daily-bar reactions indicate timing association and do not establish that news caused a price move. See `stock-data/references/news-analysis.md` for contracts and boundaries.

## Data-source and safety boundary

The existing source routing, rate-limit, authorization, data-license and provenance restrictions remain in force. Each provider is accessed only through the current Finnhub MCP or bundled stock-data gateway. A missing timestamp or unavailable field is reported, never inferred. Forecast probabilities are not trade instructions or guarantees.

Read `UPSTREAM_REVIEW.md` for source review and license notes. Detailed contracts are in `stock-data/references/`.
