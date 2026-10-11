# News Impact Rubric

Deterministic grading applied by `scripts/quant_research/news/impact.py` to every eligible event card. Results land in `news_result.json` under `events[].impact` and never leave the News layer.

The taxonomy is adapted from the MIT-licensed `market-news-analyst` skill (<https://github.com/tradermonty/claude-trading-skills>, Copyright (c) 2026 TraderMonty). Only the deterministic, frozen-bar computable parts were reused. Attribution and licence notes are recorded in [upstream-provenance.md](upstream-provenance.md).

## What was deliberately not adopted

- **The composite Impact Score** (`price score x breadth multiplier x forward modifier`, e.g. `37.5`). It is an uncalibrated pseudo-precise number. This runtime does not produce it: `events[].impact.composite_impact_score` is always `not_implemented`, consistent with the confidence and probability contracts in [probability-policy.md](probability-policy.md).
- **Cross-asset market reaction analysis.** Upstream compares index, sector, Treasury, FX, commodity and VIX moves. The News layer reads only the one frozen OHLCV series for the analysed ticker, so those bands are unobservable here and are not implemented.
- **WebSearch/WebFetch collection.** News comes only from the host's Finnhub MCP `get-news-pulse` tool during Step 2 collection. See [news-analysis.md](news-analysis.md).
- **English-only output and the long ranked report.** Output stays inside the bounded summary contracts in [news-analysis.md](news-analysis.md).

## Impact magnitude

Graded from the single observed `daily_close_return` (close-to-close) of the first frozen session ending after publication.

| Band | Absolute return |
|---|---|
| `severe` | >= 10% |
| `major` | >= 5% |
| `moderate` | >= 2% |
| `minor` | >= 0.5% |
| `negligible` | < 0.5% |
| `not_assessed` | no mature frozen session follows the event |

A comparison tolerance of `1e-9` (`band_tolerance`) is applied at each edge. A close-to-close return computed from prices is rarely exactly on a threshold — `90/100-1` evaluates to `-0.09999999999999998` while `110/100-1` evaluates to `+0.10000000000000009` — so without slack the same 10% move would grade `severe` in one direction and `major` in the other.

This is a single-instrument band. It is not an index, sector or intraday move, and it is not an expected return. Every result carries `calibration_status: heuristic_uncalibrated`.

Absence of magnitude is never evidence that an event was unimportant: it may already have been priced in, offset by concurrent events, or observed on a session the news did not cause.

## Pattern comparison

Compares the observed move against the symbol's **own** trailing 20-session return sigma, so the result is reproducible offline from frozen bars. No cross-asset historical pattern library is used.

Twenty returns require **21 closing prices**, so the window slice starts one bar earlier than `index - window` would suggest. Taking `index - window` yields only 19 returns and a systematically different sigma.

| Pattern | Condition |
|---|---|
| `inverse` | observed price direction opposes the event direction |
| `amplified` | `abs(return) / sigma >= 1.0` and direction agrees |
| `consistent` | `0.5 <= ratio < 1.0` |
| `dampened` | `ratio < 0.5` |
| `not_assessed` | no usable reaction, intraday window, missing session, or a degenerate/short volatility baseline |

A trailing sigma below `1e-4` per session (1bp) is treated as a degenerate baseline, because a near-constant return series would otherwise label every event `amplified`.

## Breadth scope

A **declared** transmission scope derived from the deterministic event taxonomy, never an observation.

| Event type | Declared scope |
|---|---|
| `MACRO` | `cross_asset_candidate` |
| `EARNINGS`, `GUIDANCE`, `ANALYST_REVISION` | `company_and_sector_candidate` |
| `SUPPLY_CHAIN`, `COMPETITION`, `REGULATORY` | `sector_candidate` |
| everything else | `company_specific_candidate` |

`observed_breadth` is always `not_assessed` and `observation_limit` is always `single_frozen_ticker_ohlcv_only`. A declared scope must not be reported as measured contagion.

## Source tier

Editorial reliability labels applied per publisher; a cluster takes the **weakest** tier present, so one unrecognized publisher cannot upgrade the rest.

Matching is **exact on a normalized alias**, never a substring test (`match_rule: exact_normalized_alias_only`). A substring rule is unsafe at this granularity: it promotes "Security Finance Blog" to `tier1_official_primary` because the name contains "sec", and "Capital.com" to `tier2_major_financial_news` because it contains "ap". Names are lower-cased, whitespace-collapsed and trimmed of surrounding punctuation, then looked up.

A syndication credit naming the original outlet (`Reuters - Yahoo Finance`, `Reuters via Yahoo`) is unwrapped once, and the named original decides the tier. The relaying platform alone (`Yahoo Finance`) is `tier4_analysis_and_aggregator`.

| Tier | Meaning |
|---|---|
| `tier1_official_primary` | regulator, central bank, exchange or issuer filing |
| `tier2_major_financial_news` | major newsroom or wire service |
| `tier3_specialized_real_time` | specialized or real-time market coverage |
| `tier4_analysis_and_aggregator` | analysis, commentary, or an aggregator relaying another publisher |
| `unclassified_publisher` | absent or unrecognized; no tier asserted |

Unknown names stay `unclassified_publisher` and are never promoted. A publisher name is a source claim: it does not authenticate the supplier, establish independence between outlets, or prove any specific factual claim.

## Where the grading appears

The impact block is computed inside the frozen News stage. Because a normal prediction run executes `cleanup-run`, which deletes every JSON artifact and keeps only `report.md` and `final_response.md`, the grading is also projected into the retained reports:

- `report.md` — one `Impact grading:` line per event with magnitude, observed return, pattern, sigma ratio, source tier, declared vs observed breadth and composite status, followed by the full caveat block above.
- `agent_summary.json` — a compact per-event projection plus a single `impact_rubric` marker. This file is itself deleted by cleanup and its size is budget-bounded, so it carries only short fields.

If both Markdown files are lost, the grading is not recoverable; use `--keep-json` when the full `news_result.json` impact block must be retained.

## Attribution caveats

Always attached to every event card:

- Daily-bar association is temporal only; it does not establish causation.
- No magnitude is not evidence of low importance.
- Concurrent same-session events cannot be separated in one close-to-close return.
- Recency is not importance.
- Multiple concurrent events are not separable; any single-event attribution is one candidate explanation.
- Publisher tier is an editorial label, not authentication.

## Boundaries

These grades are descriptive heuristics inside the News stage. They never produce a probability, never change the frozen Quant result, never restore a vetoed forecast, and never feed back into A/B/C/D. Final Synthesis may describe them as supporting context and must keep the original Quant P(up).