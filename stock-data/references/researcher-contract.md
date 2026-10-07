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

Allowed markets: `US`, `HK`. Allowed horizons: `1D`, `5D`, `20D`; default is `5D`. Horizons count valid exchange sessions, never calendar days. `asof`, when present, must be an ISO-8601 timestamp with an explicit timezone.

Optional request fields: `intent` (`forecast`, `descriptive`, `factor_research`), `asset_type` (`stock`, `etf`, `index`, `unknown`), `history_window.start/end/selection` (or history_start/end), and source-backed `instrument.resolved_ticker/provider_symbols/source/currency/unit`. Unknown fields are rejected. No depth parameter is implemented. Symbols are validated; no request value is a shell expression.

`intent=descriptive/factor_research` retains research diagnostics but vetoes a reportable future direction/probability. For ordinary descriptive requests the Skill uses its minimum-data route instead of this expensive CLI. `intent=forecast` is required for delivery of forecast estimates; it still cannot bypass data/OOS/audit gates.

## Research result

Final Synthesis retains the reportable consensus fields under `synthesis.quant`: `prob_up/direction/confidence/agreement` plus `diversity` if supplied. Agent summaries and final user replies must expose these fields; see [probability-policy.md](probability-policy.md). Categorical confidence/agreement are not numerical probabilities.

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

The orchestrator adds `forecast_eligible`, exclusion reasons and `probability_calibration`, independently of calculation status. Strict OOS/calibration and dataset gates are in [reliability-gates.md](reliability-gates.md). D never votes. Final output adds analysis_status, stages, eight-section agent_summary.json and a compact final_response.md; CLI success only means artifacts were written. Generic/brief stock or ETF analysis defaults to intent=forecast, not descriptive. Preserve literal bound fields from final_response.md and validate the actual prepared reply with validate-response before delivery, including explicit probability unavailability and its reason.

## Status meanings

- `success`: required calculation and its declared validation path completed.
- `partial`: a numeric path result exists but a required model family, calibration or validation component is absent.
- `failed`: runtime error; independent paths may continue.
- `insufficient_data`: the path cannot support its predeclared minimum sample.
- `invalid`: data lineage or validation invariant failed; the adversarial audit may veto the combined direction.
