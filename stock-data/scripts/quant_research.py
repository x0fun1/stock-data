#!/usr/bin/env python3
"""CLI for freezing stock-data responses and running quantitative research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from quant_research.contracts import normalize_request
from quant_research.orchestrator import run_analysis
from quant_research.stock_data_adapter import StockDataAdapter
from quant_research.snapshot import DOMAINS, freeze_snapshot


def _json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _schema(kind: str) -> dict[str, Any]:
    if kind == "request":
        return {"required": {"ticker": "str", "market": "US|HK", "horizon": "1D|5D|20D", "asof": "ISO-8601 timestamp with timezone or null", "mode": "standard|strict"}, "defaults": {"market": "US", "horizon": "5D", "asof": None, "mode": "standard"}}
    if kind == "collection":
        return {"required": ["request", "captured_at_utc", "domains"], "domains": {name: {"status": "complete|partial|unavailable|failed", "actual_source": "provider name", "fetched_at_utc": "timestamp", "data": "normalized domain payload"} for name in DOMAINS}, "market_data": {"required": "data.bars[]", "bar_fields": ["date", "open", "high", "low", "close", "volume"], "adjustment": "adjusted|unadjusted|split_adjusted|total_return"}}
    if kind == "gateway-responses":
        return {"request_file": "normalized research request object", "responses_file": {"domains": "map of actual stock-data gateway responses"}, "market_normalization": "caller maps gateway candles into data.bars and preserves the original envelope as gateway_envelope", "provider_calls": "performed by the Skill runtime, never by this script"}
    if kind == "researcher":
        return {"required": ["researcher_id", "result_role", "ticker", "horizon", "snapshot_id", "direction", "prob_up", "prob_down", "expected_return", "confidence", "probability_source", "evidence", "validation", "data_used", "warnings", "status"], "researcher_ids": ["quant", "factor", "ml", "factor_backtest"], "result_roles": ["forecast", "diagnostic"], "statuses": ["success", "partial", "failed", "insufficient_data", "invalid"], "probability_source_required_when_prob_up_is_numeric": True, "diagnostic_paths_enter_consensus": False}
    if kind == "final":
        return {"required": ["analysis_id", "snapshot", "researchers", "consensus", "adversarial_audit", "quant_result_freeze", "news_result", "news_result_freeze", "synthesis"], "formats": ["quant_result.json", "news_result.json", "report.json", "report.md"], "probability_blending": "prohibited"}
    if kind == "news":
        return {"news_input_fields": ["ticker", "market", "horizon", "asof_timestamp", "snapshot_id", "news_status", "source_sentiment", "articles", "bars", "warnings"], "news_result_fields": ["overall_direction", "event_strength", "sentiment", "source_agreement", "market_confirmation", "major_events", "catalysts", "risks", "confidence", "warnings"], "quant_fields_visible_to_news": False, "prob_up_emitted": False, "market_data": "frozen snapshot OHLCV only"}
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
        elif args.command == "analyze":
            policy = json.loads(args.news_policy.read_text(encoding="utf-8-sig")) if args.news_policy else None
            if policy is not None and not isinstance(policy, dict):
                raise ValueError("news policy file must contain a JSON object")
            location = run_analysis(args.snapshot_dir, args.output_root, policy)
            print(json.dumps({"status": "success", "report_dir": str(location), "json": str(location / "report.json"), "markdown": str(location / "report.md")}, ensure_ascii=False))
        else:
            print(json.dumps(_schema(args.kind), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
