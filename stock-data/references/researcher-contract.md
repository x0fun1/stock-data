# Request and Researcher Contract

## Request

```json
{"ticker": "NVDA", "market": "US", "horizon": "5D", "asof": null, "mode": "standard", "intent": "forecast"}
```

- Markets: `US`, `HK`.
- `intent=forecast` accepts only horizon `5D`. `1D/20D` are accepted only for
  `descriptive` or `factor_research` and can never be promoted to a forecast.
  An unsupported forecast horizon fails with `UNSUPPORTED_FORECAST_HORIZON`.
- A forecast request may omit `history_window`; the collector computes
  `(decision-364d, decision]`. If supplied, both edges must equal that boundary
  or the collector returns `FIXED_WINDOW_CONFLICT`.
- `asof` must be timezone-aware. Unknown fields are rejected. No depth parameter.
- `instrument` may declare a provider symbol map and a benchmark; undeclared
  values are never guessed.

## Researcher result

Each path writes the same envelope:

```json
{
  "researcher_id": "quant|factor|ml|factor_backtest",
  "result_role": "production|shadow|diagnostic",
  "ticker": "NVDA", "horizon": "5D", "snapshot_id": "...",
  "direction": "bullish|bearish|neutral|unavailable",
  "prob_up": 0.61, "prob_down": 0.39,
  "raw_probability": 0.61, "calibrated_probability": null,
  "reportable_probability": 0.61, "expected_return": 0.012,
  "confidence": null,
  "probability_source": "reproducible method identifier",
  "evidence": {"positive": [], "negative": []},
  "validation": {}, "data_used": [], "warnings": [],
  "status": "success|partial|failed|insufficient_data|invalid"
}
```

`quant` is the only production path. `factor`/`ml` are shadow; their probabilities
are displayed separately and never enter the production number. `factor_backtest`
is diagnostic and must not emit a probability.

A numeric probability requires `probability_source`, `[0,1]` range and a frozen
`validation.probability_transform` for the production path. `calibrated_probability`
is null under policy. `confidence` is null; no 0–100 score is produced.

## Eligibility and status

- `success`: required calculation completed.
- `partial`: a numeric shadow/diagnostic result exists but some declared
  component is absent.
- `failed`: runtime error; other paths continue.
- `insufficient_data`: predeclared sample floor not met.
- `invalid`: lineage/validation invariant failed; excluded.

Production eligibility is decided by the consensus flags, not by a path's own
label: `computable`, `current_prediction_integrity_pass`, `oos_integrity_pass`,
`production_probability_eligible`, `directional_edge_supported` and
`trading_signal_eligible`. See [reliability-gates.md](reliability-gates.md).

## Outputs

The analysis directory contains `analysis.json`, `report.md`,
`agent_summary.json`, `final_response.md`, `audit.json`, `researchers/*.json`,
`news_result.json`, `data.json`, `snapshot.json` and `request.json`. Preserve the
literal fixed block from `final_response.md` and validate the prepared reply with
`validate-response`. Cleanup never touches the snapshot or ledger.