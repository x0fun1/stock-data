# Request and Researcher Contract

## Request

```json
{
  "ticker": "NVDA",
  "market": "US",
  "horizon": "5D",
  "asof": null,
  "mode": "standard"
}
```

Allowed markets: `US`, `HK`. Allowed horizons: `1D`, `5D`, `20D`; default is `5D`. With a complete resolved exchange calendar, horizon labels use valid exchange sessions. Under observation-date fallback, historical labels count `h` OHLCV observations and the future horizon is estimated; do not describe it as calendar-verified. `asof`, when present, must be an ISO-8601 timestamp with an explicit timezone.

Optional request fields: `intent` (`forecast`, `descriptive`, `factor_research`), `asset_type` (`stock`, `etf`, `index`, `unknown`), `history_window.start/end/selection` (or history_start/end), and source-backed `instrument.resolved_ticker/provider_symbols/source/currency/unit`. Unknown fields are rejected. No depth parameter is implemented. Symbols are validated; no request value is a shell expression.

`intent=descriptive/factor_research` retains research diagnostics but vetoes a reportable future direction/probability. For ordinary descriptive requests the Skill uses its minimum-data route instead of this expensive CLI. `intent=forecast` is required for delivery of forecast estimates. Nonfatal data/validation gaps degrade reporting; they do not prevent Quant from running.

## Research result

Final Synthesis retains consensus `prob_up/direction/confidence/agreement`, confidence score/reasons, report status and probability basis under `synthesis.quant`; it carries raw, calibrated and reportable probability fields separately. Agent summaries expose the reportable estimate and basis. See [probability-policy.md](probability-policy.md). Categorical confidence/agreement are not numerical probabilities.

Each path writes the same top-level fields:

```json
{
  "researcher_id": "quant",
  "result_role": "forecast",
  "ticker": "NVDA",
  "horizon": "5D",
  "snapshot_id": "...",
  "direction": "bullish|bearish|neutral|unavailable",
  "prob_up": 0.61,
  "prob_down": 0.39,
  "raw_probability": 0.62,
  "calibrated_probability": null,
  "reportable_probability": 0.61,
  "expected_return": 0.012,
  "confidence": null,
  "probability_source": "reproducible method identifier",
  "evidence": {"positive": [], "negative": []},
  "validation": {},
  "data_used": [],
  "warnings": [],
  "status": "success|partial|failed|insufficient_data|invalid"
}
```

`researcher_id` is one of `quant`, `factor`, `ml`, or `factor_backtest`. `result_role` is `forecast` or `diagnostic`; legacy results without it are treated as forecasts. The `factor_backtest` path is diagnostic-only for a single-security snapshot and must not emit a consensus probability.

Unavailable numeric values are JSON `null`. A numeric probability requires a non-empty `probability_source` and must be in `[0,1]`. The `direction` field is derived from 0.5; it is not a vote. `confidence` remains null unless a researcher has a separately defined, validated confidence measure.

The orchestrator adds prediction/reporting eligibility, exclusion reasons, OOS/calibration metadata, confidence score and `horizon_type`; `invalid` paths are excluded, while the combined prediction is vetoed only for detected leakage or another fatal integrity issue. Validation/strategy gaps lower report confidence; PBO/DSR and portfolio audits are not probability gates. D never votes. Final output adds reporting_status, analysis_status, stages, summary `agent_summary.json`, concise `final_response.md`, standard `report.md`, and debug JSON. Generic/brief stock or ETF analysis defaults to intent=forecast. Preserve literal bound fields from final_response.md and validate the actual prepared reply with validate-response.

## Status meanings

- `success`: required calculation and its declared validation path completed.
- `partial`: a numeric path result exists but a required model family, calibration or validation component is absent.
- `failed`: runtime error; independent paths may continue.
- `insufficient_data`: the path cannot support its predeclared minimum sample.
- `invalid`: this path's lineage/validation invariant failed; exclude the path. The combined direction is vetoed only when a fatal pipeline-integrity condition (such as detected leakage) is confirmed.
