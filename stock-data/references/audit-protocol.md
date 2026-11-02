# Audit Protocol

Procedure version: `news-1`. Audit the news evidence, coverage, host assessment, report and operational safety separately. Reproducible files do not establish source truth, account entitlement or complete historical coverage.

## Mandatory shape of an audit report

1. State exactly what was executed, failed or not executed. Separate offline/synthetic checks from real US/HK source tests.
2. Anchor the code revision, schema, policy, prompt, model, request cutoff and frozen input digest.
3. Verify source identity, content permission, publication/update precision and query receipts; do not use article span as coverage proof.
4. Separate collection, window coverage and analysis status; report requested versus covered intervals and candidate/analyzed/omitted counts.
5. Check that every major driver and selected event is from the same admitted evidence set; missing text, retractions and post-cutoff updates cannot be overridden by host claims.
6. Verify target exposure for macro/industry and ETF conclusions, comparable financial periods/currencies and properly attributed external opinions.
7. Check the unique 7-day/30-day major selection or null reason, overall judgment, contrary evidence and change conditions.
8. Identify each defect, affected artifact, reproduction and remedy; do not hide a failed check behind a general disclaimer.

## Independent verification

A hash proves content consistency, not the truth of a source claim. Review the original evidence and query receipts, not only generated prose. Test invalid citations, changed digests, fabricated dates/numbers, excluded events relabeled as major, incomplete pagination, failed APIs disguised as empty results and altered delivery text. External text that asks for tools, secrets or path changes must remain data.

A mutation that is not detected is a defect in the audit, not a curiosity.

## Re-audit checklist

- [ ] US/HK stocks and ETFs can use sufficient news without OHLCV.
- [ ] Default 60 days, allowed 30–60, timezone-aware cutoff and date precision are enforced.
- [ ] Seven-day-only feeds and failed/truncated pages cannot claim complete 60-day coverage.
- [ ] Sparse news with successful query completion is not mislabeled a collection failure.
- [ ] Syndication/bilingual duplicates and substantive updates retain lineage and correct recency.
- [ ] Post-cutoff, retracted, headline-only and unsupported exposure records cannot become major drivers.
- [ ] Missing/invalid host output remains not assessed or insufficient, never automatically neutral.
- [ ] Unique major selection, contrary evidence and overall judgment use admitted evidence only.
- [ ] Model-authored source metadata and unsupported dates/numbers are rejected.
- [ ] Changed evidence/context/policy/prompt/model invalidates assessment reuse.
- [ ] Actual outgoing Markdown passes `validate-report`, not merely the stored report.
- [ ] Repeat/incremental collection, locks, interrupted runs and dry-run retention are checked without removing shared evidence.
- [ ] CLI, schema, imports, applicable Ruff checks and local documentation links are verified.
- [ ] Source/account tests not actually executed are listed as untested; synthetic checks are not real-world acceptance.

The full operation is described in [news-first-workflow.md](news-first-workflow.md). Do not claim all requirements passed from a keyword scan or lint result alone.
