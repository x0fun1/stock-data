"""Run the news-focused analysis pipeline against one frozen snapshot."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .forecast_policy import POLICY_VERSION, REQUIRED_STAGES
from .jsonio import write_json
from .news import (
    freeze_result,
    load_frozen_result,
    news_assessed,
    news_status_label,
    prepare_news_input,
    run_news,
)
from .paths import safe_output_destination
from .report import (
    build_agent_summary,
    enforce_agent_summary_budget,
    render_final_response,
    render_markdown,
    reporting_status_for,
)
from .snapshot import load_snapshot


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
    allowed = {"complete", "partial", "skipped", "not_applicable", "not_assessed", "blocked", "insufficient_data"}
    for item in stages:
        if item["status"] not in allowed:
            raise ValueError(f"invalid stage status: {item['status']}")
        if item["status"] != "complete" and not item.get("reason"):
            raise ValueError(f"stage {item['stage']} must explain a non-complete status")


def _analysis_status(news_status: str) -> str:
    if not news_assessed(news_status):
        return "not_assessed"
    return "partial" if news_status_label(news_status) == "partial" else "complete"


def run_analysis(
    snapshot_dir: Path, output_root: Path, news_policy: dict[str, Any] | None = None,
    *, on_analysis_created: Callable[[Path], None] | None = None,
    news_ai_assessment: dict[str, Any] | None = None,
    artifact_profile: str = "audit",
) -> Path:
    if artifact_profile not in {"audit", "compact"}:
        raise ValueError("artifact_profile must be audit or compact")
    started = time.perf_counter()
    manifest, market = load_snapshot(snapshot_dir)
    snapshot_id = manifest["snapshot_id"]
    run_id = manifest.get("run_id") or snapshot_id
    ticker, market_name = manifest["ticker"], manifest["market"]
    request = manifest.get("request") or {}
    report_manifest = {
        "ticker": ticker,
        "market": market_name,
        "asset_type": request.get("asset_type") or "unknown",
        "asof_timestamp": manifest.get("asof_timestamp"),
        "snapshot_id": snapshot_id,
        "policy": manifest.get("policy") or {},
        "data_validation": manifest.get("data_validation") or {},
        "data_quality": manifest.get("data_quality") or {},
        "warnings": (manifest.get("data_quality") or {}).get("issues") or [],
    }
    run = {
        "schema_version": "analysis-4.0",
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "policy_version": POLICY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "package_version": __version__,
    }
    destination = safe_output_destination(output_root, run_id)
    destination.mkdir(parents=True, exist_ok=True)
    stages: list[dict[str, Any]] = []
    _stage(stages, "request", "complete", payload=request, run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "request.json", request)
    _stage(stages, "collection", "complete", payload=manifest.get("source_payload_sha256"), run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "snapshot.json", {
        "schema_version": "analysis-snapshot-binding-4.0",
        "snapshot_id": snapshot_id,
        "source_payload_sha256": manifest.get("source_payload_sha256"),
        "policy": manifest.get("policy") or {},
        "binding": {"snapshot_id": snapshot_id, "content_sha256": manifest.get("snapshot_content_sha256")},
    })
    validation = manifest.get("data_validation") or {}
    blocked = bool(validation.get("blockers"))
    data_status = "blocked" if blocked else "complete"
    _stage(stages, "data", data_status, payload=validation,
           reason="snapshot data validation reported blockers" if blocked else None,
           run_id=run_id, snapshot_id=snapshot_id)
    write_json(destination / "data.json", {
        "data_quality": manifest.get("data_quality"),
        "data_validation": validation,
        "policy": manifest.get("policy") or {},
    })

    news_result = None
    frozen_path = snapshot_dir / "news_result.json"
    if frozen_path.is_file():
        try:
            news_result, _ = load_frozen_result(frozen_path)
        except ValueError:
            news_result = None
    if news_result is None and news_policy is not None:
        payload = prepare_news_input(snapshot_dir)
        preview = None
        preview_path = snapshot_dir / "news_preview.json"
        if preview_path.is_file():
            try:
                preview, _receipt = load_frozen_result(preview_path)
            except ValueError:
                preview = None
        news_result = run_news(payload, news_policy, news_ai_assessment, base_result=preview)
        freeze_result(frozen_path, news_result, result_name="news_result.json")
    news_result = news_result or {"status": "not_assessed", "snapshot_id": snapshot_id,
                                  "ticker": ticker, "market": market_name,
                                  "asof_timestamp": manifest.get("asof_timestamp")}
    news_status = news_result.get("status") or "not_assessed"
    assessed = news_assessed(news_status)
    severity_counts = news_result.get("warning_severity_counts") or {}
    evidence_warnings = int(severity_counts.get("evidence_affecting") or 0)
    presentation_warnings = int(severity_counts.get("presentation") or 0)
    stage_status = "partial" if assessed and news_status_label(news_status) == "partial" else "complete" if assessed else "not_assessed"
    _stage(stages, "news", stage_status,
           payload={"status": news_status, "warning_count": len(news_result.get("warnings") or []),
                    "warning_severity": {"evidence_affecting": evidence_warnings,
                                         "presentation": presentation_warnings}},
           reason=None if stage_status == "complete" else (
               f"news assessed with {evidence_warnings} evidence-affecting and "
               f"{presentation_warnings} presentation warning(s)"
               if stage_status == "partial" else "news evidence unavailable or not assessed"
           ),
           run_id=run_id, snapshot_id=snapshot_id)
    freeze_result(destination / "news_result.json", news_result, result_name="news_result.json")

    elapsed_seconds = round(time.perf_counter() - started, 3)
    final = {
        **run,
        "analysis_id": run_id,
        "elapsed_seconds": elapsed_seconds,
        "snapshot": report_manifest,
        "news_result": news_result,
        "analysis_status": _analysis_status(news_status),
        "reporting_status": reporting_status_for({"news_result": news_result}),
    }
    _stage(stages, "final", "complete", payload={"news": news_result}, run_id=run_id, snapshot_id=snapshot_id)
    _validate_stages(stages)
    final["stages"] = stages

    agent_summary = enforce_agent_summary_budget(build_agent_summary(final))
    markdown = render_markdown(final)
    response = render_final_response(final, summary=agent_summary)
    write_json(destination / "agent_summary.json", agent_summary)
    (destination / "report.md").write_text(markdown, encoding="utf-8")
    write_json(destination / "analysis.json", final)
    (destination / "final_response.md").write_text(response, encoding="utf-8")
    final["artifacts"] = {
        "analysis": str(destination / "analysis.json"),
        "markdown": str(destination / "report.md"),
        "agent_summary": str(destination / "agent_summary.json"),
        "final_response": str(destination / "final_response.md"),
    }
    if on_analysis_created is not None:
        on_analysis_created(destination)
    return destination
