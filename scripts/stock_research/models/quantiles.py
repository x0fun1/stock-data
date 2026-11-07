"""Conditional return quantile artifact and independent validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash
from ..common.timeutil import now_utc, parse_date
from ..contracts import Market, Target, ValidationEvidence, ValidationState, to_dict
from ..validation.metrics import date_block_bootstrap, interval_coverage, pinball
from .preprocessing import FeatureTransformer
from .quantile import QuantileLinear


@dataclass
class QuantileArtifact:
    artifact_id: str
    market: Market
    horizon: int
    status: str
    factor_names: list[str]
    transformer: dict[str, Any]
    quantiles: dict[str, dict[str, Any]]
    dataset_id: str
    dataset_version: str
    evidence_id: str = ""
    created_at: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.created_at = self.created_at or now_utc()

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuantileArtifact":
        return cls(
            artifact_id=str(data["artifact_id"]),
            market=str(data["market"]),
            horizon=int(data["horizon"]),
            status=str(data.get("status", "candidate")),
            factor_names=list(data["factor_names"]),
            transformer=dict(data["transformer"]),
            quantiles=dict(data["quantiles"]),
            dataset_id=str(data.get("dataset_id", "")),
            dataset_version=str(data.get("dataset_version", "")),
            evidence_id=str(data.get("evidence_id", "")),
            created_at=data.get("created_at"),
            metadata=dict(data.get("metadata") or {}),
        )


def predict_quantiles(artifact: QuantileArtifact, frame: pd.DataFrame) -> dict[str, Any]:
    transformer = FeatureTransformer.from_dict(artifact.transformer)
    X = transformer.transform(frame)
    values: dict[float, np.ndarray] = {}
    for key, model_data in artifact.quantiles.items():
        tau = float(model_data["tau"])
        coef = np.asarray(model_data["coefficients"], dtype=float)
        intercept = float(model_data["intercept"])
        values[tau] = X @ coef + intercept
    taus = sorted(values)
    if not taus:
        return {}
    matrix = np.column_stack([values[tau] for tau in taus])
    crossed = bool(np.any(np.diff(matrix, axis=1) < 0))
    matrix = np.sort(matrix, axis=1)
    keys = {0.1: "q10", 0.5: "q50", 0.9: "q90"}
    out: dict[str, Any] = {}
    for i, tau in enumerate(taus):
        out[keys.get(tau, f"q{int(tau * 100)}")] = float(matrix[0, i]) if len(matrix) else None
    out["crossed_before_rearrangement"] = crossed
    return out


def train_quantile_artifact(
    frame: pd.DataFrame,
    market: Market | str,
    horizon: int,
    factors: list[str],
    protocol,
    policy: dict[str, Any],
    registry,
    dataset_id: str,
    dataset_version: str,
    taus: tuple[float, ...] = (0.1, 0.5, 0.9),
    l2: float = 0.01,
    return_column: str = "return_value",
) -> dict[str, Any]:
    market = Market(market)
    if "partition" not in frame.columns:
        raise ValueError("dataset must carry partition assignments")
    data = frame.copy()
    data = data[data["label_status"] == "ok"] if "label_status" in data else data
    data = data[np.isfinite(pd.to_numeric(data[return_column], errors="coerce"))]
    if "matured_at" in data.columns:
        matured = pd.to_datetime(data["matured_at"], errors="coerce", utc=True)
        data = data[matured.notna() & (matured <= pd.Timestamp.now(tz="UTC"))]
    blocks = {name: data[data["partition"] == name] for name in ("train", "dev", "cal", "test")}
    if blocks["train"].empty or blocks["dev"].empty or blocks["test"].empty:
        raise ValueError(f"empty quantile partition(s): { {k: len(v) for k, v in blocks.items()} }")
    transformer = FeatureTransformer().fit(blocks["train"], factors)
    fit_frame = pd.concat([blocks["train"], blocks["dev"]], ignore_index=True)
    transformer = FeatureTransformer().fit(
        fit_frame,
        factors,
        winsor_quantiles=tuple(policy.get("winsor_quantiles", (0.01, 0.99))),
        clip=float(policy.get("mad_clip", 3.0)),
    )
    X_fit = transformer.transform(fit_frame)
    y_fit = fit_frame[return_column].astype(float).to_numpy()
    models: dict[str, Any] = {}
    for tau in taus:
        model = QuantileLinear(tau=float(tau), l2=l2).fit(X_fit, y_fit)
        models[str(float(tau))] = {
            "tau": float(tau),
            "coefficients": model.coefficients_.tolist(),
            "intercept": model.intercept_,
            "converged": model.converged_,
        }
    test = blocks["test"].copy()
    X_test = transformer.transform(test)
    y_test = test[return_column].astype(float).to_numpy()
    predictions: dict[float, np.ndarray] = {}
    for tau in taus:
        model = QuantileLinear(tau=float(tau), l2=l2)
        model.coefficients_ = np.asarray(models[str(float(tau))]["coefficients"], dtype=float)
        model.intercept_ = float(models[str(float(tau))]["intercept"])
        predictions[float(tau)] = model.predict(X_test)
    metrics = {"pinball": {}, "baseline_pinball": {}}
    skill_values = []
    for tau in taus:
        metrics["pinball"][str(tau)] = pinball(y_test, predictions[float(tau)], float(tau))
        baseline_value = float(np.quantile(y_fit, tau))
        metrics["baseline_pinball"][str(tau)] = pinball(y_test, np.full(len(y_test), baseline_value), float(tau))
    if 0.1 in predictions and 0.9 in predictions:
        metrics["interval_10_90"] = interval_coverage(y_test, predictions[0.1], predictions[0.9])
    dates = test["cutoff_date"].astype(str).to_numpy()
    daily_skills = []
    for tau in taus:
        base = float(np.quantile(y_fit, tau))
        model_loss = np.where(y_test - predictions[float(tau)] >= 0, tau * (y_test - predictions[float(tau)]), (tau - 1) * (y_test - predictions[float(tau)]))
        base_loss = np.where(y_test - base >= 0, tau * (y_test - base), (tau - 1) * (y_test - base))
        frame_loss = pd.DataFrame({"date": dates, "model": model_loss, "base": base_loss})
        daily = frame_loss.groupby("date").mean()
        daily = daily[daily["base"] > 0]
        if len(daily):
            daily_skills.append(1.0 - daily["model"] / daily["base"])
    skill_ci = {}
    if daily_skills:
        combined = pd.concat(daily_skills, axis=1).mean(axis=1)
        skill_ci = date_block_bootstrap(
            combined.index,
            combined.to_numpy(),
            n_resamples=int(policy.get("bootstrap_resamples", 500)),
            block_days=int(policy.get("block_days", 5)),
            seed=protocol.seed,
        )
    min_samples = int(policy.get("min_samples", 200))
    min_dates = int(policy.get("min_independent_dates", 60))
    coverage = metrics.get("interval_10_90", {}).get("coverage")
    coverage_range = policy.get("coverage_range", [0.7, 0.95])
    status = ValidationState.VERIFIED
    reason = "all_registered_checks_passed"
    if len(test) < min_samples or pd.Series(dates).nunique() < min_dates:
        status, reason = ValidationState.UNVERIFIED, "insufficient_out_of_sample_evidence"
    elif coverage is None or not (float(coverage_range[0]) <= coverage <= float(coverage_range[1])):
        status, reason = ValidationState.FAILED, f"coverage_outside_range:{coverage}"
    elif skill_ci.get("low") is None or skill_ci["low"] <= float(policy.get("min_pinball_skill_ci_low", 0.0)):
        status, reason = ValidationState.UNVERIFIED, "pinball_skill_ci_crosses_zero_or_missing"
    artifact_id = "q-" + canonical_hash(
        {
            "market": market.value,
            "horizon": int(horizon),
            "factors": factors,
            "taus": list(taus),
            "train_end": str(protocol.train_end),
            "dataset_version": dataset_version,
            "seed": protocol.seed,
        }
    )[:16]
    evidence_id = "qev-" + canonical_hash({"artifact_id": artifact_id, "metrics": metrics, "status": status.value})[:16]
    evidence = ValidationEvidence(
        evidence_id=evidence_id,
        model_id=artifact_id,
        market=market,
        horizon=int(horizon),
        target=Target.QUANTILE,
        status=status,
        sample_count=int(len(test)),
        independent_dates=int(pd.Series(dates).nunique()),
        date_start=parse_date(min(dates)) if len(dates) else None,
        date_end=parse_date(max(dates)) if len(dates) else None,
        metrics=metrics,
        calibration={"interval_10_90": metrics.get("interval_10_90")},
        skill_ci=skill_ci,
        stability={},
        notes=reason,
        created_at=now_utc(),
    )
    artifact = QuantileArtifact(
        artifact_id=artifact_id,
        market=market,
        horizon=int(horizon),
        status="candidate",
        factor_names=transformer.output_names,
        transformer=transformer.to_dict(),
        quantiles=models,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        evidence_id=evidence_id,
        created_at=now_utc(),
        metadata={"taus": list(taus), "l2": l2, "return_column": return_column, "test_rows": int(len(test))},
    )
    registry.save_evidence(evidence)
    registry.save_quantile(artifact)
    return {"artifact": artifact, "evidence": evidence, "metrics": metrics, "skill_ci": skill_ci}
