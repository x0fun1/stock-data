#!/usr/bin/env python3
"""CLI for freezing stock-data responses and running quantitative research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from quant_research.cleanup import (
    cleanup_analysis,
    cleanup_owned_run,
    create_run_workspace,
    mark_run_failed,
    release_pipeline_lock,
    update_run_workspace,
)
from quant_research.contracts import MISSING_PROBABILITY_LABEL, normalize_request, utc_now
from quant_research.delivery import validate_final_response
from quant_research.orchestrator import run_analysis
from quant_research.paths import safe_output_destination
from quant_research.retention import (
    DEFAULT_CACHE_DAYS,
    DEFAULT_PENDING_DAYS,
    DEFAULT_RETAIN_DAYS,
    GC_THROTTLE_HOURS,
    gc_runtime,
)
from quant_research.security import bounded_text
from quant_research.snapshot import DOMAINS, freeze_snapshot
from quant_research.stock_data_adapter import StockDataAdapter


def _json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _schema(kind: str) -> dict[str, Any]:
    if kind == "request":
        return {"schema_version": "1.2", "required": {"ticker": "validated symbol str", "market": "US|HK", "horizon": "1D|5D|20D", "asof": "ISO-8601 timestamp with timezone or null", "mode": "standard|strict"}, "optional": {"intent": "forecast|descriptive|factor_research", "asset_type": "stock|etf|index|unknown", "instrument": "resolved_ticker/provider_symbols/source/currency/unit", "history_window": "start/end/selection", "history_start": "ISO date", "history_end": "ISO date"}, "defaults": {"market": "US", "horizon": "5D", "asof": None, "mode": "standard", "intent": "forecast", "asset_type": "unknown"}}
    if kind == "collection":
        return {"schema_version": "1.2", "required": ["request", "captured_at_utc", "domains"], "domains": {name: {"status": "complete|partial|unavailable|failed", "actual_source": "provider name", "fetched_at_utc": "timestamp", "data": "normalized domain payload"} for name in DOMAINS}, "market_data": {"forecast_required": ["data.bars[]", "data.symbol", "data.frequency=1d", "data.history_window", "actual_source", "source_timestamp", "fetched_at_utc", "currency", "unit"], "nonfatal_optional": ["session_calendar", "latest_price_observations[]", "adjustment", "adjustment_evidence"], "calendar_resolution": ["complete supplied calendar", "pinned optional exchange-calendar library", "observed OHLCV dates (estimated horizon)"], "bar_fields": ["date", "open", "high", "low", "close", "volume"], "adjustment": "adjusted|unadjusted|split_adjusted|total_return|unknown"}, "missing_evidence": "calendar/adjustment/OOS/calibration gaps degrade confidence; only fatal prediction-integrity failures block probability"}
    if kind == "gateway-responses":
        return {"request_file": "normalized research request object", "responses_file": {"domains": "map of actual stock-data gateway responses"}, "market_normalization": "caller maps gateway candles into data.bars and preserves the original envelope as gateway_envelope", "provider_calls": "performed by the Skill runtime, never by this script"}
    if kind == "researcher":
        return {"required": ["researcher_id", "result_role", "ticker", "horizon", "snapshot_id", "direction", "prob_up", "prob_down", "raw_probability", "calibrated_probability", "reportable_probability", "expected_return", "confidence", "probability_source", "evidence", "validation", "data_used", "warnings", "status"], "researcher_ids": ["quant", "factor", "ml", "factor_backtest"], "result_roles": ["forecast", "diagnostic"], "statuses": ["success", "partial", "failed", "insufficient_data", "invalid"], "probability_source_required_when_prob_up_is_numeric": True, "diagnostic_paths_enter_consensus": False}
    if kind == "final":
        return {"schema_version": "1.2", "required": ["analysis_id", "analysis_status", "reporting_status", "stages", "snapshot", "researchers", "consensus", "adversarial_audit", "quant_result_freeze", "news_result", "news_result_freeze", "synthesis"], "synthesis_quant": {"required": ["prob_up", "direction", "confidence", "confidence_score", "confidence_score_kind", "predictive_evidence_grade", "agreement", "reporting_status", "raw_probability", "calibrated_probability", "reportable_probability"], "optional": ["diversity"], "source": "quant_result.json.consensus", "missing_probability_display": MISSING_PROBABILITY_LABEL, "news_may_change_prob_up": False}, "formats": ["quant_result.json", "news_result.json", "report.json", "report.md", "agent_summary.json", "final_response.md"], "delivery_validation": "validate-response --analysis-dir <report_dir> --response-file <prepared_reply.md>; preserve literal Quant/identity/relationship/reporting-status fields from final_response.md", "artifact_lifecycle": {"owned_pipeline": "pipeline --cleanup-json writes into runtime/runs/<run_id>; cleanup-run validates the actual prepared reply before deleting only registered JSON", "retained": ["report.md", "final_response.md", "cleanup_receipt.txt"], "research_archive": "omit --cleanup-json or use --keep-json; existing shared snapshots/analyses are never included in run cleanup", "orphan_sweep": "cleanup-run and gc delete only crash-leftover scratch names (.<file>.<12hex>.tmp, .<snapshot_id>.<8chars> dirs) before validating the tree; any other unknown file still blocks deletion", "retention": "gc expires a run directory only when its receipt is complete, it holds no JSON, it is older than --retain-days and no pipeline lock is live; runs left in awaiting_cleanup past --pending-days are cleaned first with delivery=unverified because the exact outgoing text is unavailable", "cache": "gc prunes provider cache files older than --cache-days; the pipeline runs the same sweep at most once per 24h unless --no-gc", "schema_change": False}, "probability_blending": "prohibited"}
    if kind == "news":
        return {"news_input_fields": ["ticker", "market", "horizon", "asof_timestamp", "snapshot_id", "news_status", "source_sentiment", "articles", "bars", "warnings", "coverage"], "news_result_fields": ["overall_direction", "event_strength", "sentiment", "source_agreement", "market_confirmation", "major_events", "background_records", "catalysts", "risks", "mixed_events", "confidence", "warnings", "ai_lean_candidates", "ai_lean"], "quant_fields_visible_to_news": False, "prob_up_emitted": False, "market_data": "frozen snapshot OHLCV only",
                "ai_lean_artifact": {"path": "--news-ai-assessment <json>", "schema_version": "1.0", "required": ["schema_version", "ticker", "asof_timestamp", "items"], "optional": ["basis", "model"], "item_fields": ["news_id", "lean", "reason", "evidence_grade", "headline", "source", "published_at", "url", "relevance_basis"], "lean_values": ["bullish", "bearish", "mixed", "neutral", "unclear"], "score_generation": "aggregator_only_from_quant_gate", "probability_effect": "none; display-only evidence, never enters direction/confidence/cross-validation or Quant P(up)", "rejected_when": ["unknown or repeated news_id", "ticker/as-of mismatch", "unsupported field", "Quant vocabulary in any text field", "non-allow-listed lean label"]}}
    raise ValueError(f"unknown schema kind: {kind}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quant research stage for the stock-data Skill; provider access remains in the Skill workflow.")
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request", help="normalize a research request JSON file")
    request.add_argument("--input", type=Path, required=True)
    request.add_argument("--output", type=Path, required=True)
    adapt = commands.add_parser("adapt", help="preserve gateway envelopes and write a normalized collection receipt")
    adapt.add_argument("--request", type=Path, required=True)
    adapt.add_argument("--responses", type=Path, required=True)
    adapt.add_argument("--output", type=Path, default=Path("collected.json"))
    adapt.add_argument("--captured-at-utc")
    freeze = commands.add_parser("freeze", help="validate and freeze one captured stock-data payload")
    freeze.add_argument("--input", type=Path, required=True)
    freeze.add_argument("--output-root", type=Path, default=Path("runtime/snapshots"))
    analyze = commands.add_parser("analyze", help="run isolated research paths against one frozen snapshot")
    analyze.add_argument("--snapshot-dir", type=Path, required=True)
    analyze.add_argument("--output-root", type=Path, default=Path("runtime/research"))
    analyze.add_argument("--news-policy", type=Path, help="optional JSON event-type half-life policy for session-based decay")
    analyze.add_argument("--news-ai-assessment", type=Path, help="optional host-produced headline-level AI lean artifact (display-only evidence)")
    pipeline = commands.add_parser("pipeline", help="compose request/adapt/freeze/analyze from already collected gateway DATA")
    pipeline.add_argument("--request", type=Path, required=True)
    pipeline.add_argument("--responses", type=Path, required=True)
    pipeline.add_argument("--output-root", type=Path, default=Path("runtime"))
    pipeline.add_argument("--captured-at-utc")
    pipeline.add_argument("--news-ai-assessment", type=Path, help="optional host-produced headline-level AI lean artifact (display-only evidence)")
    pipeline_mode = pipeline.add_mutually_exclusive_group()
    pipeline_mode.add_argument("--cleanup-json", action="store_true", help="isolate this pipeline run for explicit post-validation JSON cleanup")
    pipeline_mode.add_argument("--keep-json", action="store_true", help="retain JSON in the legacy shared output layout (default)")
    pipeline.add_argument("--no-gc", action="store_true", help="skip the throttled orphan/retention sweep that every pipeline start runs first")
    pipeline.add_argument("--retain-days", type=int, default=DEFAULT_RETAIN_DAYS, help="delete completed run directories older than this many days during the sweep")
    delivery = commands.add_parser("validate-response", help="check a prepared reply against frozen Quant/News results before delivery")
    delivery.add_argument("--analysis-dir", type=Path, required=True)
    delivery.add_argument("--response-file", type=Path, required=True)
    cleanup = commands.add_parser("cleanup-run", help="preview or remove JSON owned by a run or explicitly named analysis")
    cleanup.add_argument("--run-dir", type=Path, help="isolated runtime/runs/<run_id> directory")
    cleanup.add_argument("--analysis-dir", type=Path, help="owned analysis directory (with --run-dir) or legacy analysis-only target")
    cleanup.add_argument("--response-file", type=Path, help="the exact prepared reply text validated before cleanup")
    cleanup.add_argument("--dry-run", action="store_true", help="show registered JSON targets and byte count without deleting")
    cleanup.add_argument("--force-incomplete", action="store_true", help="explicitly allow cleanup of a failed/incomplete run")
    cleanup.add_argument("--assume-delivered", action="store_true", help="clean a run whose exact reply the host cannot hand back; the retained final_response.md is still validated in place")
    gc = commands.add_parser("gc", help="sweep orphan scratch paths, finish abandoned runs, expire old run directories and prune the provider cache")
    gc.add_argument("--output-root", type=Path, default=Path("runtime"), help="runtime root that holds runs/ and the shared layout")
    gc.add_argument("--retain-days", type=int, default=DEFAULT_RETAIN_DAYS, help="delete completed run directories older than this many days (0 disables)")
    gc.add_argument("--pending-days", type=int, default=DEFAULT_PENDING_DAYS, help="finish JSON cleanup for runs left awaiting cleanup longer than this (0 disables)")
    gc.add_argument("--cache-dir", type=Path, help="provider cache directory (default: <output-root>/yfinance-cache)")
    gc.add_argument("--cache-days", type=int, default=DEFAULT_CACHE_DAYS, help="delete cache files older than this many days (0 disables)")
    gc.add_argument("--dry-run", action="store_true", help="report what would be removed without deleting")
    gc.add_argument("--no-throttle", action="store_true", help="ignore the recorded last-run timestamp")
    schema = commands.add_parser("schema", help="print a machine-readable contract")
    schema.add_argument("--kind", choices=("request", "gateway-responses", "collection", "researcher", "news", "final"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "request":
            value = json.loads(args.input.read_text(encoding="utf-8-sig"))
            normalized = normalize_request(value)
            _json_file(args.output, normalized)
            print(json.dumps({"status": "success", "request": normalized}, ensure_ascii=False))
        elif args.command == "adapt":
            request_value = json.loads(args.request.read_text(encoding="utf-8-sig"))
            response_value = json.loads(args.responses.read_text(encoding="utf-8-sig"))
            if not isinstance(response_value, dict):
                raise ValueError("responses file must contain a domain map")
            domain_map = response_value.get("domains", response_value)
            if not isinstance(domain_map, dict):
                raise ValueError("responses.domains must be an object")
            location = StockDataAdapter().write_receipt(request_value, domain_map, args.output, captured_at_utc=args.captured_at_utc)
            print(json.dumps({"status": "success", "collection_receipt": str(location)}, ensure_ascii=False))
        elif args.command == "freeze":
            location = freeze_snapshot(args.input, args.output_root)
            print(json.dumps({"status": "success", "snapshot_dir": str(location)}, ensure_ascii=False))
        elif args.command in {"analyze", "pipeline"}:
            owned_run = None
            gc_report: dict[str, Any] | None = None
            try:
                if args.command == "pipeline":
                    # Runs before anything is read or written, in both layout modes:
                    # a host without a scheduler still collects crash leftovers and
                    # expired runs, and GC must never fail the research pipeline.
                    if not args.no_gc:
                        try:
                            gc_report = gc_runtime(args.output_root, retain_days=args.retain_days,
                                                   throttle_hours=GC_THROTTLE_HOURS)
                        except Exception as exc:  # noqa: BLE001 - GC is advisory
                            gc_report = {"status": "error", "error_type": type(exc).__name__,
                                         "error": bounded_text(exc)}
                    request_value = normalize_request(json.loads(args.request.read_text(encoding="utf-8-sig")))
                    response_value = json.loads(args.responses.read_text(encoding="utf-8-sig"))
                    if not isinstance(response_value, dict) or not isinstance(response_value.get("domains", response_value), dict):
                        raise ValueError("responses must contain a domain map")
                    if args.cleanup_json:
                        owned_run, _ = create_run_workspace(args.output_root)
                        collection_name = f"collection_{owned_run.name}.json"
                        receipt = safe_output_destination(owned_run / "collections", collection_name)
                        update_run_workspace(owned_run, collection_name=collection_name)
                        StockDataAdapter().write_receipt(request_value, response_value.get("domains", response_value), receipt, captured_at_utc=args.captured_at_utc)
                        snapshot_dir = freeze_snapshot(
                            receipt, owned_run / "snapshots",
                            on_snapshot_created=lambda path: update_run_workspace(owned_run, snapshot_id=path.name),
                        )
                        output_root = owned_run / "research"
                    else:
                        receipt = safe_output_destination(args.output_root / "collections", "collection_" + utc_now().replace(":", "").replace("-", "").replace(".", "") + ".json")
                        StockDataAdapter().write_receipt(request_value, response_value.get("domains", response_value), receipt, captured_at_utc=args.captured_at_utc)
                        snapshot_dir = freeze_snapshot(receipt, args.output_root / "snapshots")
                        output_root = args.output_root / "research"
                else:
                    snapshot_dir, output_root = args.snapshot_dir, args.output_root
                policy = json.loads(args.news_policy.read_text(encoding="utf-8-sig")) if getattr(args, "news_policy", None) else None
                if policy is not None and not isinstance(policy, dict):
                    raise ValueError("news policy file must contain a JSON object")
                ai_assessment_path = getattr(args, "news_ai_assessment", None)
                ai_assessment = json.loads(ai_assessment_path.read_text(encoding="utf-8-sig")) if ai_assessment_path else None
                if ai_assessment is not None and not isinstance(ai_assessment, dict):
                    raise ValueError("news AI assessment file must contain a JSON object")
                if owned_run is None:
                    location = run_analysis(snapshot_dir, output_root, policy, news_ai_assessment=ai_assessment)
                else:
                    location = run_analysis(
                        snapshot_dir, output_root, policy,
                        on_analysis_created=lambda path: update_run_workspace(owned_run, analysis_id=path.name),
                        news_ai_assessment=ai_assessment,
                    )
                    update_run_workspace(owned_run, state="awaiting_cleanup")
                summary = json.loads((location / "agent_summary.json").read_text(encoding="utf-8"))
                result = {"status": "success", "analysis_status": summary["analysis_status"], "may_report_direction": summary["sections"]["final_synthesis"]["may_report_direction"], "report_dir": str(location), "json": str(location / "report.json"), "markdown": str(location / "report.md"), "agent_summary": str(location / "agent_summary.json"), "final_response": str(location / "final_response.md")}
                if owned_run is not None:
                    result["run_dir"] = str(owned_run)
                    result["cleanup_required"] = True
                    release_pipeline_lock(owned_run)
                if gc_report is not None:
                    result["gc"] = gc_report
                print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                if owned_run is not None:
                    mark_run_failed(owned_run, exc)
                raise
        elif args.command == "validate-response":
            print(json.dumps(validate_final_response(args.analysis_dir, args.response_file), ensure_ascii=False))
        elif args.command == "cleanup-run":
            if args.run_dir is not None:
                result = cleanup_owned_run(args.run_dir, analysis_dir=args.analysis_dir,
                                           response_file=args.response_file,
                                           dry_run=args.dry_run, force_incomplete=args.force_incomplete,
                                           delivery_unverified=args.assume_delivered)
            else:
                if args.analysis_dir is None:
                    raise ValueError("provide --run-dir or --analysis-dir")
                result = cleanup_analysis(args.analysis_dir, response_file=args.response_file,
                                          dry_run=args.dry_run, force_incomplete=args.force_incomplete,
                                          delivery_unverified=args.assume_delivered)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("cleanup_status") in {"complete", "already_cleaned", "dry_run"} else 2
        elif args.command == "gc":
            result = gc_runtime(args.output_root, retain_days=args.retain_days,
                                cache_days=args.cache_days, pending_days=args.pending_days,
                                cache_dir=args.cache_dir, dry_run=args.dry_run,
                                throttle_hours=None if args.no_throttle else GC_THROTTLE_HOURS)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("status") in {"success", "throttled"} else 2
        else:
            # Machine-readable schema must survive Windows console code pages.
            print(json.dumps(_schema(args.kind), ensure_ascii=True, indent=2))
    except Exception as exc:  # noqa: BLE001 - 顶层 CLI 守卫：任何失败都转成机器可读的错误输出
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": bounded_text(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
