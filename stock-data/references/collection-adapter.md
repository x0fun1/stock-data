# Stock-Data Collection Adapter

The adapter is a file contract, not another data fetcher. Collect data with this Skill's Finnhub/global route, preserve every gateway response, normalize only the fields required for research, then pass the response map through `quant_research.py adapt` and the receipt to `quant_research.py freeze`.

## Receipt shape

```json
{
  "request": {
    "ticker": "NVDA",
    "market": "US",
    "horizon": "5D",
    "asof": null,
    "mode": "standard"
  },
  "captured_at_utc": "2026-10-07T00:00:00Z",
  "domains": {
    "market": {
      "status": "complete",
      "actual_source": "source actually used",
      "source_timestamp": "source-provided market timestamp or null",
      "fetched_at_utc": "gateway fetch time with timezone",
      "currency": "USD",
      "unit": "share and native volume units",
      "adjustment": "adjusted",
      "fallback_used": false,
      "fallback_reason": null,
      "data": {
        "history_window": {
          "start": "2023-10-07",
          "end": "2026-10-06",
          "selection": "latest 3 calendar years through latest confirmed closed session"
        },
        "bars": [
          {"date": "2026-10-06", "open": 180.0, "high": 182.0, "low": 179.0, "close": 181.0, "volume": 1000000}
        ]
      },
      "gateway_envelope": {"original": "unmodified response"}
    },
    "fundamentals": {"status": "unavailable", "actual_source": null, "data": null},
    "expectations": {"status": "unavailable", "actual_source": null, "data": null},
    "news": {
      "status": "complete",
      "actual_source": "source actually used",
      "fetched_at_utc": "gateway fetch time with timezone",
      "data": {"articles": [
        {"article_id": "source id or null", "url": "source URL or null", "title": "source headline",
         "summary": "source summary or null", "source": "publisher", "source_type": "news",
         "record_type": "event", "published_at": "source timestamp with timezone",
         "updated_at": "source update timestamp with timezone or null", "tickers": ["NVDA"],
         "event_type": "GUIDANCE_RAISE", "event_strength": null, "relevance": null,
         "magnitude": null, "novelty": null, "source_quality": null}
      ], "source_sentiment": {"source": "provider", "period": "source-defined window",
        "sentiment_source": "source name", "sentiment_score": null,
        "bullish_percent": null, "bearish_percent": null}}
    },
    "insider": {"status": "unavailable", "actual_source": null, "data": null},
    "options": {"status": "unavailable", "actual_source": null, "data": null},
    "short_volume": {"status": "unavailable", "actual_source": null, "data": null},
    "sec": {"status": "unavailable", "actual_source": null, "data": null},
    "macro": {"status": "unavailable", "actual_source": null, "data": null}
  }
}
```

The example bar is illustrative, not market data. Do not copy its values into a report.

## Adapter invocation

Save the standardized research request in `request.json`. Save the actual gateway responses in `gateway-responses.json`, keyed by data domain (`market`, `fundamentals`, etc.). Run:

```text
python stock-data/scripts/quant_research.py adapt --request request.json --responses gateway-responses.json --output collected.json
```

For the market response, first map provider-specific candles to `data.bars` using the gateway schema and the exchange's session dates. Keep the original complete response in `gateway_envelope`. The adapter will not infer Finnhub/Yahoo field mappings or synthesize timestamps.

## Rules

- `market.data.bars` is required, with one daily record per exchange session and strictly ascending unique dates. Fields are `date`, `open`, `high`, `low`, `close`, `volume`.
- The default research window is the latest three calendar years through the latest confirmed closed session. If a provider returns a longer series, retain its complete envelope under `gateway_envelope` but put only the selected window in normalized `market.data.bars`. Record the actual start, end, and selection rule in `market.data.history_window`. Honor an explicit user-specified window.
- Do not extend the selected window just to meet a path's sample threshold. That path must return `partial`/`insufficient_data` and a null probability when its evidence is too small. A longer-window sensitivity run is a separate, clearly labeled analysis.
- Record the price adjustment convention. Unknown adjustment status is a warning and blocks strong historical return claims.
- With `mode: strict`, an unknown adjustment convention rejects the snapshot.
- Keep provider envelopes under `gateway_envelope`; the freezer also copies the original domain block to `raw/<domain>.json`.
- Use `actual_source`, `source_timestamp`, `fetched_at_utc`, `currency`, `unit`, `fallback_used`, and `fallback_reason` only when the gateway supplied or confirmed them. Do not invent missing provenance.
- Domain `status` must be `complete`, `partial`, `available`, `unavailable`, or `failed`.
- For observations such as news, filings, analyst changes and financial facts, preserve `published_at`, `filed_at`, `period_start`, `period_end`, revision/vintage, units and currency when available.
- Normalize news only into `news.data.articles[]`. Preserve source headline/summary, ID/URL, exact publication/update timestamps with timezone, publisher/source type, explicit record type (`event`, `report`, `news`, `opinion`, `analyst_opinion`, `social`, or `unknown`), tickers and any source-supplied score metadata. Use `unknown` when the source does not distinguish fact from opinion; the classifier will keep unsupported records unclassified. The example values are schema examples only.
- Optional provider aggregate sentiment is normalized separately as `news.data.source_sentiment`; preserve its source, period, source label, and values without rescaling. It is usable only when the News domain has a timezone-aware `source_timestamp` or `fetched_at_utc` no later than the shared as-of. It is never treated as per-article sentiment or an event score.
- Optional article-level `sentiment_score` is a source-supplied signed value in `[-1,+1]`; retain it separately from the local opinion-text lexicon and never combine or rescale it without a documented source mapping.
- Do not infer article timestamps from gateway fetch time, map undocumented provider fields, manufacture source-quality/relevance/novelty scores, or claim that a truncated source window is a complete archive. Keep the original response under `gateway_envelope`.
- Articles lacking a timezone-aware `published_at`, published after the snapshot as-of, or updated after as-of are excluded from News analysis. `record_type` and `source_type` determine whether a record enters event analysis or opinion/sentiment analysis; preserve ambiguous values as `unknown`.
- A point-in-time record without availability evidence may be retained for current descriptive context but cannot be used as a historical feature.
- Do not combine data fetched at incompatible as-of times or mix currencies, bar frequencies, or adjustment conventions.

## Known news-field mappings

Use these only when the named source/tool and schema are actually used; keep the full provider envelope:

| Source response | Normalized News field | Rule |
|---|---|---|
| Finnhub `get-news-pulse.top_headlines[]`: `headline`, `url`, `source`, `datetime_utc` | article `title`, `url`, `source`, `published_at` | Preserve the UTC timestamp; `count` and `delta_vs_prev_week` are source context, not event strength or duplicate article count. |
| Finnhub `get-news-pulse`: `sentiment_score`, `bullish_percent`, `bearish_percent`, `sentiment_source`, `period` | `data.source_sentiment` | Preserve raw values and source-defined period; use only if the response availability timestamp is no later than as-of. |
| global `stock_news`: `title`, `publisher`, `link`, `publish_time` | article `title`, `source`, `url`, `published_at` | `publish_time` is provider epoch seconds in the current local gateway contract; convert that exact instant to UTC ISO-8601. |

Do not infer a sentiment aggregate for a source that returned headlines only. For any other provider, use its current documented schema; otherwise leave the field unavailable.
