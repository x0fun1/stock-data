# Stock Research Skill

Evidence-first US/HK quantitative and news research. The executable skill lives in this directory; the design specifications are in the repository `docs/` folder.

## What is implemented

- Versioned contracts and capability states.
- US NYSE-rule calendar and HK observed-session calendar.
- Immutable raw store, Parquet curated versions, SQLite catalog.
- Action-aware wealth index, window returns, labels and path risk.
- Price/volatility/liquidity factors, point-in-time financial quarter/TTM factors, macro factors.
- Date-grouped partitions with purge/embargo.
- Training-only transforms, L2 logistic regression, quantile regression, Platt calibration.
- Brier/log-loss/AUC/calibration metrics, date-block bootstrap, validation evidence.
- Target-level publication decisions and a single-message report.
- Seven-grade news context with URL/body/cluster deduplication and evidence gates.
- Authorized local imports and read-only acquisition clients for SEC, FRED, Eastmoney, Sina and Finnhub.
- Bundled pinned Kronos-small/tokenizer weights, complete upstream source and local native inference.
- Daily reports include a frozen next-day direction baseline; a UTC hourly entry uses all prior completed OHLCV.
- Native multifactor snapshots, conditional industry/size residualization, family controls, Ridge and local LightGBM candidates.
- Date-grouped IC/coverage/assumed-cost diagnostics and sealed historical model comparison; an independent FF3 + Momentum risk layer.

## Install

Python 3.12+. The complete pinned dependency installation is verified on Windows x64 with Python 3.13.7 and CPU PyTorch.

```powershell
python scripts/install_skill.py --destination "$env:USERPROFILE\.codex\skills\stock-research"
```

This installs the complete package into the destination, creates its own `.venv`, verifies source/weight hashes, installs CPU dependencies and runs real Kronos inference plus LightGBM/factor-processing self-tests. Existing installations require `--update`. It does not modify global Python or call an MCP/model service. Use the installed `scripts/skill.py` launcher for subsequent commands.

The first dependency installation needs network access. `--offline` only prevents checkpoint downloads; bundled weights are verified and reused.

No DuckDB or sklearn is required. `duckdb` is optional for ad-hoc queries.

## Quick start

Run from this directory:

```bash
# Inspect registry state
python scripts/skill.py doctor

# Current research; imports must exist under the runtime root
python scripts/skill.py import --kind prices --file examples/prices-template.csv --market US --ticker AAPL
python scripts/skill.py research --market US --ticker AAPL --benchmark SPY --horizons 1,5,20,60

# Preserve explicit UTC start/end intervals for hourly data.
python scripts/skill.py quant-baseline --file NVDA-hourly.csv --ticker NVDA --frequency 1h --cutoff 2026-11-05T20:00:00Z
```

The report is written to `runtime/reports/` and printed to stdout. Without an active verified model, probabilities are shown as **不发布** while descriptions, historical risk, news and limitations remain available.

Kronos direction and raw path votes are reported separately and are not calibrated probabilities. The original weights remain frozen; the [historical training plan](references/kronos-training-plan.md) describes later multi-stock fine-tuning and masked evaluation.

Daily research also runs the [native multifactor workflow](references/multifactor-workflow.md). It reports available raw factors, own-history percentiles and independent risk attribution. Company rankings require an adequate company panel; one stock and its ETF benchmark cannot form that panel. Historical Ridge/LightGBM studies require an explicit data-admission declaration or `--development-only`; their research scores do not activate investment probabilities.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

On other platforms use `.venv/bin/python -m pytest`. The suite uses synthetic fixtures only. It does not require credentials or network access.

## Layout

```text
SKILL.md                 entry document
references/              task-specific operating rules
policies/                source policy, defaults, calendar strategy
examples/                request/protocol/data templates
scripts/research.py      CLI
scripts/skill.py         isolated-environment launcher
scripts/install_skill.py complete one-command installer
scripts/stock_research/  implementation package
vendor/kronos/          complete pinned official model/fine-tuning source and LICENSE
model-assets/kronos-small/ frozen checkpoints and relative hash manifest
tests/                   unit, integration and CLI tests
```

## Status

The pretrained Kronos baseline and native multifactor research architecture are integrated. Custom neural fine-tuning, automatic Kronos OOF-factor export and a complete broad-stock training panel are still pending. No investment probability is published unless a model has frozen validation evidence for the exact market, horizon and target. See `SKILL.md` and `references/research-and-validation.md`.
