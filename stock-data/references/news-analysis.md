# Post-Quant News / Sentiment Analysis

## Stage order and isolation

The News layer starts only after Research A/B/C/D, probability consensus, and the Quant bias audit have completed. The orchestrator writes `quant_result.json` and its SHA-256 receipt before calling the News runner.

`prepare_news_input(snapshot_dir)` verifies and reads the frozen snapshot, then constructs a restricted `NewsInput` containing only ticker, market, prediction horizon, snapshot ID and `asof_timestamp`; normalized timestamp-valid news records; frozen OHLCV bars from that same snapshot; and news coverage warnings. It does not read the research output directory or any Quant probability, direction, factor, ML, backtest, or audit result. `run_news`/`analyze_news` accepts only `NewsInput` and an optional decay policy; neither entrypoint has a filesystem path. The orchestrator writes and hashes the returned News result. News errors do not erase the already completed Quant result. Final Synthesis is the only stage that loads both digest-verified results.

## Input contract and point-in-time filtering

Normalized records live in `news.data.articles[]`; preserve the original provider response in `gateway_envelope`. Keep the source's publication/update time and timezone. Gateway fetch time is not a substitute for publication time. The local runtime excludes records with missing/invalid timezone-aware `published_at`, `published_at > asof_timestamp`, or a supplied `updated_at > asof_timestamp`.

An unavailable or empty News domain results in `status: not_assessed`, `overall_direction: unknown`, and explicit warnings. Short provider windows stay short; they are not represented as a three-year archive just because the Quant OHLCV window is three years.

## Deduplication and record roles

1. Remove exact duplicate article IDs (within the publisher) and canonicalized URLs.
2. Merge same-role headlines when they are at least 0.88 text-similar and were published within 72 hours. This deliberately conservative first pass does not claim perfect semantic event resolution.
3. Count each resulting factual cluster as one canonical event, preserving article count and distinct sources. Repeated syndication is not multiple independent catalysts.
4. Keep `opinion`, `analyst_opinion`, `social`, `social_post`, and commentary/source-type social records in opinion clusters. These never enter the factual event count. An `unknown` record enters factual event candidates only when the deterministic screen finds a supported event category or source hint; otherwise it stays in a third unclassified bucket and counts as neither an event nor an opinion. Ambiguous records are not silently relabeled.

Event categories include earnings, guidance, analyst revision, product, merger/acquisition, buyback, dividend, capital raise, regulatory, legal, management, insider, supply chain, competition, macro, and unclassified. The initial runtime uses an explicit keyword screen plus a source `event_type` hint when it maps to the supported taxonomy. No match means `UNCLASSIFIED`; no clear tone means direction `unknown`.

Opinion score is a separate simple lexicon in the range `[-1, +1]`, marked uncalibrated. It uses only explicit opinion/social/analyst-opinion items. Optional article-level source sentiment is kept as a separate signed `[-1,+1]` field. A provider aggregate (when explicitly supplied with an as-of-valid availability timestamp) is preserved as a separate provider field and compared without rescaling; it is not treated as per-article sentiment. It does not turn factual event direction into sentiment. Cross-source agreement is computed only where explicit opinion text has a lexicon match. An LLM-authored narrative in a user-facing response must cite the underlying timestamped headlines/opinions and state uncertainty; it cannot add numerical scores.

## Event scores and time decay

The runtime preserves optional source-supplied `event_strength`, `relevance`, `magnitude`, `novelty`, and `source_quality` only when each is a finite value in `[0,1]`. Missing values remain `null`; it does not invent a precision score. `effective_event_score` is computed only when event strength, relevance, novelty, and source quality were all supplied:

```text
event_strength × relevance × novelty × source_quality × time_decay
```

Recency is measured in observed exchange sessions after first publication. The default half-life is the requested horizon in sessions (`1D`, `5D`, or `20D`) as a neutral fallback. An optional JSON policy can override half-lives by event category:

```json
{
  "half_life_sessions_by_event_type": {
    "default": 5,
    "EARNINGS": 10,
    "REGULATORY": 20
  }
}
```

All values are example configuration, not recommended/calibrated values. Run with `analyze --news-policy path/to/policy.json`. The report records the selected half-life and decay for each event.

## Market reaction validation

The validator uses only the frozen snapshot's daily OHLCV. It maps publication to the same local exchange session if published by the nominal 16:00 local close; otherwise it uses the next available frozen session. The mapping is an approximation, especially for intraday headlines because daily bars cannot isolate the exact reaction. It never fetches prices again.

For an available reaction session, the report may include close-to-close return, opening gap versus prior close, abnormal volume z-score versus up to the prior 20 bars (minimum five), pre/post five-return volatility when there are enough matured sessions, and a four-way event-price state:

- positive event + positive close return: `confirmed_positive`;
- positive event + negative close return: `sell_the_news_or_expectations_too_high`;
- negative event + positive close return: `bad_news_priced_in_or_relief`;
- negative event + negative close return: `confirmed_negative`.

This is an observed time association, not causal attribution. Unclassified event direction, insufficient bars, absent benchmark, or immature post-event windows yield unknown/null fields. Sector-relative return is null unless a compatible benchmark is already frozen in the snapshot.

## News result and final synthesis

`news_result.json` includes event bias, separate opinion sentiment, source agreement/divergence, canonical event and opinion counts, event cards, catalysts/risks, market confirmation, warnings, confidence, method labels, and an SHA-256 freeze receipt. It has no `prob_up` field.

Final Synthesis compares only the two frozen stage results. It uses directional event facts first, then explicit sentiment when event direction is unavailable. A direct event/opinion or provider/opinion conflict becomes `MIXED`. It labels `STRONG_ALIGNMENT`, `ALIGNMENT`, `MIXED`, `DIVERGENCE`, `STRONG_DIVERGENCE`, or `NOT_ASSESSED`; a disagreement lowers categorical confidence. It copies the Quant probability unchanged and does not average, adjust, or replace it. News confidence is at most medium in this heuristic first version.

Hard rules:

- News runs after Quant and cannot change Quant outputs.
- News analysts cannot see Quant conclusions before News Result is frozen.
- Articles must obey the common as-of boundary and be deduplicated into canonical events.
- Event facts and opinions/sentiment remain distinct.
- Market reaction uses frozen snapshot prices only.
- News does not generate P(up); only Final Synthesis can compare News and Quant results.
- Missing news coverage or unsupported data means `not_assessed`, not an inferred conclusion.
