#!/usr/bin/env python3
"""Evidence-first stock/ETF news research CLI."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from news_research import __version__
from news_research.common.jsonio import read_json
from news_research.common.security import redact_text
from news_research.contracts import schema
from news_research.orchestration import analyze_run, collect, prepare_run, report_run, validate_report
from news_research.sources.registry import source_registry
from news_research.storage.store import gc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="US/HK stock and ETF evidence-first news research (30-60 calendar days)")
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="List documented capabilities; no account probe is implied")
    contract = sub.add_parser("schema", help="Print the versioned JSON contract")
    contract.add_argument("--kind", choices=("request", "assessment", "import"), required=True)
    collection = sub.add_parser("collect", help="Collect/import evidence, persist receipts and freeze a run")
    collection.add_argument("--request", type=Path, required=True)
    collection.add_argument("--imports", type=Path)
    collection.add_argument("--output-root", type=Path, required=True)
    collection.add_argument("--incremental", action="store_true", help="Reuse verified history and refresh the recent interval")
    for name, help_text in (("prepare", "Prepare frozen evidence batches for the host"),
                            ("analyze", "Validate a host assessment against frozen evidence"),
                            ("report", "Render a report, including incomplete analysis states"),
                            ("validate-report", "Check the exact response and its citations")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "analyze":
            command.add_argument("--assessment", type=Path, required=True)
        if name == "validate-report":
            command.add_argument("--response-file", type=Path, required=True)
    cleanup = sub.add_parser("gc", help="Clean registered runs and expired licensed text, never the shared evidence ledger")
    cleanup.add_argument("--output-root", type=Path, required=True)
    cleanup.add_argument("--retention-days", type=int, default=120)
    mode = cleanup.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview (the default)")
    mode.add_argument("--apply", action="store_true", help="Apply the explicitly requested cleanup")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "sources":
            output = source_registry()
        elif args.command == "schema":
            output = schema(args.kind)
        elif args.command == "collect":
            output = collect(read_json(args.request), args.output_root, read_json(args.imports) if args.imports else None,
                             incremental=args.incremental)
        elif args.command == "prepare":
            output = prepare_run(args.run_dir)
        elif args.command == "analyze":
            output = analyze_run(args.run_dir, args.assessment)
        elif args.command == "report":
            output = report_run(args.run_dir)
        elif args.command == "validate-report":
            output = validate_report(args.run_dir, args.response_file)
        else:
            output = gc(args.output_root, retention_days=args.retention_days, dry_run=not args.apply)
    except (ValueError, OSError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": redact_text(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
