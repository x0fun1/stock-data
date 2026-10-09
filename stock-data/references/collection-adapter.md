# Stock-Data Collection Adapter

Schema 1.2 source evidence, reason codes and the three-level reporting gate are defined in [reliability-gates.md](reliability-gates.md). The illustrative minimal envelope below does not pass the minimum OHLCV history requirement. Never invent source, timestamp, adjustment or calendar evidence. Calendar/adjustment evidence gaps alone degrade confidence; fatal OHLCV/provenance conflicts block prediction. `make_receipt()` retains caller-normalized data; raw envelopes cannot replace bars/articles, and known outer/inner provider failures discard their payloads.

The adapter is a file contract, not another data fetcher. Collect data with this Skill's Finnhub/global route, preserve every gateway response, normalize only the fields required for research, then pass the response map through `quant_research.py adapt` and the receipt to `quant_research.py freeze`.

## Yahoo collector response map

`yahoo_collect.py` collects only market/news (default both; `--domains market` or `--domains news` supplements a missing domain). Its Yahoo route always tries yfinance first, then automatically invokes a semantically compatible legacy route after an error, empty result, or unavailable dependency/capability; there is no backend-selection option. Partial yfinance data is retained. The collector does not rerun Finnhub or execute research. Keep the existing request schema; provider-specific settings belong to collection CLI/config, not research parameters. The host reuses successful Finnhub responses. See [yfinance-data.md](yfinance-data.md) and current CLI help.

```text
python stock-data/scripts/yahoo_collect.py --request request.json --domains market news --output gateway-responses.json --cache-dir runtime/yfinance-cache
python stock-data/scripts/quant_research.py pipeline --request request.json --responses gateway-responses.json --output-root runtime
```

Each top-level market/news domain uses complete/partial/unavailable/failed, actual_source identifying the interface actually used, distinct source identity/time and fetched_at_utc, confirmed currency/unit/frequency, and data.bars/data.articles. Fallback metadata (`fallback_attempted`, `fallback_used`, `fallback_route`, `fallback_reason`) is retained through the collector and adapter. Market retains actual history_window, adjustment/evidence and evidenced last_bar_closed/session_calendar. gateway_envelope retains sanitized provider-native results, library/version, parameters and transformations; raw HTTP provenance is claimed only when captured. Missing evidence stays unknown. In particular, history `source_timestamp_kind=bar_label_not_trade_time` is an index label, not a close/quote update; it may fail the existing 24h freshness/source_before_close gates even for a calendar-confirmed closed bar. Unconfirmed source `unit` remains null. Never replace these with fetched_at, an invented close time or currency-derived unit. Response-file generation is not proof of valid predictive input; actual audit must pass unchanged gates. Provenance additions are optional; schema 1.2 gains no required fields.

Bars use exchange session dates with UTC timestamp/timezone preserved. Inclusive research end becomes next local day's exclusive provider end. Keep provider OHLC/Adj Close/actions and explicit adjusted ratios; do not fill absent bars or volume. Current metadata/empty actions do not prove complete calendar/action coverage. Compare matching session/currency/price basis only; old Yahoo direct access and yfinance are one source.

Stream and Search news are independently mapped. Query symbol is not an article ticker tag; source publication time and explicit related-ticker evidence follow existing NewsInput rules. Updated time is not publication time; advertisements excluded, unverified type unknown, absent sentiment/relevance scores not invented. Short feeds disclose limited coverage, not historical PIT availability.

Domain failures are independent: failed news does not erase valid bars; network failure is not successful empty data. Partial exposes gaps even with exit 0. Freeze gates and Quant → audit → News → synthesis unchanged; news unavailable becomes not_assessed, never neutral. Existing non-Yahoo normalization remains supported.

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
          "start": "2026-10-06",
          "end": "2026-10-06",
          "selection": "illustrative one-bar actual normalized window"
        },
        "bars": [
          {"date": "2026-10-06", "open": 180.0, "high": 182.0, "low": 179.0, "close": 181.0, "volume": 1000000}
        ]
      },
      "gateway_envelope": {"data_role": "redacted provider DATA; no action fields"}
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
        "ticker": "NVDA", "available_at": null, "percent_scale": "fraction",
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

For the market response, first map provider-specific candles to `data.bars` using the gateway schema and the exchange's session dates. Retain the provider DATA in `gateway_envelope`; archiving removes credentials/action fields. The adapter will not infer Finnhub/Yahoo field mappings or synthesize timestamps.

## Rules

- `market.data.bars` is required, with strictly ascending unique daily observations and fields `date`, `open`, `high`, `low`, `close`, `volume`. A complete resolved exchange calendar must match every confirmed session; otherwise missing confirmed bars are fatal. With no complete calendar, use the observed rows in order and declare an estimated-observation horizon (do not invent holiday/session rows or claim calendar verification). Weekend observations and severe unexplained date gaps are fatal.
- The default research window is the latest three calendar years through the latest confirmed closed observation. If a provider returns a longer series, retain its complete envelope under `gateway_envelope` but put only the selected window in normalized `market.data.bars`. Record the actual start, end, and selection rule in `market.data.history_window`. Honor an explicit user-specified window.
- Do not extend the selected window just to meet a path's sample threshold. That path must return `partial`/`insufficient_data` and a null path probability when its own evidence is too small; other valid paths may still supply an available degraded reportable estimate. A longer-window sensitivity run is a separate, clearly labeled analysis.
- Record adjustment and company-action evidence separately. Unknown adjustment does not mean contaminated; short-horizon direction paths may run with degraded confidence. Confirmed in-window corporate action plus unadjusted OHLCV is fatal. Unknown adjustment is not allowed to support total-return, dividend or long-horizon-return claims.
- `session_calendar` is optional. If supplied, it must include source, timezone, `coverage_start`, `coverage_end`, and an ordered `sessions[]` list with date and timezone-aware `close_at`. Optional `latest_price_observations[]` contains independent `source`, `session_date`, `price`, `currency`, and `kind: daily_close` fields. Only same-session/same-currency closes are compared; a >0.3% conflict is fatal, while matching independent closes can verify the latest close without an official calendar.
- Include corporate-action evidence as `adjustment_evidence` with source, `price_basis`, `checked_through`, and `actions[]` records (`date`, `type`). Missing/unverified evidence remains `unavailable`; do not convert it into an assertion that there were no actions.
- `mode: strict` does not make unknown adjustment/OOS/calibration a prediction blocker. Such deficiencies affect report status and confidence, not whether Quant is attempted.
- Keep envelopes under `gateway_envelope`; credentials and externally proposed actions are removed before archiving. The freezer copies that redacted block to `raw/<domain>.json`.
- Use `actual_source`, `source_timestamp`, `fetched_at_utc`, `currency`, `unit`, `fallback_used`, and `fallback_reason` only when the gateway supplied or confirmed them. Do not invent missing provenance.
- Domain `status` must be `complete`, `partial`, `available`, `unavailable`, or `failed`.
- For observations such as news, filings, analyst changes and financial facts, preserve `published_at`, `filed_at`, `period_start`, `period_end`, revision/vintage, units and currency when available.
- Normalize news only into `news.data.articles[]`. Preserve source headline/summary, ID/URL, exact publication/update timestamps with timezone, publisher/source type, explicit record type (`event`, `report`, `news`, `opinion`, `analyst_opinion`, `social`, or `unknown`), tickers and any source-supplied score metadata. Use `unknown` when the source does not distinguish fact from opinion; the classifier will keep unsupported records unclassified. The example values are schema examples only.
- Optional `news.data.source_sentiment` retains raw values without rescaling. Directional use requires recent as-of-valid availability and exact `ticker`. `percent_scale` is `fraction` (default 0..1) or `percent` (0..100); invalid values are excluded. Unknown target/time aggregates are background-only.
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
| Finnhub `get-news-pulse`: `sentiment_score`, `bullish_percent`, `bearish_percent`, `sentiment_source`, `period` | `data.source_sentiment` | Preserve raw values/period, target and available time. Directional use also requires the recent target-specific eligibility gate and declared percent scale. |
| global `stock_news`: `title`, `publisher`, `link`, `publish_time`, `article_id`, `tickers` | article `title`, `source`, `url`, `published_at`, `article_id`, `tickers` | `publish_time` is provider epoch seconds; convert that exact instant to UTC ISO-8601 and preserve related ticker tags. Missing tags cannot be inferred from the query alone. |

Do not infer a sentiment aggregate for a source that returned headlines only. For any other provider, use its current documented schema; otherwise leave the field unavailable.
