# Upstream Review

Reviewed before implementation on 2026-10-07. This note records design inputs only; no upstream source code has been copied into this project.

## Data Gateway — `stock-data`

Repository: https://github.com/x0fun1/stock-data

### Reviewed local gateway materials

- `stock-data/SKILL.md`
- `stock-data/references/global-stock-data.md`
- `stock-data/references/source-policies.md`
- `stock-data/references/finnhub-mcp-1.21.3-schema.md`
- `stock-data/scripts/global_stock_data.py --list`
- `stock-data/scripts/global_stock_data.py` and its offline behavior tests

The Git remote is configured as `https://github.com/x0fun1/stock-data.git`, but the remote page was not available to the research browser. The checked-in copy is therefore the operative gateway contract for this refactor. Its current abilities include Finnhub MCP routing, source-by-source fallbacks through the bundled global script, provenance envelopes, and local technical-indicator helpers. The script does not automatically route between sources; the Skill workflow is responsible for that.

### Adapter design

Keep all market access in the existing `stock-data` workflow. The research runtime accepts a captured, normalized `stock-data` collection payload, preserves its raw envelopes and provenance, validates and freezes it once, and gives the frozen snapshot to each researcher. Researchers do not import the gateway implementation or call providers.

The input contract requires daily OHLCV plus optional domain payloads and source metadata. If the requested history, timestamps, or availability metadata are absent, the affected researcher must return `insufficient_data` or a warning. Do not fill gaps from another provider during research.

## Research A — `quantskills/skill-quant-research`

Repository: https://github.com/quantskills/skill-quant-research

### Reviewed files

- `SKILL.md`
- `references/data-contract.md`
- `references/statistical-validation.md`
- `references/risk-and-reporting.md`
- `scripts/validate_quant_data.py`
- `README.md` / `README.en.md`
- Repository tree: no `tests/` directory was listed at review time.

### Design retained and adaptation

Retain falsifiable hypotheses, explicit signal/execution timing, point-in-time joins, explicit units and price conventions, simple baselines, chronological validation, fold-local transforms, uncertainty, failure conditions, and a reproducibility contract. Adapt its broad portfolio/backtest workflow to the single-security forecast contract here. This project does not claim costed strategy performance from a forecast snapshot.

The upstream skill declares GPL-3.0-only. No code or prose is copied; the local integrity and hypothesis workflow is a clean implementation of the project contract.

## Research B — `quantskills/skill-quant-factor-skill-factory`

Repository: https://github.com/quantskills/skill-quant-factor-skill-factory

### Reviewed files

- `SKILL.md`
- `README.md`
- `references/acceptance_checklist.md`
- `scripts/generate_factor_skill_batch.py`
- Repository tree: no `tests/` directory was listed at review time.

### Design retained and adaptation

Retain explicit factor definitions, coverage, real-data validation, Rank IC, ICIR, quantile spread, turnover, and package/acceptance checks. The upstream is a factor-Skill generator rather than a directional probability model; this project instead defines a small fixed candidate set, validates across horizons, removes correlated features, and only emits a probability when a historical mapping is supported.

The upstream skill declares GPL-3.0-only. No generator code, factor definitions, or documentation was copied. Local factors are independently specified from raw OHLCV and carry their own formulas and parameters.

## Research C — `quantskills/skill-ml-purged-cv`

Repository: https://github.com/quantskills/skill-ml-purged-cv

### Reviewed files

- `SKILL.md`
- `README.md` sections covering Information Intervals, Purge, Embargo, CPCV, causal walk-forward, Fold-Local transforms, holdout, feature lineage, and evidence boundaries
- `LICENSE`
- Repository tree and public tool/API descriptions

The web reader could not load the repository's individual reference, implementation, or test paths during this review. The public README/API documentation was used to establish the interface and design boundary; those unavailable files were not treated as reviewed. Revisit them before adding deeper integration.

### Design retained and adaptation

Use closed information intervals, purge actual interval overlaps, keep session groups together, fit preprocessing only on training folds, distinguish embargo from the pre-test gap, and keep a final chronological holdout separate from tuning. A causal walk-forward path is required before promoting a model probability. The first pass uses a bounded native logistic baseline; a future integration may call the installed `purged_kfold_validation` package for its broader CPCV, PBO, DSR, holdout-governance, and forward-evidence tools.

The upstream repository declares MIT in its README/GitHub license metadata. This project does not vendor its implementation; therefore no MIT code notice is required. Its concepts are credited here.

## Research D — `quantskills/skill-factor-backtest`

Repository: https://github.com/quantskills/skill-factor-backtest

### Reviewed upstream materials

- `SKILL.md` and `README.md`
- `references/backtest-contract.md`
- Public input, execution-lag, IC, forward-window, grouped-return, and output contracts

The upstream declares `GPL-3.0-only`. Its full portfolio workflow requires a dated ticker-by-factor panel plus a broad Parquet market-data root containing a trading calendar, adjusted/tradable price matrices, security masks, and a benchmark. Its primary Rank IC and grouped portfolio analysis are cross-sectional. A single-INTC snapshot cannot satisfy that full universe contract.

### Design retained and adaptation

Add a diagnostic-only factor-validation path for IC and forward returns over 1D/5D/20D. On a single ticker, label the statistic as time-series IC and the quantile spread as a historical time-series diagnostic; never call it cross-sectional Rank IC or a portfolio backtest. Require a multi-ticker panel and compatible market-data universe before advertising the full portfolio/benchmark workflow. Diagnostic output does not contribute a second probability to consensus.

No upstream code or report prose is copied. The adapter and calculations are independently implemented against this project's snapshot schema.

## Research E — `quantskills/skill-backtesting-bias-avoidance`

Repository: https://github.com/quantskills/skill-backtesting-bias-avoidance

### Reviewed upstream materials

- `SKILL.md`
- `references/backtest-guide.md`
- Bias taxonomy and validation/reporting rules for look-ahead, survivorship, overfitting/data snooping, costs, chronological OOS, HAC uncertainty, CSCV/PBO, and DSR
- `LICENSE`

The upstream declares `GPL-3.0-only`. A full strategy audit requires a defined strategy and execution rule, trial/parameter history, per-trial returns, universe membership (including delisted assets), and cost assumptions. A one-ticker OHLCV direction snapshot does not supply all of those inputs.

### Design retained and adaptation

Extend the existing adversarial audit with explicit pass/warn/not-assessed results for leakage controls, chronological holdout, multiple testing, survivorship/PIT coverage, transaction costs, and PBO/DSR eligibility. Compute only diagnostics supported by the stored snapshot and researcher outputs. If strategy trials, a point-in-time universe, execution costs, or a trial-return matrix are missing, report those checks as not assessed; do not imply that the strategy is bias-free or calculate PBO/DSR from a single return series.

No upstream code or prose is copied. This project applies an independently written audit to forecast and factor-diagnostic outputs; it does not claim the upstream's full transaction-costed strategy engine.

## News / Sentiment — `TauricResearch/TradingAgents` and `quantskills/skill-news-sentiment-analyst`

Repositories:

- https://github.com/TauricResearch/TradingAgents
- https://github.com/quantskills/skill-news-sentiment-analyst

### Reviewed upstream materials

- TradingAgents `tradingagents/agents/analysts/news_analyst.py` and `sentiment_analyst.py`, plus analyst execution/schema boundaries
- `skill-news-sentiment-analyst/SKILL.md` and its report/data-flow description

TradingAgents separates News and Sentiment analysts and grounds their prompts in separately collected source blocks; its current sentiment inputs include news and social feeds, while the upstream notes those feeds are not archived for historical point-in-time runs. The QuantSkills Skill separates news collection, AI event interpretation, and market-data validation, but is A-share-specific and relies on its own collector/verifier. Both upstream repositories identify Apache-2.0; the QuantSkills repository's README declares Apache 2.0, while the standalone license file could not be fetched during review.

### Design retained and adaptation

Run News/Sentiment only after the local Quant Result is frozen. The news package receives a restricted `NewsInput` constructed from the frozen snapshot's ticker/market/as-of/horizon, normalized timestamped news records, and existing OHLCV bars. It has no Quant result argument or file path. Deduplicate reports into canonical events, keep reported events separate from explicit opinion/social records, enforce the snapshot as-of boundary, compute market reactions only from frozen bars, and let the synthesis function read both frozen results after News completes. News produces descriptive direction/sentiment/confirmation only; no P(up) or adjustment to Quant probability is allowed.

The local first pass is deterministic and conservative: keyword/event screening may return `unknown` or `unclassified`; it is not represented as a general-purpose semantic model or causal finding. Social/analyst sentiment remains unavailable unless the existing data gateway supplies normalized timestamped opinion records. A provider-level aggregate sentiment is preserved separately only when its availability time is no later than the snapshot as-of; it is not mixed with article-level opinion scores. No upstream collection code, prompts, wording, classifier, or report template is copied.

## Project boundary and implementation decisions

- `stock-data` is the sole collection gateway; no researcher has provider credentials or fetch logic.
- Researchers receive the same immutable snapshot and write separate result files. Consensus is a later stage that reads those results only after researcher execution completes.
- Raw OHLCV remains the shared input. Each researcher builds its own features; the factor-backtest diagnostic is explicitly marked as single-asset time-series validation unless a universe panel is present.
- Quant Result is frozen before News/Sentiment. The NewsInput contract contains no Quant direction/probability, and only final synthesis may compare the two frozen outputs.
- News/event facts and opinion/sentiment are separate record classes; deduplicated canonical events count as one event regardless of article count. News timestamps are filtered against the same snapshot as-of time.
- Probabilities and metrics are code-derived; no LLM-authored numeric estimates are accepted.
- Missing PIT lineage, inadequate sample size, unavailable ML dependencies, or unverified historical facts reduce researcher status rather than being guessed.
- No third-party code or factor formula implementation is reused.
