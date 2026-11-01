# News Impact Rubric

Deterministic grading applied by `scripts/quant_research/news/impact.py` to every eligible event card. Results land in `news_result.json` under `events[].impact` and never leave the News layer.

The taxonomy is adapted from the MIT-licensed `market-news-analyst` skill (<https://github.com/tradermonty/claude-trading-skills>, Copyright (c) 2026 TraderMonty). Only the deterministic, frozen-bar computable parts were reused. Attribution and licence notes are recorded in [upstream-provenance.md](upstream-provenance.md).

## What was deliberately not adopted

- **The composite Impact Score** (`price score x breadth multiplier x forward modifier`, e.g. `37.5`). It is an uncalibrated pseudo-precise number. This runtime does not produce it: `events[].impact.composite_impact_score` is always `not_implemented`, consistent with the confidence and probability contracts in [news-analysis.md](news-analysis.md).
- **Cross-asset market reaction analysis.** Upstream compares index, sector, Treasury, FX, commodity and VIX moves. The News layer reads only the one frozen OHLCV series for the analysed ticker, so those bands are unobservable here and are not implemented.
- **WebSearch/WebFetch collection.** News is collected by `scripts/news_collect.py` from SEC EDGAR, registered IR RSS feeds, and Finnhub REST (plus optional host MCP). See [news-analysis.md](news-analysis.md).
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
| `not_assessed` | no mature frozen session follows the event, or the reaction status/window does not support attribution |

A comparison tolerance of `1e-9` (`band_tolerance`) is applied at each edge. A close-to-close return computed from prices is rarely exactly on a threshold — `90/100-1` evaluates to `-0.09999999999999998` while `110/100-1` evaluates to `+0.10000000000000009` — so without slack the same 10% move would grade `severe` in one direction and `major` in the other.

The band is withheld when the reaction semantics cannot support attributing the observed session move to the event window: an `intraday_daily_association` whole-session return includes pre-publication trading, and a `not_classifiable` reaction has no usable direction semantics. In that case `observed_abs_return` is still reported when it exists — only the band is not asserted. This gate mirrors the pattern comparison so the two halves of the impact block never disagree about whether the same reaction is usable.

This is a single-instrument band. It is not an index, sector or intraday move, and it is not an expected return. Every result carries `calibration_status: heuristic_uncalibrated`.

Absence of magnitude is never evidence that an event was unimportant: it may already have been priced in, offset by concurrent events, or observed on a session the news did not cause.

## Pattern comparison

Compares the observed move against the symbol's **own** trailing return sigma, taken from up to the last 20 returns strictly before the event session, so the result is reproducible offline from frozen bars. The actual number of returns used is reported as `trailing_sigma_observations` (minimum 5); a shorter baseline is never described as a full 20-session window. No cross-asset historical pattern library is used.

A full window of twenty returns requires **21 closing prices**, so the window slice starts one bar earlier than `index - window` would suggest. Taking `index - window` yields only 19 returns and a systematically different sigma.

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

Matching is **exact on a normalized alias**, never a substring test (`match_rule: exact_normalized_alias_only`). A substring rule is unsafe at this granularity: it promotes "Security Finance Blog" to `tier1_official_primary` because the name contains "sec", and "Capital.com" to `tier2_major_financial_news` because it contains "ap". Names are lower-cased, whitespace-collapsed and trimmed of surrounding punctuation; a leading article and a trailing `.com`/`.org`/`.net` suffix are folded before the lookup, then looked up. Anything absent from the curated alias table stays unclassified.

Explicit syndication credits use `original via relay` or `relay from original`; only the named original decides the tier, even when an unknown original is relayed by a recognized outlet. Punctuation-only credits such as `Reuters - Yahoo Finance` are treated as original/relay only when all suffixes are recognized aggregators; other multi-publisher credits use the weakest tier. The relaying platform alone (`Yahoo Finance`) is `tier4_analysis_and_aggregator`.

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

If both Markdown files are lost, the grading is not recoverable; use `--keep-json` when the full `news_result.json` impact block must be retained. Note that the retained Markdown is itself bounded by `gc --retain-days` (default 30 days), so a grading that must survive longer has to be archived outside `runtime/` or kept with `--keep-json`.

## Attribution caveats

Always attached to every event card:

- Daily-bar association is temporal only; it does not establish causation.
- No magnitude is not evidence of low importance.
- Concurrent same-session events cannot be separated in one close-to-close return.
- Recency is not importance.
- Multiple concurrent events are not separable; any single-event attribution is one candidate explanation.
- Publisher tier is an editorial label, not authentication.

## Publication timing bucket

Every event card carries `impact.event_time_bucket`:

| Bucket | Derived from | Information path |
|---|---|---|
| `pre_market` | publication before `close_at - 6.5h` | pre-open positioning |
| `intraday` | publication inside the regular session | same-session |
| `after_close` | publication after `close_at` | overnight gap into the next session |
| `unknown` | no mapped session or close time | not asserted |

The bucket exists because timing, not direction, is where the measured effect
lives: on 318 real 8-K Item 2.02 releases the absolute 5-day move was 5.28% for
after-close releases and 2.57% for pre-market releases. A single magnitude band
without the bucket hides a two-fold difference. The bucket never carries a
direction and never enters any score.

## Priced-in judgement

`already_priced_in` is descriptive and separate from attribution:

- `likely_priced_in` — `|event-session move| >= 1 sigma_5d` and the following
  5 sessions did not continue in the event's direction;
- `continued` — the same move threshold, followed by same-direction drift;
- `move_within_noise` — the move is below one 5-day sigma, so nothing is claimed;
- `not_assessable` — the reaction is not classifiable or the 5-session window has
  not matured in the frozen bars.

At the result level the state is `likely_priced_in` when at least three event
windows are assessable and at least half of them show no continuation. This is
the honest answer to "was the news already in the price?"; it is not a forecast.

## Boundaries

These grades are descriptive heuristics inside the News stage. They never produce a probability, never produce a direction conclusion, and never feed any score. They are descriptive context only.

## strength_v1 (v4 news judgement)

每条直接相关候选由宿主 AI 给出 `lean`（`bullish|bearish|neutral|mixed|unclear`）与 `reason`，可给出 `strength` 与 `event_type`。程序按以下固定规则复核；若 AI 声明的 `strength` 与规则结果不一致，整个评估被拒绝。规则版本固定为 `strength_v1`；看到结果后不得改规则，要改只能升版本号并记录原因。

- **强**：同时满足 (a) 直接相关；(b) 来源为 `A_primary`，或核实状态为 `primary_confirmed` / `multi_source`，且 `verification_pit != post_hoc`；(c) 事件类型属于强事件表；(d) 标题或摘要含具体数量（金额、比例、日期、数量之一）。
- **中**：直接相关，且满足 (b)，但事件类型属于中事件表，或属于强事件表却缺具体数量。
- **弱**：间接相关；或核实状态为 `single_source` / `unverified` / `not_assessed`；或事件类型属于弱事件表。

事件表维护在 [news-sources.json](news-sources.json) 的 `event_tables`。**事件表与“少于 5 条直接相关就标注样本过少”的阈值都是人为约定，没有统计依据，必须写进交付的“局限”。**

`verification_pit=post_hoc` 的条目不得进入“强”档。`A_primary` 的发行人新闻稿属于发行人陈述，AI 判断须写明这一点。

## 描述性矩阵

- 输出 `lean × 强/中/弱` 条数矩阵，近 30 天与第 31–60 天分开。
- 每类 lean 最强至多 3 条，按强度、再按发布时间由新到旧；每条必须有原文链接。
- 直接相关条目少于 5 条时矩阵照列，但标注“样本过少，不作倾向性描述”，不输出整体性措辞。
- 不输出概率、方向结论、综合分数或加权求和；`validate-response` 检测到这些模式时拒绝。
