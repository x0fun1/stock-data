# Stock-Data Collection Adapter

The adapter is a file contract, not another data fetcher. Collect market data with the Yahoo gateway; collect news with `scripts/news_collect.py` (SEC + IR RSS + Finnhub) and verify with `scripts/news_verify.py` (TinyFish; relevance first, relative dates resolved at request time, first 429 stops further requests). Preserve every provider response, normalize only documented fields, then pass the response map through `quant_research.py adapt` and the receipt to `quant_research.py freeze`.

## Market collection

`yahoo_collect.py` collects only the `market` domain. It does not fetch news and does not run research.

```text
python3 stock-data/scripts/yahoo_collect.py --request request.json --output gateway-responses.json --cache-dir runtime/yfinance-cache
python3 stock-data/scripts/quant_research.py adapt --request request.json --responses gateway-responses.json --output collected.json
python3 stock-data/scripts/quant_research.py freeze --input collected.json --output-root runtime/snapshots
```

Rules:

- `market.data.bars[]` must be strictly ascending, unique daily `date/open/high/low/close/volume`; no future-dated bars.
- Descriptive trend statistics use at most the latest 52 weeks (about 250 exchange sessions). Do not extend the window to manufacture samples.
- Provider metadata exposes a currency (for example `USD`) but not a trade/volume unit. Record the currency as evidence; leave `unit` null and disclose it. A missing unit is not a reason to mark a news-focused collection ineligible.
- Optional `session_calendar` must be source-backed, timezone-aware and ordered. Without a verified calendar, only descriptive output is allowed and the missing-calendar state is disclosed. If no calendar library (`exchange_calendars` / `pandas_market_calendars`) is installed, emit `CALENDAR_LIB_MISSING` explicitly instead of silently degrading.
- Keep the original provider envelope under `gateway_envelope`; remove credentials, cookies and sensitive query strings before writing.
- Domain status is `complete`, `partial`, `available`, `unavailable` or `failed`. A failed domain does not erase other domains.

## News collection

```text
python3 stock-data/scripts/news_collect.py --ticker GOOGL --as-of 2026-11-01T21:00:00Z --output news_collected.json
python3 stock-data/scripts/news_verify.py --candidates news_collected.json --output news_verified.json
python3 stock-data/scripts/quant_research.py news-candidates --snapshot-dir <snapshot> --collected news_collected.json --verification news_verified.json --output news_candidates.json
```

- Fixed window: `(as-of − 60 days, as-of]`; report recent 30 days and days 31–60 separately.
- `pit_status=published_at_only`: the collector keeps `published_at <= as-of`, but cannot reconstruct what was visible at that historical instant.
- Each source has its own status: `ok`, `empty`, `not_configured`, `not_configured_for_ticker`, `not_applicable`, `rate_limited`, `error`; IR feeds add `partial_window` when the earliest item is later than the window start.
- SEC filings use `acceptanceDateTime` (UTC) or `filingDate` as a date-only fallback. Form 4 and Form 144 are counted only, not sent to the AI.
- Finnhub `related` is always the query symbol, so relevance uses title+summary matching against `company_aliases` and the ticker. Tickers without aliases are marked `relevance_unassessed` and disclosed rather than passed silently or dropped. The key travels only in `X-Finnhub-Token`.
- Source tiers (`A_primary`, `B_media`, `C_aggregator`) and IR feeds live in [news-sources.json](news-sources.json), maintained by a person. Finnhub rows use the `finnhub_source_tiers` map keyed by the provider `source` name; unmapped names are `C_aggregator`.
- Yahoo Finance and The Economist are static reference links only. Finnhub rows whose `origin_source` is Yahoo may keep their summaries for host-AI review; their original links are never fetched, and Yahoo itself is never requested or parsed.

## Receipt shape (illustrative)

```json
{
  "request": {"ticker": "NVDA", "market": "US", "horizon": "60D", "intent": "news", "asof": null},
  "captured_at_utc": "2026-10-07T00:00:00Z",
  "domains": {
    "market": {
      "status": "complete",
      "actual_source": "Yahoo Finance",
      "source_timestamp": "2026-10-06T20:00:00Z",
      "fetched_at_utc": "2026-10-07T00:00:00Z",
      "currency": "USD",
      "unit": null,
      "adjustment": "provider",
      "price_basis": "split_adjusted",
      "data": {
        "history_window": {"start": "2025-10-07", "end": "2026-10-06", "selection": "trailing 52 weeks"},
        "bars": [{"date": "2026-10-06", "open": 180.0, "high": 182.0, "low": 179.0, "close": 181.0, "volume": 1000000}]
      }
    },
    "fundamentals": {"status": "unavailable", "actual_source": null, "data": null},
    "news": {
      "status": "complete",
      "actual_source": "news_collect.py",
      "fetched_at_utc": "2026-10-07T00:00:00Z",
      "data": {"articles": [
        {"article_id": "sec_0000000000-26-000001", "url": "https://www.sec.gov/Archives/edgar/data/0/000000000026000001/0000000000-26-000001-index.htm",
         "title": "8-K filing", "summary": null, "source": "SEC EDGAR", "source_type": "sec",
         "record_type": "filing", "published_at": "2026-10-06T20:05:00Z", "tickers": ["NVDA"]}
      ]}
    }
  }
}
```

Values are illustrative, not market data. Never copy example values into a report.

## Analyze

`analyze --snapshot-dir <snapshot> --news-collected news_collected.json --news-verification news_verified.json --news-ai-assessment assessment.json` validates the assessment against collector candidate IDs, computes deterministic price reaction/volatility fields, and writes the descriptive summary. `news_result.json` and `analysis.json` remain frozen and digest-checked; `validate-response` must pass before delivery.

## Known news-field mappings

| Source response | Normalized field | Rule |
|---|---|---|
| Finnhub company-news `headline` | article `title` | Preserve headline text; no body text is fetched |
| Finnhub `url` | article `url` | Keep provider URL; never fetch forbidden domains |
| Finnhub `source` | article `source` | Publisher name as supplied |
| Finnhub `datetime` | article `published_at` | Unix seconds → UTC ISO |
| Finnhub `related` | article `related` | Comma-separated provider tags; match ticker/aliases |
| SEC `acceptanceDateTime` | article `published_at` | UTC; `filingDate` fallback is date-only |
| SEC `items` | article `items` | 8-K item codes; titles from the human-maintained map |
| IR RSS `pubDate` | article `published_at` | RFC 822 → UTC ISO; when the feed timezone cannot be proven, keep `time_precision=date` and do not use it for intraday sorting or PIT intraday comparison |

A headline-only feed cannot support body-level analysis. The report must disclose title/summary-level judgement, and the AI assessment must not claim to have read the body.
