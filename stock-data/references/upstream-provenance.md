# Upstream Provenance

The gateway retains its historical source integrations. This v4 runtime is news-focused: the former prediction paths (A–E), factor/ML researchers and forward ledger were removed. License observations below are historical records, not a new remote verification or a repository-wide license grant.

## Historical gateway sources

- **Finnhub stock analysis 0.1.0**: archived Skill metadata named the authors as `xo, Hermes Agent` and declared `license: MIT`. No complete license text accompanied that archive; the declaration is retained here without claiming independent verification. Finnhub MCP connectivity remains supplied by the host.
- **global-stock-data 2.0.3** (archived filename/version label): [simonlin1212/global-stock-data](https://github.com/simonlin1212/global-stock-data), author Simon 林. The historical archive included gateway source; its repository-level reuse license was not established. Removing that archive does not remove applicable attribution or license obligations.
- **Gateway**: [x0fun1/stock-data](https://github.com/x0fun1/stock-data). Local source-routing/schema references, gateway functions and offline tests; retains the existing Yahoo/Finnhub/global routes. Repository-level reuse license was not established in that review; this is an update within this repository.

## News sources (v4)

| Source | What is used | Terms / limits |
|---|---|---|
| SEC EDGAR `data.sec.gov` / `www.sec.gov` | `company_tickers.json` and `submissions/CIK##########.json`; filings metadata only, no document body | No API key; requires a User-Agent with contact details; official limit 10 requests/second, this package uses ≤2/second. US issuers only. |
| Company IR RSS | Publisher press-release feeds registered in [news-sources.json](news-sources.json) | Feed use follows each publisher's robots.txt and `Crawl-delay` (currently 10 s). Feeds are capped and may not cover 60 days; `partial_window` is disclosed. New feeds require a person to verify robots and a 200 response. |
| Finnhub REST | `GET /api/v1/company-news` headlines, source, URL, summary, `related`, Unix timestamp | Free tier is for personal use only and must not be redistributed. Quota and history depth follow the official page; 429 is reported as `rate_limited` without retry. Key is passed only in `X-Finnhub-Token`. |
| TinyFish Search | `GET https://api.search.tinyfish.ai` ranked news results (title, URL, date, publisher) | Free daily allowance; per-minute rate limit; snippets are not stored. |
| TinyFish Fetch | `POST https://api.fetch.tinyfish.ai` up to 3 whitelisted URLs per candidate | Free daily URL allowance; Fetch text is discarded at runtime and never written to an artifact. Its content-storage/AI-processing terms were not verified. |
| Yahoo Finance / The Economist | Static reader links only | The program never requests, parses or stores their pages or feeds. |

## Removed prediction components

The 52-week/5-session probability estimator, factor and ML shadow paths, factor backtest diagnostic, forward ledger, consensus/direction-claim layer and their self-check fixtures were deleted in v4. No direction probability, composite score or trading signal is produced. Market OHLCV and descriptive price-reaction statistics remain.

## News impact taxonomy

The deterministic impact-magnitude taxonomy and source-tier ideas were adapted from the MIT-licensed `market-news-analyst` skill (<https://github.com/tradermonty/claude-trading-skills>, Copyright (c) 2026 TraderMonty). Only the deterministic, frozen-bar computable parts were reused; the composite Impact Score, cross-asset reaction analysis and web-search collection were deliberately not adopted. No upstream source, prompt or text was copied.

`strength_v1` event tables, the `minimum_direct_items=5` threshold and source tiers are human-maintained conventions, not statistical estimates. They are recorded in [news-sources.json](news-sources.json) and [news-impact-rubric.md](news-impact-rubric.md).

Historical source archives and review reports are not part of this runtime package. Providing more inputs does not turn local code into a full upstream engine. Report unsupported checks as not assessed.
