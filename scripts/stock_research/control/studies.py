"""Dataset construction and historical study execution."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..common.errors import DataError
from ..common.jsonio import write_json
from ..common.timeutil import now_utc, parse_date, parse_dt
from ..config import load_policies
from ..contracts import ExperimentProtocol, Market, ResearchRequest, Target, to_dict
from ..datasets.builder import build_dataset
from ..datasets.splits import PartitionSpec, annotate_partitions, partition_summary
from ..models.registry import ModelRegistry
from ..models.training import train_binary_model
from ..storage.curated_store import CuratedStore
from .orchestration import load_inputs


def protocol_from_dict(data: dict[str, Any]) -> ExperimentProtocol:
    return ExperimentProtocol(
        name=str(data.get("name", "study")),
        market=str(data.get("market", "US")),
        horizons=tuple(data.get("horizons") or (5,)),
        targets=tuple(data.get("targets") or ("absolute",)),
        factors=tuple(data.get("factors") or ()),
        train_start=parse_date(data["train_start"]),
        train_end=parse_date(data["train_end"]),
        dev_start=parse_date(data["dev_start"]),
        dev_end=parse_date(data["dev_end"]),
        cal_start=parse_date(data["cal_start"]),
        cal_end=parse_date(data["cal_end"]),
        test_start=parse_date(data["test_start"]),
        test_end=parse_date(data["test_end"]),
        candidate_models=tuple(data.get("candidate_models") or ("logistic_l2",)),
        l2_grid=tuple(float(x) for x in data.get("l2_grid") or (0.01, 0.1, 1.0)),
        seed=int(data.get("seed", 20261106)),
        registered_at=parse_dt(data["registered_at"]) if data.get("registered_at") else now_utc(),
        code_version=str(data.get("code_version", "")),
        notes=str(data.get("notes", "")),
    )


def build_dataset_from_store(
    market: Market | str,
    tickers: list[str],
    benchmark: str | None,
    start: date | str,
    end: date | str,
    horizons: list[int],
    targets: list[Target | str],
    factors: list[str],
    protocol: ExperimentProtocol,
    runtime_root: str | Path,
    skill_root: str | Path | None = None,
) -> dict[str, Any]:
    market = Market(market)
    start, end = parse_date(start), parse_date(end)
    request = ResearchRequest(
        market=market,
        tickers=tickers,
        benchmark=benchmark,
        cutoff=datetime.combine(end, datetime.max.time()),
        horizons=tuple(horizons),
        targets=tuple(Target(t) for t in targets),
    )
    inputs = load_inputs(request, runtime_root=runtime_root, skill_root=skill_root)
    if inputs.calendar is None:
        raise DataError("cannot build a dataset without a validated trading calendar")
    cutoffs = inputs.calendar.sessions(start, end)
    identities = []
    for ticker in tickers:
        identity = inputs.securities.resolve(market, ticker, end)
        identities.append(identity)
    price_map = {i.security_id: inputs.price_map[i.security_id] for i in identities if i.security_id in inputs.price_map}
    frame, manifest = build_dataset(
        identities,
        price_map,
        inputs.ledger,
        inputs.benchmark,
        inputs.benchmark_ledger,
        inputs.calendar,
        cutoffs,
        horizons,
        targets,
        factors,
        inputs.financials,
        inputs.macro,
        inputs.policies.defaults.get("price", {}),
        label_config=None,
        macro_series_map=inputs.policies.defaults.get("macro_series_map"),
        security_metadata=inputs.store.load("security_metadata", inputs.snapshot["datasets"]["security_metadata"]["version"]) if "security_metadata" in inputs.snapshot["datasets"] else None,
    )
    spec = PartitionSpec(
        train_start=protocol.train_start,
        train_end=protocol.train_end,
        dev_start=protocol.dev_start,
        dev_end=protocol.dev_end,
        cal_start=protocol.cal_start,
        cal_end=protocol.cal_end,
        test_start=protocol.test_start,
        test_end=protocol.test_end,
    )
    if not frame.empty:
        frame = annotate_partitions(frame, spec)
    store = CuratedStore(Path(runtime_root))
    version = store.save(
        "features_labels",
        frame,
        metadata={
            "manifest": to_dict(manifest),
            "protocol": to_dict(protocol),
            "partitions": partition_summary(frame),
            "factors": factors,
        },
        source_manifests=[],
    )
    return {
        "dataset": "features_labels",
        "version": version,
        "rows": int(len(frame)),
        "partitions": partition_summary(frame),
        "protocol": to_dict(protocol),
    }


def train_from_dataset(
    dataset_version: str | None,
    market: Market | str,
    horizon: int,
    target: Target | str,
    factors: list[str],
    protocol: ExperimentProtocol,
    runtime_root: str | Path,
    skill_root: str | Path | None = None,
    activate: bool = False,
) -> dict[str, Any]:
    market = Market(market)
    target = Target(target)
    policies = load_policies(skill_root)
    runtime = Path(runtime_root)
    store = CuratedStore(runtime)
    frame = store.load("features_labels", dataset_version)
    subset = frame[(frame["horizon"].astype(int) == int(horizon)) & (frame["target"].astype(str) == target.value)].copy()
    if subset.empty:
        raise DataError(f"dataset has no rows for horizon={horizon} target={target.value}")
    if "partition" not in subset.columns:
        spec = PartitionSpec(
            train_start=protocol.train_start,
            train_end=protocol.train_end,
            dev_start=protocol.dev_start,
            dev_end=protocol.dev_end,
            cal_start=protocol.cal_start,
            cal_end=protocol.cal_end,
            test_start=protocol.test_start,
            test_end=protocol.test_end,
        )
        subset = annotate_partitions(subset, spec)
    publication_policy = {
        **policies.defaults.get("price", {}),
        **policies.defaults.get("publication", {}),
        **policies.defaults.get("quantile", {}),
    }
    registry = ModelRegistry(runtime, store.catalog)
    if target is Target.QUANTILE:
        from ..models.quantiles import train_quantile_artifact

        q_result = train_quantile_artifact(
            subset,
            market,
            horizon,
            factors,
            protocol,
            publication_policy,
            registry,
            "features_labels",
            dataset_version or store.latest_version("features_labels") or "",
        )
        experiment_id = "qexp-" + q_result["artifact"].artifact_id
        experiment = {
            "experiment_id": experiment_id,
            "protocol": to_dict(protocol),
            "model": q_result["artifact"].to_dict(),
            "evidence": to_dict(q_result["evidence"]),
            "metrics": q_result["metrics"],
            "skill_ci": q_result["skill_ci"],
        }
        exp_dir = runtime / "experiments"
        exp_dir.mkdir(parents=True, exist_ok=True)
        exp_path = exp_dir / f"{experiment_id}.json"
        write_json(exp_path, experiment)
        store.catalog.register_experiment(experiment_id, exp_path, {"market": market.value, "horizon": horizon, "target": target.value})
        activated = False
        if activate:
            registry.activate_quantile(q_result["artifact"].artifact_id, q_result["evidence"])
            activated = True
        return {
            "experiment_id": experiment_id,
            "model_id": q_result["artifact"].artifact_id,
            "evidence_id": q_result["evidence"].evidence_id,
            "evidence_status": q_result["evidence"].status.value,
            "evidence_notes": q_result["evidence"].notes,
            "metrics": q_result["metrics"],
            "skill": q_result["skill_ci"],
            "activated": activated,
            "experiment_path": str(exp_path),
        }
    result = train_binary_model(
        subset,
        market,
        horizon,
        target,
        factors,
        protocol,
        publication_policy,
        registry,
        "features_labels",
        dataset_version or store.latest_version("features_labels") or "",
        code_version="skill-0.1.0",
    )
    experiment_id = "exp-" + result["artifact"].model_id
    experiment = {
        "experiment_id": experiment_id,
        "protocol": to_dict(protocol),
        "model": to_dict(result["artifact"]),
        "evidence": to_dict(result["evidence"]),
        "evaluation": result["evaluation"],
        "candidates": result["candidates"],
        "model_path": result["model_path"],
        "evidence_path": result["evidence_path"],
    }
    exp_dir = runtime / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    exp_path = exp_dir / f"{experiment_id}.json"
    write_json(exp_path, experiment)
    store.catalog.register_experiment(experiment_id, exp_path, {"market": market.value, "horizon": horizon, "target": target.value})
    activated = False
    if activate:
        registry.activate(result["artifact"].model_id, result["evidence"])
        activated = True
    return {
        "experiment_id": experiment_id,
        "model_id": result["artifact"].model_id,
        "evidence_id": result["evidence"].evidence_id,
        "evidence_status": result["evidence"].status.value,
        "evidence_notes": result["evidence"].notes,
        "metrics": result["evaluation"].get("metrics", {}),
        "skill": result["evaluation"].get("skill", {}),
        "activated": activated,
        "model_path": result["model_path"],
        "evidence_path": result["evidence_path"],
        "experiment_path": str(exp_path),
        "candidates": result["candidates"],
    }
