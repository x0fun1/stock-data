#!/usr/bin/env python
"""Command line entry for the US/HK news and message research skill."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from stock_research.config import default_runtime_root, default_skill_root, load_policies
from stock_research.contracts import ResearchRequest
from stock_research.control.maintenance import IMPORT_KINDS, import_file
from stock_research.control.orchestration import DATASETS, run_research
from stock_research.storage.catalog import Catalog
from stock_research.storage.curated_store import CuratedStore


def _request_from_args(args) -> ResearchRequest:
    if args.request:
        with open(args.request, "r", encoding="utf-8") as handle:
            return ResearchRequest.from_dict(json.load(handle))
    if not args.ticker:
        raise SystemExit("provide --request or at least one --ticker")
    cutoff = None
    if args.cutoff:
        cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    return ResearchRequest(
        market=args.market,
        tickers=args.ticker,
        cutoff=cutoff,
        news_lookback_days=args.news_lookback,
        output_format="json" if args.json else "markdown",
    )


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_research(args) -> int:
    bundle, markdown = run_research(_request_from_args(args), runtime_root=args.root or default_runtime_root())
    if args.json:
        _print({"summary": {"run_id": bundle.run_manifest.run_id, "news_impact_summary": bundle.news_impact_summary,
                            "macro_background": bundle.macro_background, "limitations": bundle.limitations},
                "report": markdown})
    else:
        print(markdown)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
    return 0


def cmd_import(args) -> int:
    _print(import_file(args.kind, args.file, args.market, args.root or default_runtime_root(), source_id=args.source))
    return 0


def cmd_import_news_reviews(args) -> int:
    from stock_research.control.news_reviews import import_news_reviews
    _print(import_news_reviews(args.file, args.root or default_runtime_root()))
    return 0


def cmd_news_collect(args) -> int:
    from stock_research.acquisition.news_collector import collect_news_evidence
    _print(collect_news_evidence(args.root or default_runtime_root(), args.workflow_id, args.collection_id,
                                 provider=args.provider, max_sources=args.max_sources,
                                 plugin_batch_id=args.plugin_batch_id))
    return 0


def cmd_news_plugin_plan(args) -> int:
    from stock_research.acquisition.news_collector import prepare_plugin_title_plan
    _print(prepare_plugin_title_plan(args.root or default_runtime_root(), args.workflow_id, args.batch_id))
    return 0


def cmd_news_workflow(args) -> int:
    from stock_research.control.news_workflow import (apply_news_assessments, finalize_news_workflow,
                                                      next_news_tasks, prepare_news_workflow, workflow_status)
    root = Path(args.root or default_runtime_root())
    folder = root / "news-workflows" / args.workflow_id
    if args.step == "prepare":
        result = prepare_news_workflow(root, args.ticker, args.workflow_id, market=args.market)
    elif args.step == "next":
        result = next_news_tasks(folder, args.limit)
    elif args.step == "apply":
        if not args.file:
            raise ValueError("apply requires --file with per-item host LLM assessments")
        result = apply_news_assessments(root, folder, args.file)
    elif args.step == "finalize":
        result = finalize_news_workflow(folder)
    else:
        result = workflow_status(folder)
    _print(result)
    return 0


def cmd_reproduce(args) -> int:
    root = Path(args.root or default_runtime_root())
    entry = Catalog(root).get_run(args.run_id)
    if entry is None:
        raise SystemExit(f"run {args.run_id} is not in the catalog")
    data = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
    report_path = Path(data.get("run_manifest", {}).get("outputs", {}).get("markdown", ""))
    if report_path.is_file():
        print(report_path.read_text(encoding="utf-8"))
        return 0
    _print(data.get("run_manifest", {}))
    return 0


def cmd_doctor(args) -> int:
    root = Path(args.root or default_runtime_root())
    policies = load_policies(default_skill_root())
    catalog = CuratedStore(root).catalog
    datasets = {}
    for name in DATASETS:
        entry = catalog.get_dataset(name)
        datasets[name] = None if entry is None else {"version": entry["version"], "rows": entry["rows"], "created_at": entry["created_at"]}
    storage_bytes = {name: sum(path.stat().st_size for path in (root / name).rglob("*") if path.is_file())
                     for name in ("raw", "curated", "reports", "news-workflows")}
    storage_bytes["sqlite_index"] = (root / "catalog.sqlite").stat().st_size
    _print({
        "skill_root": str(default_skill_root()),
        "runtime_root": str(root),
        "policy_version": policies.version,
        "policy_hash": policies.hash,
        "datasets": datasets,
        "storage_bytes": storage_bytes,
        "packaging": {"runtime_data_in_skill_bundle": False, "sqlite_role": "version_and_file_index",
                      "data_storage": "compressed_JSON_records_and_raw_files"},
        "scope": "news, macro and company-message interpretation; no price model is bundled",
        "note": "No credentials are printed. Live acquisition requires a configured authorized source.",
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="US/HK evidence-first news and message research skill")
    root_parent = argparse.ArgumentParser(add_help=False)
    root_parent.add_argument("--root", help="runtime data root (default: this skill's runtime directory)")
    parser.add_argument("--root", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    research = sub.add_parser("research", help="interpret current news and macro messages in one report", parents=[root_parent])
    research.add_argument("--request", help="request JSON file")
    research.add_argument("--market", default="US", choices=["US", "HK"])
    research.add_argument("--ticker", action="append", default=[])
    research.add_argument("--cutoff", help="knowledge cutoff with an explicit timezone; default now")
    research.add_argument("--news-lookback", type=int, default=30)
    research.add_argument("--json", action="store_true")
    research.add_argument("--out")
    research.set_defaults(func=cmd_research)

    imp = sub.add_parser("import", help="import an authorized local CSV/JSON file", parents=[root_parent])
    imp.add_argument("--kind", required=True, choices=IMPORT_KINDS)
    imp.add_argument("--file", required=True)
    imp.add_argument("--market", default="US", choices=["US", "HK"])
    imp.add_argument("--source", default="user_import")
    imp.set_defaults(func=cmd_import)

    reviews = sub.add_parser("import-news-reviews", help="import cited full-text or official-source event reviews", parents=[root_parent])
    reviews.add_argument("--file", required=True)
    reviews.set_defaults(func=cmd_import_news_reviews)

    news_workflow = sub.add_parser("news-workflow", help="run the durable per-headline search/LLM review workflow", parents=[root_parent])
    news_workflow.add_argument("--step", choices=["prepare", "next", "apply", "status", "finalize"], default="prepare")
    news_workflow.add_argument("--ticker", default="NVDA")
    news_workflow.add_argument("--market", choices=["US", "HK"], default="US")
    news_workflow.add_argument("--workflow-id", default="news-nvda-20261108")
    news_workflow.add_argument("--limit", type=int, default=8)
    news_workflow.add_argument("--file")
    news_workflow.set_defaults(func=cmd_news_workflow)

    collect = sub.add_parser("news-collect", help="collect every title through Search/Fetch before any LLM analysis", parents=[root_parent])
    collect.add_argument("--workflow-id", required=True)
    collect.add_argument("--collection-id", required=True)
    collect.add_argument("--provider", choices=["tinyfish", "tinyfish-plugin", "archive"], default="tinyfish")
    collect.add_argument("--plugin-batch-id")
    collect.add_argument("--max-sources", type=int, default=2)
    collect.set_defaults(func=cmd_news_collect)

    plugin_plan = sub.add_parser("news-plugin-plan", help="export a deterministic title plan for the connected TinyFish batch runner", parents=[root_parent])
    plugin_plan.add_argument("--workflow-id", required=True)
    plugin_plan.add_argument("--batch-id", required=True)
    plugin_plan.set_defaults(func=cmd_news_plugin_plan)

    reproduce = sub.add_parser("reproduce", help="print a frozen run report", parents=[root_parent])
    reproduce.add_argument("--run-id", required=True)
    reproduce.set_defaults(func=cmd_reproduce)

    doctor = sub.add_parser("doctor", help="show data registry state without credentials", parents=[root_parent])
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
