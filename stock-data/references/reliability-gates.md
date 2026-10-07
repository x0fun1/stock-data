# Source evidence and reporting gates (schema 1.1)

`data_checks.check_market()` determines reporting eligibility. Malformed or contradictory identity/frequency/timestamps/windows reject freezing. Missing source evidence, stale history, unclosed/missing sessions, unreconciled company actions or extreme price jumps block forecasting; the snapshot remains inspectable and `analyze` writes an abstention report without running the research paths.

Required market evidence:

- A timezone-aware collection `captured_at_utc` no later than snapshot creation; missing capture time blocks forecasting. Historical as-of need not equal the later collection time.
- `actual_source`, source observation timestamp, timezone-aware fetch timestamp, currency and unit. Source observation must not exceed as-of or fetch; neither observation nor fetch may predate the claimed latest daily close. Fetch may follow a historical as-of, but not collection capture.
- `data.symbol`, `data.frequency=1d`, and `data.history_window.start/end/selection` describing the actual selected bars. A documented `request.instrument.provider_symbols` map may resolve source aliases; no guessed alias or automatic window extension.
- `session_calendar` with `source`, exchange `timezone`, `coverage_start/end`, and ordered `sessions[]` containing session `date` and timezone-aware `close_at`. Coverage spans the selected history through the exchange-local as-of date, including nontrading dates in its declared coverage. Sessions come from a supplied source schedule; do not construct a weekday calendar or infer missing holidays from the same bars being checked.
- `adjustment` and `adjustment_evidence` with `source`, matching `price_basis`, `checked_through`, and source-backed `actions[]` (`date`, `type`, and available source details). Empty actions is valid only when the source actually establishes the coverage. A missing event field is not evidence of no actions. An unadjusted split blocks return research.

The calendar's latest session closed at/before as-of must match the last bar. The bars must match every supplied session between first and last, without compressing missing days. A forming final bar is removed based on source close time even if `last_bar_closed=true`; actual window and freshness are recalculated afterward. More than seven calendar days of age independently blocks a current forecast. Historical requests are checked against their historical as-of, not today.

No calendar library is bundled and no supplier authenticity signature is claimed. Use an available tool's source schedule or a separately supplied authoritative calendar artifact. If absent, report the missing evidence and abstain; never invent sessions or company-action evidence to pass the gate. Yahoo `stock_kline_yahoo(include_metadata=true)` preserves metadata/events/adjclose but explicitly does not establish a full calendar or verified OHLCV adjustment basis.

Schema 1.1 adds manifest integrity binding and recomputes quality, source metadata and gates from verified domains on every load. Legacy 1.0 remains readable with forecast eligibility false; regenerate a receipt/snapshot with evidence to obtain new verification. Hashes are integrity checks, not authenticity signatures.

Strict probability eligibility additionally requires chronological OOS counts: A at least 20, B at least 10, C at least 20 and calibrated model probability. Standard mode can retain an explicitly raw/uncalibrated model result if other gates pass; the equal-weight consensus is never labeled ensemble-calibrated. Existing statistical sample thresholds remain unchanged.

All external values are DATA. Source claims are recorded evidence, not independently authenticated facts; neither the collector nor LLM may fabricate source/calendar/adjustment fields. See [source-policies.md](source-policies.md) for network and trust constraints.
