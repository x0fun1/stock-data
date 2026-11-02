# Audit Protocol (v4)

The former probability audit was removed with the direction-prediction path. This document now covers the checks that remain: frozen-input integrity, news collection/verification disclosure, and delivery validation.

## 1. Input integrity

- Every analysis reads one frozen snapshot; `load_snapshot` verifies the manifest digest, per-domain digests, ticker/market/as-of identity and bar count.
- Market bars must be ascending, unique, numeric and not after the as-of date. Unclosed same-as-of bars are excluded and disclosed.
- Unknown currency/unit/calendar evidence stays null or `unverified`; it is never replaced with a guessed value.

## 2. News disclosure

- The fixed window is `(as-of − 60 days, as-of]`; recent 30 days and days 31–60 are reported separately.
- Every source is reported independently with `ok/empty/not_configured/not_configured_for_ticker/not_applicable/rate_limited/error`, HTTP status where available, observed first/last publication time, and `partial_window`.
- All stored records satisfy `published_at <= as-of`; the output states `pit_status=published_at_only`.
- One source failure never erases another source, and a failed request is never replaced by a cached result.

## 3. Verification

- SEC/IR records are `primary_confirmed` without an extra request.
- At most 30 third-party records are verified per run; the remainder are `not_assessed` with the cap reason.
- Search/Fetch request counts are reported; Fetch is limited to 3 whitelisted URLs per record.
- Fetch text and Search snippets are never written to artifacts.
- `verification_pit=post_hoc` items cannot be rated strong.

## 4. AI assessment and summary

- Every candidate ID must exist in the run, must be unique, and ticker/as-of must match the snapshot.
- Identity fields (`headline`, `source`, `published_at`, `url`) are copied, never authored; a mismatch rejects the artifact.
- `strength_v2` is recomputed from source tier, verification status, event table and the presence of a quantity. An AI-declared strength that disagrees is rejected.
- The matrix must equal a fresh recomputation from the direct items; key items must have an original URL.
- No probability, composite score, direction conclusion, or buy/sell/position advice is allowed.

## 5. Delivery mutation checks

`validate-response` rejects:

- changed matrix counts;
- a key item without its original link;
- added forbidden language (the exact tokens are intentionally not repeated as user-facing output);
- a missing `局限` line;
- `partial_window` rewritten as full coverage;
- a `post_hoc` item labelled strong.

## 6. Limitations

The audit does not prove provider authenticity, same-event semantics, syndication independence, or statistical validity of the event tables. Those are disclosed as limitations, not scored.
