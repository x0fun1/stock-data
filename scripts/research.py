#!/usr/bin/env python
"""Command line entry for the US/HK research skill."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from stock_research.config import default_runtime_root, default_skill_root, load_policies
from stock_research.contracts import ResearchRequest, Target, to_dict
from stock_research.control.maintenance import import_file
from stock_research.control.orchestration import run_research
from stock_research.control.studies import build_dataset_from_store, protocol_from_dict, train_from_dataset
from stock_research.storage.curated_store import CuratedStore
from stock_research.storage.catalog import Catalog
from stock_research.models.registry import ModelRegistry
from stock_research.control.kronos_baseline import run_file_baseline


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


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
        benchmark=args.benchmark,
        cutoff=cutoff,
        horizons=tuple(int(x) for x in _split_list(args.horizons)) or (1, 5, 20, 60),
        targets=tuple(Target(x) for x in (_split_list(args.targets) or ["absolute", "excess"])),
        news_lookback_days=args.news_lookback,
        output_format="json" if args.json else "markdown",
        quant_baseline=not args.no_quant_baseline,
        multifactor=not args.no_multifactor,
        universe=_split_list(args.universe),
    )


def cmd_research(args) -> int:
    request = _request_from_args(args)
    if args.no_quant_baseline:
        request.quant_baseline = False
    if args.no_multifactor:
        request.multifactor = False
    runtime = args.root or default_runtime_root()
    bundle, markdown = run_research(request, runtime_root=runtime)
    if args.json:
        print(json.dumps({"summary": {"run_id": bundle.run_manifest.run_id, "results": to_dict(bundle.results), "decisions": to_dict(bundle.decisions), "baseline_forecasts": bundle.baseline_forecasts, "multifactor_snapshot": bundle.multifactor_snapshot, "limitations": bundle.limitations}, "report": markdown}, ensure_ascii=False, indent=2))
    else:
        print(markdown)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
    return 0


def cmd_quant_baseline(args) -> int:
    try:
        result, report = run_file_baseline(args.file, ticker=args.ticker, frequency=args.frequency,
            cutoff=args.cutoff, target_start=args.target_start, runtime_root=args.root or default_runtime_root())
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else report)
    if args.out:
        Path(args.out).write_text(report, encoding='utf-8')
    return 0 if result.get('status') == 'ok' else 2


def cmd_import(args) -> int:
    result = import_file(
        args.kind,
        args.file,
        args.market,
        args.root or default_runtime_root(),
        ticker=args.ticker,
        security_id=args.security_id,
        source_id=args.source,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_import_factors(args) -> int:
    from stock_research.multifactor.factor_returns import import_factor_archives
    result = import_factor_archives(args.ff3, args.momentum, args.root or default_runtime_root())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_multifactor_study(args) -> int:
    from stock_research.multifactor.study import normalize_dataset_admission, run_multifactor_study
    with open(args.protocol, encoding="utf-8") as handle:
        protocol = json.load(handle)
    root = Path(args.root or default_runtime_root())
    store = CuratedStore(root)
    entry = store.metadata("features_labels", args.dataset)
    frame = store.load("features_labels", args.dataset)
    frame = frame[(frame["market"].astype(str) == args.market) &
                  (frame["horizon"].astype(int) == args.horizon) &
                  (frame["target"].astype(str) == args.target)].copy()
    admission = normalize_dataset_admission(protocol.get("dataset_admission"))
    if not args.development_only and not (admission["status"] == "verified_point_in_time" and admission["has_evidence_locator"]):
        raise SystemExit("multifactor-study requires verified dataset_admission with an evidence locator; use --development-only explicitly for unverified historical research")
    if args.development_only:
        protocol["dataset_admission"] = {"status": "research_development_unverified", "reason": "explicit_development_only"}
    else:
        protocol["dataset_admission"] = admission
    protocol["dataset_version"] = entry["version"]
    result = run_multifactor_study(frame, protocol, _split_list(args.factors) or protocol.get("factors", []),
        root / "multifactor-studies", candidates=_split_list(args.models) or None,
        neutralize=args.neutralize, min_universe=args.min_universe, seed=int(protocol.get("seed", 20261107)))
    print(json.dumps(to_dict(result), ensure_ascii=False, indent=2))
    return 0


def cmd_build_dataset(args) -> int:
    with open(args.protocol, "r", encoding="utf-8") as handle:
        protocol = protocol_from_dict(json.load(handle))
    result = build_dataset_from_store(
        market=args.market,
        tickers=_split_list(args.tickers),
        benchmark=args.benchmark,
        start=args.start,
        end=args.end,
        horizons=[int(x) for x in _split_list(args.horizons)] or [1, 5, 20, 60],
        targets=[Target(x) for x in (_split_list(args.targets) or ["absolute", "excess"])],
        factors=_split_list(args.factors),
        protocol=protocol,
        runtime_root=args.root or default_runtime_root(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_study(args) -> int:
    with open(args.protocol, "r", encoding="utf-8") as handle:
        protocol = protocol_from_dict(json.load(handle))
    result = train_from_dataset(
        dataset_version=args.dataset,
        market=args.market,
        horizon=args.horizon,
        target=args.target,
        factors=_split_list(args.factors),
        protocol=protocol,
        runtime_root=args.root or default_runtime_root(),
        activate=args.activate,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_reproduce(args) -> int:
    root = Path(args.root or default_runtime_root())
    catalog = Catalog(root)
    entry = catalog.get_run(args.run_id)
    if entry is None:
        raise SystemExit(f"run {args.run_id} is not in the catalog")
    path = Path(entry["path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    report_path = Path(data.get("run_manifest", {}).get("outputs", {}).get("markdown", ""))
    if report_path.is_file():
        print(report_path.read_text(encoding="utf-8"))
        return 0
    print(json.dumps(data.get("run_manifest", {}), ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args) -> int:
    root = Path(args.root or default_runtime_root())
    policies = load_policies(default_skill_root())
    store = CuratedStore(root)
    catalog = store.catalog
    datasets = {}
    for name in ("prices", "securities", "actions", "action_coverage", "financial_facts", "macro", "news", "security_metadata", "factor_returns", "features_labels"):
        entry = catalog.get_dataset(name)
        datasets[name] = None if entry is None else {"version": entry["version"], "rows": entry["rows"], "created_at": entry["created_at"]}
    models = catalog.list_models()
    print(json.dumps(
        {
            "skill_root": str(default_skill_root()),
            "runtime_root": str(root),
            "policy_version": policies.version,
            "policy_hash": policies.hash,
            "datasets": datasets,
            "models": [{"model_id": m["model_id"], "status": m["status"], **m["metadata"]} for m in models],
            "quant_baseline": {"model": "kronos-small", "role": "frozen_research_baseline",
                "enabled": policies.defaults.get('quant_baseline', {}).get('enabled', False),
                "package_manifest": str(default_skill_root() / 'model-assets/kronos-small/manifest.json'),
                "assets_present": (default_skill_root() / 'model-assets/kronos-small/manifest.json').is_file(),
                "probability_verified": False},
            "multifactor": {"enabled": policies.defaults.get("multifactor", {}).get("enabled", True),
                "version": "multifactor-1.0", "execution": "native_local_python",
                "models": ["group_equal_weight", "ridge", "lightgbm"], "risk": "FF3_plus_Momentum",
                "min_cross_section": policies.defaults.get("multifactor", {}).get("min_cross_section", 20),
                "probability_verified": False},
            "note": "No credentials are printed. Live acquisition requires a configured authorized source.",
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="US/HK evidence-first quantitative and news research skill")
    root_parent = argparse.ArgumentParser(add_help=False)
    root_parent.add_argument("--root", help="runtime data root (default: this skill's runtime directory)")
    parser.add_argument("--root", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    research = sub.add_parser("research", help="run a current research report", parents=[root_parent])
    research.add_argument("--request", help="request JSON file")
    research.add_argument("--market", default="US", choices=["US", "HK"])
    research.add_argument("--ticker", action="append", default=[])
    research.add_argument("--benchmark")
    research.add_argument("--cutoff")
    research.add_argument("--horizons", default="1,5,20,60")
    research.add_argument("--targets", default="absolute,excess")
    research.add_argument("--news-lookback", type=int, default=30)
    research.add_argument("--json", action="store_true")
    research.add_argument("--out")
    research.add_argument("--no-quant-baseline", action="store_true", help="skip the frozen Kronos baseline")
    research.add_argument("--no-multifactor", action="store_true", help="skip current native multifactor analysis")
    research.add_argument("--universe", help="comma-separated operating-company comparison universe; default registered equities")
    research.set_defaults(func=cmd_research)

    baseline = sub.add_parser('quant-baseline', help='run the bundled frozen Kronos model on local OHLCV', parents=[root_parent])
    baseline.add_argument('--file', required=True, help='authorized OHLCV CSV; hourly data keeps UTC start/end')
    baseline.add_argument('--ticker', required=True)
    baseline.add_argument('--frequency', choices=['1d', '1h'], default='1d')
    baseline.add_argument('--cutoff', help='explicit timezone timestamp; hourly cutoff is a whole-hour boundary')
    baseline.add_argument('--target-start', help='optional check of the next valid target start')
    baseline.add_argument('--json', action='store_true')
    baseline.add_argument('--out')
    baseline.set_defaults(func=cmd_quant_baseline)

    imp = sub.add_parser("import", help="import an authorized local CSV/JSON file", parents=[root_parent])
    imp.add_argument("--kind", required=True, choices=["prices", "actions", "coverage", "financial_facts", "macro", "news", "securities", "security_metadata"])
    imp.add_argument("--file", required=True)
    imp.add_argument("--market", default="US", choices=["US", "HK"])
    imp.add_argument("--ticker")
    imp.add_argument("--security-id")
    imp.add_argument("--source", default="user_import")
    imp.set_defaults(func=cmd_import)

    factor_import = sub.add_parser("import-factors", help="import local Ken French FF3+RF/Momentum daily archives for risk attribution", parents=[root_parent])
    factor_import.add_argument("--ff3", required=True)
    factor_import.add_argument("--momentum", required=True)
    factor_import.set_defaults(func=cmd_import_factors)

    mf_study = sub.add_parser("multifactor-study", help="compare native research scores on a fixed historical factor panel", parents=[root_parent])
    mf_study.add_argument("--dataset", help="registered features_labels version; default latest")
    mf_study.add_argument("--market", default="US", choices=["US", "HK"])
    mf_study.add_argument("--horizon", required=True, type=int)
    mf_study.add_argument("--target", default="excess", choices=["absolute", "excess"])
    mf_study.add_argument("--protocol", required=True)
    mf_study.add_argument("--factors")
    mf_study.add_argument("--models", help="equal_weight,ridge,lightgbm; defaults to all three")
    mf_study.add_argument("--neutralize", action="store_true")
    mf_study.add_argument("--min-universe", type=int, default=20)
    mf_study.add_argument("--development-only", action="store_true", help="explicitly permit unverified historical development evidence; never activates probabilities")
    mf_study.set_defaults(func=cmd_multifactor_study)

    build = sub.add_parser("build-dataset", help="build a point-in-time feature/label dataset", parents=[root_parent])
    build.add_argument("--market", default="US", choices=["US", "HK"])
    build.add_argument("--tickers", required=True)
    build.add_argument("--benchmark")
    build.add_argument("--start", required=True)
    build.add_argument("--end", required=True)
    build.add_argument("--horizons", default="1,5,20,60")
    build.add_argument("--targets", default="absolute,excess")
    build.add_argument("--factors", required=True)
    build.add_argument("--protocol", required=True)
    build.set_defaults(func=cmd_build_dataset)

    study = sub.add_parser("study", help="train and validate a model on a registered dataset", parents=[root_parent])
    study.add_argument("--dataset", help="dataset version; default latest")
    study.add_argument("--market", default="US", choices=["US", "HK"])
    study.add_argument("--horizon", type=int, required=True)
    study.add_argument("--target", default="absolute", choices=["absolute", "excess", "quantile"])
    study.add_argument("--factors", required=True)
    study.add_argument("--protocol", required=True)
    study.add_argument("--activate", action="store_true", help="activate only if evidence is verified")
    study.set_defaults(func=cmd_study)

    reproduce = sub.add_parser("reproduce", help="print a frozen run report", parents=[root_parent])
    reproduce.add_argument("--run-id", required=True)
    reproduce.set_defaults(func=cmd_reproduce)

    doctor = sub.add_parser("doctor", help="show data/model registry state without credentials", parents=[root_parent])
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
