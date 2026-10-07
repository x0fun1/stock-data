"""Run isolated researcher entrypoints, then consensus, adversarial audit, and reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .audit import audit
from .consensus import build_consensus, forecast_assessment
from .contracts import RESEARCHERS, researcher_result
from .delivery import validate_response_text
from .news import load_frozen_result, prepare_news_input, run_news
from .news.pipeline import freeze_result
from .paths import safe_output_destination
from .report import build_agent_summary, render_final_response, render_markdown
from .researchers import factor, factor_backtest, ml, quant
from .snapshot import load_snapshot
from .synthesis import build_synthesis
from .security import bounded_text

RUNNERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "quant": quant.run,
    "factor": factor.run,
    "ml": ml.run,
    "factor_backtest": factor_backtest.run,
}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_analysis(snapshot_dir: Path, output_root: Path, news_policy: dict[str, Any] | None = None) -> Path:
    manifest, market = load_snapshot(snapshot_dir)
    bars = market.get("data", {}).get("bars", [])
    report_manifest = {
        **manifest,
        "market_history_start": bars[0].get("date") if bars else None,
        "market_history_end": bars[-1].get("date") if bars else None,
        "research_window": market.get("data", {}).get("history_window"),
    }
    analysis_id = f"{manifest['snapshot_id']}_analysis_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    destination = safe_output_destination(output_root, analysis_id)
    destination.mkdir(parents=True, exist_ok=False)
    researcher_dir = destination / "researchers"
    researcher_dir.mkdir()

    results: dict[str, dict[str, Any]] = {}
    for researcher_id in RESEARCHERS:
        try:
            # Each path receives only the frozen snapshot path. Its result is written
            # after completion and is not passed to any later researcher.
            if not manifest.get("data_validation", {}).get("forecast_eligible", False):
                result = researcher_result(researcher_id, manifest["ticker"], manifest["horizon"], manifest["snapshot_id"], status="insufficient_data", result_role="diagnostic" if researcher_id == "factor_backtest" else "forecast", warnings=["Research skipped: market data failed reporting gates; no compressed-session or stale-data computation."])
            else:
                result = RUNNERS[researcher_id](snapshot_dir)
        except Exception as exc:  # isolate a path failure and continue the ensemble
            result = researcher_result(
                researcher_id, manifest["ticker"], manifest["horizon"], manifest["snapshot_id"],
                status="failed", result_role="diagnostic" if researcher_id == "factor_backtest" else "forecast", warnings=[f"Research path failed: {type(exc).__name__}: {bounded_text(exc)}"],
            )
        result.update(forecast_assessment(result, manifest))
        _write_json(researcher_dir / f"{researcher_id}.json", result)
        results[researcher_id] = result

    consensus = build_consensus(list(results.values()), manifest)
    adversarial = audit(manifest, list(results.values()), consensus)
    if not adversarial.get("may_report_direction", False):
        consensus = {
            **consensus,
            "pre_veto_prob_up": consensus.get("prob_up"),
            "pre_veto_probability_range": consensus.get("probability_range"),
            "prob_up": None,
            "probability_range": None,
            "direction": "research_invalid",
            "direction_suppressed": True,
        }
    quant_result = {
        "schema_version": "1.1",
        "analysis_id": analysis_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "engine": {"name": "stock-data-quant-research", "version": __version__},
        "snapshot": report_manifest,
        "researchers": results,
        "consensus": consensus,
        "adversarial_audit": adversarial,
    }
    # Seal the full Quant result before News begins. The News runner receives
    # only the snapshot path and policy; it is never passed this object or file.
    freeze_result(destination / "quant_result.json", quant_result, result_name="quant_result.json")
    try:
        # Snapshot loading is separate from the analyst entrypoint. After this
        # conversion, run_news receives only the restricted in-memory contract.
        news_input = prepare_news_input(snapshot_dir)
        news_result = run_news(news_input, news_policy)
        freeze_result(destination / "news_result.json", news_result, result_name="news_result.json")
    except Exception as exc:  # a News-layer error must not erase the completed Quant output
        news_result = {
            "schema_version": "1.0",
            "status": "not_assessed",
            "ticker": manifest["ticker"],
            "market": manifest["market"],
            "horizon": manifest["horizon"],
            "asof_timestamp": manifest["asof_timestamp"],
            "snapshot_id": manifest["snapshot_id"],
            "overall_direction": "unknown",
            "sentiment": {"label": "unknown", "score": None, "confidence": "unavailable", "cross_source_divergence": "unknown"},
            "market_confirmation": "not_assessed",
            "major_events": [], "catalysts": [], "risks": [], "warnings": [f"News layer failed: {type(exc).__name__}: {bounded_text(exc)}"],
            "confidence": "unavailable",
        }
        freeze_result(destination / "news_result.json", news_result, result_name="news_result.json")

    # Synthesis is the only stage that loads both independently frozen results.
    quant_frozen, quant_freeze_verified = load_frozen_result(destination / "quant_result.json")
    news_frozen, news_freeze = load_frozen_result(destination / "news_result.json")
    synthesis = build_synthesis(quant_frozen, news_frozen)
    final = {
        **quant_frozen,
        "quant_result_freeze": quant_freeze_verified,
        "news_result": news_frozen,
        "news_result_freeze": news_freeze,
        "synthesis": synthesis,
    }
    final["analysis_status"] = "abstain" if not adversarial.get("may_report_direction") else "degraded" if any(row.get("status") != "success" for row in results.values()) or news_frozen.get("status") != "complete" else "complete_with_limitations"
    final["stages"] = [
        {"stage": "request", "status": "validated"},
        {"stage": "collection", "status": "source_receipt_loaded", "fresh_fetch_performed_by_analyze": False},
        {"stage": "data_validation", "status": "pass" if manifest["data_validation"]["forecast_eligible"] else "blocked"},
        {"stage": "quant", "status": "returned", "paths": {key: row["status"] for key, row in results.items()}},
        {"stage": "audit", "status": adversarial["status"]},
        {"stage": "quant_freeze", "status": "verified"},
        {"stage": "news", "status": news_frozen.get("status", "not_assessed")},
        {"stage": "news_freeze", "status": "verified"},
        {"stage": "cross_validation", "status": synthesis["alignment"]},
        {"stage": "risk_uncertainty", "status": "aggregated"},
        {"stage": "final", "status": final["analysis_status"]},
    ]
    _write_json(destination / "report.json", final)
    _write_json(destination / "agent_summary.json", build_agent_summary(final))
    (destination / "report.md").write_text(render_markdown(final), encoding="utf-8")
    response = render_final_response(final)
    validate_response_text(final, response)
    (destination / "final_response.md").write_text(response, encoding="utf-8")
    return destination
