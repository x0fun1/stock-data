"""Run the fixed-policy research pipeline and bind every stage to one snapshot."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .audit import audit
from .audit import attach_news_observation
from .consensus import build_consensus, forecast_assessment
from .contracts import researcher_result
from .delivery import validate_response_text
from .feasibility import build_timing
from .forecast_policy import (
    POLICY_VERSION,
    REQUIRED_STAGES,
    is_sampled_ordinal,
    model_identity,
    session_ordinal,
)
from .jsonio import write_json
from .ledger import prospective_records
from .market_data import calendar_session_list, market_bars
from .news import freeze_result, load_frozen_result, prepare_news_input, run_news
from .news import news_assessed, news_status_label
from .paths import safe_output_destination
from .report import (
    build_agent_summary,
    enforce_agent_summary_budget,
    render_final_response,
    render_markdown,
    reporting_status_for,
)
from .researchers import factor, factor_backtest, ml, quant
from .security import bounded_text
from .snapshot import load_snapshot
from .synthesis import build_synthesis

RUNNERS: dict[str, Callable[[Path, tuple[dict[str, Any], dict[str, Any]] | None], dict[str, Any]]] = {
    "quant": quant.run,
    "factor": factor.run,
    "ml": ml.run,
    "factor_backtest": factor_backtest.run,
}

ROLE_BY_PATH = {"quant": "production", "factor": "shadow", "ml": "shadow", "factor_backtest": "diagnostic"}


def _canonical_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _stage(stages: list[dict[str, Any]], name: str, status: str, *, payload: Any = None, reason: str | None = None,
           run_id: str | None = None, snapshot_id: str | None = None) -> None:
    if name not in REQUIRED_STAGES:
        raise ValueError(f"unknown stage: {name}")
    if any(item["stage"] == name for item in stages):
        raise ValueError(f"duplicate stage: {name}")
    stages.append({
        "stage": name,
        "status": status,
        "reason": reason,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "policy_version": POLICY_VERSION,
        "payload_sha256": _canonical_digest(payload) if payload is not None else None,
    })


def _validate_stages(stages: list[dict[str, Any]]) -> None:
    names = [item["stage"] for item in stages]
    if names != list(REQUIRED_STAGES):
        raise ValueError(f"stage sequence must be exactly {list(REQUIRED_STAGES)}; got {names}")
    for item in stages:
        if item["status"] not in {"complete", "partial", "skipped", "not_applicable", "not_assessed", "blocked", "insufficient_data"}:
            raise ValueError(f"invalid stage status: {item['status']}")
        if item["status"] != "complete" and not item.get("reason"):
            raise ValueError(f"stage {item['stage']} must explain a non-complete status")


def _decision_sampled(decision_session: str | None, sessions: list[date] | None) -> bool | None:
    if not decision_session or not sessions:
        return None
    try:
        ordinal = session_ordinal(sessions, date.fromisoformat(decision_session))
    except ValueError:
        return None
    return is_sampled_ordinal(ordinal)


def run_analysis(
    snapshot_dir: Path, output_root: Path, news_policy: dict[str, Any] | None = None,
    *, on_analysis_created: Callable[[Path], None] | None = None,
    news_ai_assessment: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> Path:
    started = time.perf_counter()
    manifest, market = load_snapshot(snapshot_dir)
    snapshot_id = manifest["snapshot_id"]
    run_id = manifest.get("run_id") or snapshot_id
    report_manifest = {
        "ticker": manifest["ticker"],
        "market": manifest["market"],
        "horizon": manifest["horizon"],
        "asof_timestamp": manifest.get("asof_timestamp"),
        "snapshot_id": snapshot_id,
        "policy": manifest.get("policy") or {},
        "target_session": (manifest.get("policy") or {}).get("target_session"),
        "data_validation": manifest.get("data_validation") or {},
        "data_quality": manifest.get("data_quality") or {},
        "warnings": (manifest.get("data_quality") or {}).get("issues") or [],
    }
    ticker, market_name = manifest["ticker"], manifest["market"]
    policy = manifest.get("policy") or {}
    run = {
        "schema_version": "analysis-3.0",
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "policy_version": POLICY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "package_version": __version__,
    }
    destination = safe_output_destination(output_root, run_id)
    snapshot_binding = {"snapshot_id": snapshot_id, "content_sha256": manifest.get("snapshot_content_sha256")}
    destination.mkdir(parents=True, exist_ok=True)
    stages: list[dict[str, Any]] = []
    _stage(stages, "request", "complete", payload=manifest.get("request"), run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "request.json", manifest.get("request") or {})
    _stage(stages, "collection", "complete", payload=manifest.get("source_payload_sha256"), run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "snapshot.json", {
        "schema_version": "analysis-snapshot-binding-1.0",
        "snapshot_id": snapshot_id,
        "source_payload_sha256": manifest.get("source_payload_sha256"),
        "policy": policy,
        "binding": snapshot_binding,
    })

    data_status = "complete" if (manifest.get("data_validation") or {}).get("forecast_eligible") else "blocked"
    _stage(stages, "data", data_status, payload=manifest.get("data_validation"),
           reason=None if data_status == "complete" else "snapshot data validation did not pass", run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "data.json", {
        "data_quality": manifest.get("data_quality"),
        "data_validation": manifest.get("data_validation"),
        "policy": policy,
    })

    raw_bars = (market.get("data") or {}).get("bars") or []
    bars = market_bars(market) if data_status == "complete" else [
        {"date": str(row.get("date"))[:10], "close": row.get("close")} for row in raw_bars if isinstance(row, dict)
    ]
    sessions = calendar_session_list(market)
    results: dict[str, dict[str, Any]] = {}
    if data_status == "complete":
        for path in ("quant", "factor", "ml", "factor_backtest"):
            try:
                result = RUNNERS[path](snapshot_dir, (manifest, market))
            except Exception as exc:  # noqa: BLE001 - isolate a candidate failure from the production path
                result = researcher_result(
                    path, ticker, manifest["horizon"], snapshot_id, status="failed",
                    result_role=ROLE_BY_PATH[path],
                    warnings=[f"Research path failed: {type(exc).__name__}: {bounded_text(exc)}"],
                )
            result["result_role"] = ROLE_BY_PATH[path]
            result["model_version"] = model_identity(path)["model_version"]
            result["policy_version"] = POLICY_VERSION
            results[path] = result
    quant_status = (results.get("quant") or {}).get("status", "blocked")
    _stage(stages, "quant", "complete" if quant_status == "success" else ("insufficient_data" if quant_status == "insufficient_data" else "blocked"),
           payload={path: {"status": item.get("status"), "prob_up": item.get("prob_up")} for path, item in results.items()},
           reason=None if quant_status == "success" else "production path A did not produce a probability", run_id=run_id, snapshot_id=snapshot_id)

    prospective: list[dict[str, Any]] = []
    if ledger_path:
        try:
            prospective = prospective_records(ledger_path, ticker=ticker, market=market_name)
        except (OSError, ValueError):
            prospective = []
    assessments: dict[str, dict[str, Any]] = {}
    if "quant" in results:
        assessments["quant"] = forecast_assessment(
            results["quant"], manifest, bars=bars, sessions=sessions, prospective_records=prospective,
        )
    consensus = build_consensus(
        list(results.values()), manifest,
        assessments=assessments, bars=bars, sessions=sessions, prospective_records=prospective,
    )
    decision_session = (market.get("data") or {}).get("policy_window", {}).get("decision_session")
    session_ordinal = _decision_sampled(decision_session, sessions)
    consensus["decision_sampled"] = session_ordinal
    # ``None`` must not be read as "not sampled": it means the phase could not be
    # anchored because the calendar does not contain 2000-01-03.  The sampling
    # phase is a throttle, so an unanchored phase simply disables throttling.
    consensus["sampling"] = {
        "status": "unanchored_calendar" if session_ordinal is None else "anchored",
        "decision_sampled": session_ordinal,
        "role": "throttle_only_not_qualification",
        "note": "qualification reads the full prefix replay; the phase never gates eligibility",
    }
    consensus["timing"] = build_timing(manifest, bars)
    consensus["timing"]["target_session"] = policy.get("target_session")
    # The Quant audit runs before News interpretation so that no News artifact,
    # including a malicious one, can influence a Quant veto.
    adversarial = audit(
        manifest, list(results.values()), consensus,
        bars=bars, sessions=sessions, prospective_records=prospective,
    )
    news_result = None
    frozen_path = snapshot_dir / "news_result.json"
    if frozen_path.is_file():
        try:
            news_result, _ = load_frozen_result(frozen_path)
        except ValueError:
            news_result = None
    if news_result is None and news_policy is not None:
        payload = prepare_news_input(snapshot_dir)
        news_result = run_news(payload, news_policy, news_ai_assessment)
        freeze_result(frozen_path, news_result, result_name="news_result.json")
    news_result = news_result or {"status": "not_assessed", "snapshot_id": snapshot_id,
                                  "ticker": ticker, "market": market_name, "horizon": manifest.get("horizon"),
                                  "asof_timestamp": manifest.get("asof_timestamp")}
    attach_news_observation(adversarial, news_result)
    if not adversarial["may_report_probability"]:
        consensus["prob_up"] = None
        consensus["reportable_probability"] = None
        consensus["direction"] = "unavailable"
        consensus["status"] = "unavailable"
        consensus["production"]["prob_up"] = None
        consensus["production"]["model_lean"] = "unavailable"
        consensus["reason_codes"] = sorted(set(consensus.get("reason_codes", []) + adversarial.get("reason_codes", [])))
    _stage(stages, "audit", "complete", payload=adversarial, run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "audit.json", adversarial)
    (destination / "researchers").mkdir(exist_ok=True)
    for path, item in results.items():
        write_json(destination / "researchers" / f"{path}.json", item)
    freeze_result(destination / "news_result.json", news_result, result_name="news_result.json")

    news_status = news_result.get("status") or "not_assessed"
    assessed = news_assessed(news_status)
    stage_status = "partial" if assessed and news_status_label(news_status) == "partial" else "complete" if assessed else "not_assessed"
    _stage(stages, "news", stage_status,
           payload={"status": news_status, "warning_count": len(news_result.get("warnings") or [])},
           reason=None if stage_status == "complete" else (
               f"news assessed with {len(news_result.get('warnings') or [])} disclosed warning(s)"
               if stage_status == "partial" else "news evidence unavailable or not assessed"
           ),
           run_id=run_id, snapshot_id=snapshot_id)
    _stage(stages, "cross-validation", "not_applicable",
           reason="news and Quant are reported independently; news never re-scores the production probability", run_id=run_id, snapshot_id=snapshot_id)
    _stage(stages, "risk", "not_assessed",
           reason="returns are gross close-to-close observations; execution, spread, slippage and fees are not modelled", run_id=run_id, snapshot_id=snapshot_id)

    synthesis = build_synthesis({**run, "snapshot": report_manifest, "consensus": consensus,
                                 "adversarial_audit": adversarial, "researchers": results}, news_result)
    final = {
        **run,
        "snapshot": report_manifest,
        "researchers": results,
        "consensus": consensus,
        "adversarial_audit": adversarial,
        "news_result": news_result,
        "synthesis": synthesis,
        "analysis_status": consensus.get("status"),
        "reporting_status": reporting_status_for({
            "consensus": consensus, "adversarial_audit": adversarial,
        }),
    }
    _stage(stages, "final", "complete", payload={"consensus": consensus, "audit": adversarial, "synthesis": synthesis},
           run_id=run_id, snapshot_id=snapshot_id)
    _validate_stages(stages)
    final["stages"] = stages

    agent_summary = enforce_agent_summary_budget(build_agent_summary(final))
    markdown = render_markdown(final)
    response = render_final_response(final, summary=agent_summary)
    validate_response_text(final, response)
    write_json(destination / "agent_summary.json", agent_summary)
    _markdown_path = destination / "report.md"
    _markdown_path.write_text(markdown, encoding="utf-8")
    write_json(destination / "analysis.json", final)
    (destination / "final_response.md").write_text(response, encoding="utf-8")
    final["artifacts"] = {
        "analysis": str(destination / "analysis.json"),
        "markdown": str(_markdown_path),
        "agent_summary": str(destination / "agent_summary.json"),
        "final_response": str(destination / "final_response.md"),
    }
    write_json(destination / "analysis.json", final)
    run["stages"] = stages
    run["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    if on_analysis_created is not None:
        on_analysis_created(destination)
    return destination
