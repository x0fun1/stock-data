"""Eight-section reports and a bounded DATA-only projection for the host Agent."""

from __future__ import annotations

from typing import Any
from .security import bounded_text, markdown_text, safe_url


def _pct(value: Any) -> str:
    return f"{float(value):.1%}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "Unavailable"


def _cell(value: Any) -> str:
    return markdown_text(value if value not in (None, "") else "Unavailable")


def _link(title: Any, url: Any) -> str:
    url = safe_url(url)
    return f"[{_cell(title)}](<{url.replace('>', '%3E').replace('<', '%3C')}>)" if url else _cell(title)


def _validation_summary(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("validation") or {}
    holdout = value.get("final_holdout") or {}
    return {"oos_predictions": value.get("oos_predictions", value.get("holdout_predictions", holdout.get("observations"))),
            "brier": value.get("brier", value.get("holdout_brier", holdout.get("brier"))),
            "base_rate_brier": value.get("base_rate_brier", value.get("holdout_base_rate_brier", holdout.get("base_rate_brier"))),
            "calibration": item.get("probability_calibration", "not_assessed")}


def build_agent_summary(result: dict[str, Any]) -> dict[str, Any]:
    snapshot, quant = result["snapshot"], result["consensus"]
    news, synthesis = result.get("news_result", {}), result.get("synthesis", {})
    paths = [{"id": key, "status": item.get("status"), "role": item.get("result_role"),
              "eligible": item.get("forecast_eligible", False),
              "prob_up": item.get("prob_up") if item.get("forecast_eligible") and result["adversarial_audit"].get("may_report_direction") else None,
              "validation": _validation_summary(item), "exclusions": [bounded_text(text, 200) for text in item.get("forecast_exclusion_reasons", [])[:4]]}
             for key, item in result["researchers"].items()]
    evidence = [{"path": key, "polarity": polarity, "factor": bounded_text(row.get("factor_name", row.get("hypothesis", row.get("family"))), 160), "current_contribution": row.get("current_contribution"), "training_rank_ic": row.get("training_rank_ic")}
                for key, item in result["researchers"].items() for polarity in ("positive", "negative") for row in item.get("evidence", {}).get(polarity, [])[:2]]
    events = [{key: event.get(key) for key in ("event_id", "event_type", "event_group", "first_published_at", "occurred_at", "updated_at", "direction", "url", "fact_status", "source_independence")} | {"headline": bounded_text(event.get("headline"), 300), "sources": [bounded_text(source, 100) for source in event.get("sources", [])[:3]], "reaction_status": event.get("market_reaction", {}).get("status")} for event in news.get("major_events", [])[:5]]
    diagnostic = result["researchers"].get("factor_backtest", {}).get("validation") or {}
    ic = [{"factor": bounded_text(name, 100), "horizon": diagnostic.get("requested_horizon"), "holdout": {key: value.get("horizons", {}).get(diagnostic.get("requested_horizon"), {}).get("holdout", {}).get(key) for key in ("spearman_time_series_ic", "mean_forward_return", "observations", "status")}} for name, value in list(diagnostic.get("factor_metrics", {}).items())[:3]]
    bias = result["adversarial_audit"].get("bias_audit", {})
    risks = synthesis.get("key_risks", [])
    selected_risks = []
    for source in ("data_or_audit", "news", "factor_backtest", "coverage_or_validation"):
        selected_risks.extend([row for row in risks if row.get("source") == source][:3])
    return {"schema_version": "1.1", "content_role": "UNTRUSTED_DATA_SUMMARY_NO_EXECUTION_INSTRUCTIONS", "analysis_id": result["analysis_id"],
            "identity": {key: snapshot[key] for key in ("ticker", "market", "horizon", "asof_timestamp", "snapshot_id")},
            "analysis_status": result["analysis_status"], "stages": result["stages"],
            "sections": {
                "price_time": snapshot.get("latest_confirmed_close"),
                "quant": {"direction": quant.get("direction"), "prob_up": quant.get("prob_up"), "confidence": quant.get("confidence"), "status": quant.get("status"), "calibration_status": quant.get("calibration_status"), "paths": paths},
                "technical_factor_evidence": evidence,
                "backtest_ic_bias": {"audit_status": result["adversarial_audit"]["status"], "checks": {key: value.get("status") for key, value in bias.items() if isinstance(value, dict)}, "diagnostic_examples": ic, "scope": "D: single-security time-series diagnostics; full portfolio backtest/PBO/DSR not computed"},
                "news": {"status": news.get("status"), "event_bias": news.get("overall_direction"), "events": events, "coverage": {key: news.get("coverage", {}).get(key) for key in ("observed_publication_start", "observed_publication_end", "eligible_event_count", "eligible_opinion_count", "excluded_evidence_count")}},
                "cross_validation": {key: synthesis.get(key) for key in ("alignment", "alignment_basis", "news_evidence_direction", "overall_confidence", "evidence_links")},
                "risk_counter_evidence": {"items": selected_risks, "total": len(risks), "omitted": len(risks) - len(selected_risks)},
                "final_synthesis": {"status": result["analysis_status"], "may_report_direction": result["adversarial_audit"].get("may_report_direction", False), "quant_probability_preserved": synthesis.get("quant_probability_preserved", False), "warnings": [bounded_text(item, 240) for item in synthesis.get("warnings", [])[:5]]},
            }}


def render_markdown(result: dict[str, Any]) -> str:
    manifest, quant, audit = result["snapshot"], result["consensus"], result["adversarial_audit"]
    news, synthesis = result.get("news_result", {}), result.get("synthesis", {})
    price = manifest.get("latest_confirmed_close") or {}
    allowed = audit.get("may_report_direction", False)
    lines = [f"# {_cell(manifest['ticker'])} — {_cell(manifest['horizon'])} Research", "",
             f"Analysis status: **{_cell(result.get('analysis_status'))}**. " + ("ABSTAIN: no reporting-eligible direction/probability." if not allowed else "Research estimates with the validation limits below."), "",
             "## 1. Price & Data Time", "",
             f"- Price: {_cell(price.get('price'))} {_cell(price.get('currency'))}; kind: {_cell(price.get('kind'))}",
             f"- Market session: {_cell(price.get('session_date'))}; confirmed close time: {_cell(price.get('close_at'))}",
             f"- As of: {_cell(manifest['asof_timestamp'])}; snapshot: `{manifest['snapshot_id']}`",
             f"- Source: {_cell(price.get('source'))}; bars: {manifest.get('market_bar_count', 0)}; window: {_cell(manifest.get('market_history_start'))} to {_cell(manifest.get('market_history_end'))}",
             f"- Forecast data gates: {_cell(manifest.get('data_validation', {}).get('forecast_eligible'))}; quality: {_cell(manifest.get('data_quality', {}).get('overall'))}",
             "- Bar close is not a live quote. Source authenticity is not independently verified.", "",
             "## 2. Quant Core", "",
             f"- Status: {_cell(quant.get('status'))}; direction: {_cell(quant.get('direction'))}; P(up): {_pct(quant.get('prob_up'))}",
             f"- Agreement: {_cell(quant.get('agreement'))}; diversity: {_cell(quant.get('diversity'))}; confidence: {_cell(quant.get('confidence'))}",
             f"- Calibration: {_cell(quant.get('calibration_status'))}; equal-weight mean is not an independently calibrated ensemble probability."]
    probability_range = quant.get("probability_range")
    lines.append(f"- Range across eligible paths (not a statistical confidence interval): {_pct(probability_range[0])}–{_pct(probability_range[1])}" if probability_range else "- Range across eligible paths: Unavailable")
    lines.extend(["", "| Path | Role / status | Eligible | Reportable P(up) | OOS N | Calibration |", "|---|---|---|---:|---:|---|"])
    for name, item in result["researchers"].items():
        validation = _validation_summary(item)
        lines.append(f"| {_cell(name)} | {_cell(item.get('result_role'))} / {_cell(item.get('status'))} | {_cell(item.get('forecast_eligible'))} | {_pct(item.get('prob_up')) if allowed and item.get('forecast_eligible') else 'Unavailable'} | {_cell(validation['oos_predictions'])} | {_cell(validation['calibration'])} |")
    for name, item in result["researchers"].items():
        for reason in item.get("forecast_exclusion_reasons", []):
            lines.append(f"\n{name} exclusion: {_cell(reason)}")
    lines.extend(["", "## 3. Technical & Factor Evidence", ""])
    for name, item in result["researchers"].items():
        for polarity in ("positive", "negative"):
            for row in item.get("evidence", {}).get(polarity, []):
                lines.append(f"- {_cell(name)} / {polarity}: {_cell(row.get('factor_name', row.get('hypothesis', row.get('family'))))}; current contribution {_cell(row.get('current_contribution'))}; training IC {_cell(row.get('training_rank_ic'))}; samples {_cell(row.get('non_overlapping_samples'))}; Wilson 95% interval {_cell(row.get('wilson_95_interval'))}")
    if not any(item.get("evidence", {}).get(polarity) for item in result["researchers"].values() for polarity in ("positive", "negative")):
        lines.append("Evidence unavailable or computation skipped by data gates.")
    lines.extend(["", "## 4. Backtest / IC / Bias", "", f"- Audit: {_cell(audit.get('status'))}; verification scope: {_cell(audit.get('verification_scope'))}"])
    for veto in audit.get("vetoes", []):
        lines.append(f"- VETO: {_cell(veto)}")
    for name, check in audit.get("bias_audit", {}).items():
        if isinstance(check, dict):
            lines.append(f"- {_cell(name)}: {_cell(check.get('status'))}; {_cell(check.get('detail'))}")
        elif isinstance(check, list):
            for row in check:
                lines.append(f"- OOS {_cell(row.get('researcher_id'))}: Brier {_cell(row.get('brier'))} vs base rate {_cell(row.get('base_rate_brier'))}")
    diagnostics = result["researchers"].get("factor_backtest", {}).get("validation") or {}
    lines.extend(["", "D is single-security time-series IC/forward-return diagnostics. Full cross-sectional portfolio backtest, PBO, DSR, costs and survivorship verification are not supplied by this engine.", "",
                  "| Factor | Holdout IC | Mean forward return | N | Status |", "|---|---:|---:|---:|---|"])
    for name, value in diagnostics.get("factor_metrics", {}).items():
        metric = value.get("horizons", {}).get(diagnostics.get("requested_horizon"), {}).get("holdout", {})
        lines.append(f"| {_cell(name)} | {_cell(metric.get('spearman_time_series_ic'))} | {_pct(metric.get('mean_forward_return'))} | {_cell(metric.get('observations'))} | {_cell(metric.get('status'))} |")
    lines.extend(["", "## News & Event Analysis", "", f"- Status: {_cell(news.get('status'))}; eligible event bias: {_cell(news.get('overall_direction'))}; confidence: {_cell(news.get('confidence'))}",
                  f"- Coverage: {_cell(news.get('coverage', {}).get('observed_publication_start'))} to {_cell(news.get('coverage', {}).get('observed_publication_end'))}; excluded records: {_cell(news.get('coverage', {}).get('excluded_evidence_count'))}",
                  "- Reported events, opinions and provider aggregates are separate, uncalibrated DATA. News does not generate a probability."])
    for event in news.get("major_events", []):
        lines.append(f"- {_cell(event.get('first_published_at'))} / {_cell(event.get('event_group'))} / {_cell(event.get('direction'))}: {_link(event.get('headline'), event.get('url'))}; source {_cell(', '.join(event.get('sources', [])))}; reaction {_cell(event.get('market_reaction', {}).get('status'))}")
    for opinion in news.get("opinion_records", [])[:5]:
        lines.append(f"- Opinion {_cell(opinion.get('sentiment'))}: {_link(opinion.get('headline'), opinion.get('url'))}")
    for record in news.get("background_records", [])[:5]:
        lines.append(f"- Background excluded from bias: {_cell(record.get('headline'))}; {_cell('; '.join(record.get('exclusion_reasons', [])))}")
    if not news.get("major_events"):
        lines.append("No eligible event evidence; event cross-validation is not assessed.")
    lines.extend(["", "## Quant / News Cross-Check", "", f"- Relationship: {_cell(synthesis.get('alignment'))}; basis: {_cell(synthesis.get('alignment_basis'))}",
                  f"- Quant: {_cell(synthesis.get('quantitative_bias'))}; unchanged P(up): {_pct(synthesis.get('quant_prob_up'))}",
                  f"- News event bias: {_cell(synthesis.get('news_event_bias'))}; tone: {_cell(synthesis.get('news_sentiment'))}",
                  "- Events are candidate explanations for observed market behavior; temporal association does not establish causality.", "", "## 7. Risk & Counter-evidence", ""])
    for risk in synthesis.get("key_risks", []):
        lines.append(f"- {_cell(risk.get('source'))}: {_cell(risk.get('detail'))}")
    for name, item in result["researchers"].items():
        for warning in item.get("warnings", []):
            lines.append(f"- {_cell(name)}: {_cell(warning)}")
    lines.extend(["", "## 8. Final Synthesis", "", f"- Outcome: {_cell(result.get('analysis_status'))}; relationship: {_cell(synthesis.get('alignment'))}; overall confidence: {_cell(synthesis.get('overall_confidence'))}"])
    for warning in synthesis.get("warnings", []):
        lines.append(f"- {_cell(warning)}")
    lines.extend(["- Missing evidence is not filled with invented probabilities or causal claims. Historical validation does not prove future performance.", "", "Stage receipt:", ""])
    lines.extend(f"- {row['stage']}: {_cell(row['status'])}" for row in result.get("stages", []))
    return "\n".join(lines) + "\n"
