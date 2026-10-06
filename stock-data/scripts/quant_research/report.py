"""Human-readable research report rendering."""

from __future__ import annotations

from typing import Any


def _pct(value: Any) -> str:
    return f"{float(value):.1%}" if isinstance(value, (int, float)) else "Unavailable"


def _cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def render_markdown(result: dict[str, Any]) -> str:
    manifest = result["snapshot"]
    consensus = result["consensus"]
    audit = result["adversarial_audit"]
    lines = [
        f"# {manifest['ticker']} — {manifest['horizon']} Quant Research Ensemble",
        "",
        "## Snapshot",
        "",
        f"- Snapshot ID: `{manifest['snapshot_id']}`",
        f"- As of: {manifest['asof_timestamp']}",
        f"- Data quality: {manifest.get('data_quality', {}).get('overall', 'unknown')} ({manifest.get('data_quality', {}).get('score', 'n/a')})",
        f"- OHLCV sessions: {manifest.get('market_bar_count', 'n/a')}",
        f"- Research window: {manifest.get('market_history_start') or 'unknown'} to {manifest.get('market_history_end') or 'unknown'}",
        "- Sources: " + (", ".join(sorted({str(item.get("actual_source")) for item in manifest.get("sources", {}).values() if item.get("actual_source")})) or "not reported"),
        "",
    ]
    window = manifest.get("research_window")
    if isinstance(window, dict) and window.get("selection"):
        lines.append(f"- Window selection: {window['selection']}")
    lines.extend([
        "### Collected domains",
        "",
        "| Domain | Status | Source | Source time | Fallback |",
        "|---|---|---|---|---|",
    ])
    for domain, status in manifest.get("data_domains", {}).items():
        source = manifest.get("sources", {}).get(domain, {})
        lines.append(f"| {domain} | {status} | {source.get('actual_source') or '—'} | {source.get('source_timestamp') or '—'} | {'yes' if source.get('fallback_used') else 'no'} |")
    lines.extend([
        "",
        "## Independent research paths",
        "",
    ])
    for researcher_id in ("quant", "factor", "ml", "factor_backtest"):
        item = result["researchers"].get(researcher_id, {})
        validation = item.get("validation", {})
        if item.get("result_role") == "diagnostic":
            lines.extend([
                f"### {researcher_id.replace('_', ' ').title()} Diagnostics",
                "",
                f"- Status: `{item.get('status', 'failed')}`",
                "- Role: diagnostic only; it does not contribute a probability to consensus.",
                f"- Method: `{validation.get('method', 'not available')}`",
                f"- Scope: `{validation.get('universe_type', 'not reported')}`; full cross-sectional portfolio backtest: `{validation.get('full_cross_sectional_backtest', {}).get('status', 'not reported')}`",
                "",
                f"Holdout results for the requested {validation.get('requested_horizon', item.get('horizon', 'unknown'))} horizon:",
                "",
                "| Factor | Time-series IC (Spearman) | Mean forward return | Top-minus-bottom | N | Status |",
                "|---|---:|---:|---:|---:|---|",
            ])
            requested_horizon = validation.get("requested_horizon")
            for factor_name, factor_result in validation.get("factor_metrics", {}).items():
                metric = factor_result.get("horizons", {}).get(requested_horizon, {}).get("holdout", {})
                lines.append(
                    f"| {factor_name} | {_pct(metric.get('spearman_time_series_ic'))} | "
                    f"{_pct(metric.get('mean_forward_return'))} | "
                    f"{_pct(metric.get('top_minus_bottom_training_quantile_return'))} | "
                    f"{metric.get('observations', 0)} | `{metric.get('status', 'not available')}` |"
                )
            if item.get("warnings"):
                lines.append("- Warnings: " + "; ".join(item["warnings"]))
            lines.extend([
                "",
                "These single-security statistics are time-series diagnostics; they are not cross-sectional Rank IC, executable portfolio returns, or a costed backtest.",
                "",
            ])
            continue
        lines.extend([
            f"### {researcher_id.title()} Research",
            "",
            f"- Status: `{item.get('status', 'failed')}`",
            f"- P(up): {_pct(item.get('prob_up'))}",
            f"- Expected return: {_pct(item.get('expected_return'))}",
            f"- Probability source: `{item.get('probability_source') or 'none'}`",
            f"- Validation: `{validation.get('method', validation.get('model', 'not available'))}`",
        ])
        if item.get("warnings"):
            lines.append("- Warnings: " + "; ".join(item["warnings"]))
        evidence = item.get("evidence", {})
        for polarity in ("positive", "negative"):
            for entry in evidence.get(polarity, []):
                detail = entry.get("hypothesis") or entry.get("factor_name") or entry.get("model_probability")
                lines.append(f"- {polarity.title()} evidence: {entry.get('family', 'other')} — {detail}")
        lines.append("")
    lines.extend([
        "## Consensus",
        "",
        f"- Status: `{consensus.get('status')}`",
        f"- P(up): {_pct(consensus.get('prob_up'))}",
        f"- Direction: `{consensus.get('direction')}`",
        f"- Agreement: `{consensus.get('agreement')}`",
        f"- Probability range: {_pct(consensus.get('probability_range', [None, None])[0])} – {_pct(consensus.get('probability_range', [None, None])[1])}" if consensus.get("probability_range") and not consensus.get("direction_suppressed") else "- Probability range: Unavailable",
        f"- Diversity: `{consensus.get('diversity')}`",
        f"- Confidence: `{consensus.get('confidence')}`",
        "",
        "## Adversarial audit",
        "",
        f"- Status: `{audit.get('status')}`",
    ])
    for veto in audit.get("vetoes", []):
        lines.append(f"- VETO: {veto}")
    for warning in audit.get("warnings", []):
        lines.append(f"- Warning: {warning}")
    bias_audit = audit.get("bias_audit", {})
    if bias_audit:
        lines.extend([
            "",
            "### Bias checks",
            "",
            "| Check | Status | Detail |",
            "|---|---|---|",
        ])
        labels = {
            "look_ahead_and_label_timing": "Look-ahead and label timing",
            "chronological_out_of_sample": "Chronological out-of-sample",
            "multiple_testing_and_overfitting": "Multiple testing / overfitting",
            "pbo": "Probability of Backtest Overfitting (PBO)",
            "deflated_sharpe_ratio": "Deflated Sharpe Ratio (DSR)",
            "transaction_costs_and_market_impact": "Costs and market impact",
            "survivorship_bias": "Survivorship bias",
        }
        for key, label in labels.items():
            check = bias_audit.get(key, {})
            if check:
                detail = str(check.get("detail", "")).replace("|", "\\|")
                lines.append(f"| {label} | `{check.get('status', 'unknown')}` | {detail} |")
        brier_checks = bias_audit.get("oos_brier_vs_base_rate", [])
        if brier_checks:
            summary = "; ".join(
                f"{check['researcher_id']} ({check['validation_path']}): {check['brier']:.4f} vs {check['base_rate_brier']:.4f} "
                f"({'beats' if check['beats_base_rate'] else 'does not beat'} baseline)"
                for check in brier_checks
            )
            lines.extend(["", f"OOS Brier vs base-rate checks: {summary}"])
    lines.extend([
        "",
        "## Assessment",
        "",
        ("Research invalid: the audit vetoed a directional conclusion." if not audit.get("may_report_direction") else "This is a quantitative research summary, not a deterministic outcome or a trade instruction."),
        "",
        "Probability values come from the listed numeric research path and must be read with its sample size, validation and warnings. Historical validation is not proof of future performance.",
        "",
    ])
    news = result.get("news_result", {})
    sentiment = news.get("sentiment", {})
    lines.extend([
        "## News & Event Analysis",
        "",
        f"- Status: `{news.get('status', 'not_assessed')}`; collected news domain: `{news.get('coverage', {}).get('news_domain_status', manifest.get('data_domains', {}).get('news', 'not reported'))}`",
        f"- Event bias: `{news.get('overall_direction', 'unknown')}`; sentiment: `{sentiment.get('label', 'unknown')}` (score {_cell(sentiment.get('score'))}, {_cell(sentiment.get('score_scale'))})",
        f"- Provider aggregate sentiment: `{sentiment.get('provider_direction', 'unknown')}`; source `{sentiment.get('provider_aggregate', {}).get('source', '—') if isinstance(sentiment.get('provider_aggregate'), dict) else '—'}`; raw metrics {_cell(sentiment.get('provider_aggregate'))}",
        f"- Source agreement: `{news.get('source_agreement', 'unknown')}`; market confirmation: `{news.get('market_confirmation', 'not_assessed')}`; News confidence: `{news.get('confidence', 'unavailable')}`",
        f"- Records: {news.get('coverage', {}).get('asof_article_count', 0)} PIT records; {news.get('coverage', {}).get('canonical_event_count', 0)} canonical events; {news.get('coverage', {}).get('canonical_opinion_count', 0)} opinion clusters; {news.get('coverage', {}).get('duplicate_records_merged', 0)} duplicate records merged.",
        "",
        "Event facts and opinion/sentiment are classified separately. Event and sentiment scores are heuristic/un-calibrated where shown; News does not emit a probability.",
        "",
        "| Published | Type | Group | Headline | Target match | Direction | Articles / sources | Time decay | Market reaction | Daily return |",
        "|---|---|---|---|---|---|---|---:|---|---:|",
    ])
    for event in news.get("major_events", []):
        reaction = event.get("market_reaction", {})
        lines.append(
            f"| {_cell(event.get('first_published_at'))} | {_cell(event.get('event_type'))} | {_cell(event.get('event_group'))} | "
            f"{_cell(event.get('headline'))} | {_cell(event.get('ticker_relevance'))} | {_cell(event.get('direction'))} | {event.get('article_count', 0)} / {_cell(', '.join(event.get('sources', [])))} | "
            f"{_pct(event.get('time_decay'))} | {_cell(reaction.get('status'))} | {_pct(reaction.get('daily_close_return'))} |"
        )
    if not news.get("major_events"):
        lines.append("| — | — | — | No assessable event records | — | — | — | — | — | — |")
    if news.get("opinion_records"):
        lines.extend(["", "Opinion / sentiment records:"])
        for opinion in news["opinion_records"][:5]:
            lines.append(f"- `{opinion.get('sentiment')}` ({_cell(opinion.get('sources'))}): {_cell(opinion.get('headline'))}")
    if news.get("unclassified_records"):
        lines.extend(["", "Unclassified records (not counted as events or opinions):"])
        for item in news["unclassified_records"][:5]:
            lines.append(f"- {_cell(item.get('published_at'))} ({_cell(item.get('sources'))}): {_cell(item.get('headline'))}")
    if news.get("catalysts"):
        lines.extend(["", "Catalysts:"])
        for item in news["catalysts"][:5]:
            lines.append(f"- {_cell(item.get('event_type'))}: {_cell(item.get('headline'))}")
    if news.get("risks"):
        lines.extend(["", "Risks:"])
        for item in news["risks"][:5]:
            lines.append(f"- {_cell(item.get('event_type'))}: {_cell(item.get('headline'))}")
    for warning in news.get("warnings", []):
        lines.append(f"- News warning: {_cell(warning)}")
    for limitation in news.get("limitations", []):
        lines.append(f"- Limitation: {_cell(limitation)}")
    lines.extend([
        "",
        "## Quant / News Cross-Check",
        "",
    ])
    synthesis = result.get("synthesis", {})
    lines.extend([
        f"- Quantitative bias: `{synthesis.get('quantitative_bias', consensus.get('direction', 'unavailable'))}`",
        f"- Quant P(up): {_pct(synthesis.get('quant_prob_up', consensus.get('prob_up')))}",
        f"- News/event bias: `{synthesis.get('news_event_bias', news.get('overall_direction', 'unknown'))}`; sentiment: `{synthesis.get('news_sentiment', sentiment.get('label', 'unknown'))}`",
        f"- Market confirmation: `{synthesis.get('market_confirmation', news.get('market_confirmation', 'not_assessed'))}`",
        f"- Evidence alignment: `{synthesis.get('alignment', 'NOT_ASSESSED')}`",
        f"- News direction used for alignment: `{synthesis.get('news_evidence_direction', 'unknown')}` (`{synthesis.get('alignment_basis', 'not reported')}`)",
        f"- Overall confidence: `{synthesis.get('overall_confidence', consensus.get('confidence', 'unavailable'))}`",
        "- Quant probability is preserved unchanged; no Quant/News probability blending is performed.",
    ])
    for item in synthesis.get("key_risks", []):
        lines.append(f"- Cross-check risk: {_cell(item.get('event_type'))}: {_cell(item.get('headline'))}")
    for warning in synthesis.get("warnings", []):
        lines.append(f"- Cross-check warning: {_cell(warning)}")
    lines.append("")
    return "\n".join(lines)
