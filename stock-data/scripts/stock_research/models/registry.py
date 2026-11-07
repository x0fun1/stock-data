"""Model artifact registry with an explicit activation gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ..common.errors import DataError
from ..common.jsonio import read_json, write_json
from ..contracts import Market, ModelArtifact, Target, ValidationEvidence, ValidationState, to_dict
from ..storage.catalog import Catalog


def artifact_to_dict(artifact: ModelArtifact) -> dict[str, Any]:
    return to_dict(artifact)


def artifact_from_dict(data: dict[str, Any]) -> ModelArtifact:
    return ModelArtifact(
        model_id=str(data["model_id"]),
        market=str(data["market"]),
        horizon=int(data["horizon"]),
        target=Target(data["target"]),
        status=str(data.get("status", "candidate")),
        factor_names=list(data["factor_names"]),
        transformer=dict(data.get("transformer") or {}),
        coefficients=[float(x) for x in data["coefficients"]],
        intercept=float(data["intercept"]),
        baseline_rate=float(data.get("baseline_rate", 0.5)),
        dataset_id=str(data.get("dataset_id", "")),
        dataset_version=str(data.get("dataset_version", "")),
        evidence_id=str(data.get("evidence_id", "")),
        calibrator=dict(data["calibrator"]) if data.get("calibrator") else None,
        created_at=data.get("created_at"),
        code_version=str(data.get("code_version", "")),
        seed=int(data.get("seed", 0)),
        metadata=dict(data.get("metadata") or {}),
    )


class ModelRegistry:
    def __init__(self, root: str | Path, catalog: Catalog | None = None) -> None:
        self.root = Path(root)
        self.models_dir = self.root / "models"
        self.evidence_dir = self.root / "validation"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = catalog or Catalog(self.root)

    def save_evidence(self, evidence: ValidationEvidence) -> Path:
        path = self.evidence_dir / f"{evidence.evidence_id}.json"
        write_json(path, to_dict(evidence))
        return path

    def load_evidence(self, evidence_id: str) -> ValidationEvidence | None:
        path = self.evidence_dir / f"{evidence_id}.json"
        if not path.exists():
            return None
        data = read_json(path)
        return ValidationEvidence(
            evidence_id=data["evidence_id"],
            model_id=data["model_id"],
            market=data["market"],
            horizon=int(data["horizon"]),
            target=data["target"],
            status=ValidationState(data["status"]),
            sample_count=int(data["sample_count"]),
            independent_dates=int(data["independent_dates"]),
            date_start=data.get("date_start"),
            date_end=data.get("date_end"),
            metrics=dict(data.get("metrics") or {}),
            calibration=dict(data.get("calibration") or {}),
            skill_ci=dict(data.get("skill_ci") or {}),
            stability=dict(data.get("stability") or {}),
            notes=str(data.get("notes", "")),
            created_at=data.get("created_at"),
        )

    def save(self, artifact: ModelArtifact) -> Path:
        path = self.models_dir / f"{artifact.model_id}.json"
        write_json(path, artifact_to_dict(artifact))
        self.catalog.register_model(
            artifact.model_id,
            path,
            artifact.status,
            {
                "market": artifact.market.value,
                "horizon": artifact.horizon,
                "target": artifact.target.value,
                "evidence_id": artifact.evidence_id,
                "dataset_version": artifact.dataset_version,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
            },
        )
        return path

    def load(self, model_id: str) -> ModelArtifact:
        path = self.models_dir / f"{model_id}.json"
        if not path.exists():
            entry = next((m for m in self.catalog.list_models() if m["model_id"] == model_id), None)
            if entry:
                path = Path(entry["path"])
        if not path.exists():
            raise DataError(f"model artifact not found: {model_id}")
        return artifact_from_dict(read_json(path))

    def save_quantile(self, artifact) -> Path:
        from .quantiles import QuantileArtifact

        path = self.models_dir / f"{artifact.artifact_id}.json"
        write_json(path, artifact.to_dict())
        self.catalog.register_model(
            artifact.artifact_id,
            path,
            artifact.status,
            {
                "market": artifact.market.value,
                "horizon": artifact.horizon,
                "target": Target.QUANTILE.value,
                "model_type": "quantile",
                "evidence_id": artifact.evidence_id,
                "dataset_version": artifact.dataset_version,
            },
        )
        return path

    def load_quantile(self, artifact_id: str):
        from .quantiles import QuantileArtifact

        path = self.models_dir / f"{artifact_id}.json"
        if not path.exists():
            entry = next((m for m in self.catalog.list_models() if m["model_id"] == artifact_id), None)
            if entry:
                path = Path(entry["path"])
        if not path.exists():
            raise DataError(f"quantile artifact not found: {artifact_id}")
        return QuantileArtifact.from_dict(read_json(path))

    def load_active_quantile(self, market: str, horizon: int):
        for entry in self.catalog.list_models("active"):
            meta = entry.get("metadata", {})
            if meta.get("model_type") == "quantile" and meta.get("market") == Market(market).value and int(meta.get("horizon", -1)) == int(horizon):
                return self.load_quantile(entry["model_id"])
        return None

    def activate_quantile(self, artifact_id: str, evidence: ValidationEvidence):
        artifact = self.load_quantile(artifact_id)
        if evidence.status is not ValidationState.VERIFIED:
            raise DataError(f"quantile {artifact_id} cannot be activated: evidence status is {evidence.status.value}")
        artifact.status = "active"
        artifact.evidence_id = evidence.evidence_id
        path = self.models_dir / f"{artifact_id}.json"
        write_json(path, artifact.to_dict())
        self.catalog.update_model_status(
            artifact_id,
            "active",
            {
                "market": artifact.market.value,
                "horizon": artifact.horizon,
                "target": Target.QUANTILE.value,
                "model_type": "quantile",
                "evidence_id": artifact.evidence_id,
                "dataset_version": artifact.dataset_version,
            },
        )
        return artifact

    def load_active(self, market: str, horizon: int, target: Target | str) -> ModelArtifact | None:
        target = Target(target)
        for entry in self.catalog.list_models("active"):
            meta = entry.get("metadata", {})
            if meta.get("market") == Market(market).value and int(meta.get("horizon", -1)) == int(horizon) and meta.get("target") == target.value:
                return self.load(entry["model_id"])
        return None

    def activate(self, model_id: str, evidence: ValidationEvidence) -> ModelArtifact:
        artifact = self.load(model_id)
        if evidence.status is not ValidationState.VERIFIED:
            raise DataError(f"model {model_id} cannot be activated: evidence status is {evidence.status.value}")
        if evidence.model_id != model_id:
            raise DataError("evidence belongs to a different model")
        artifact.status = "active"
        artifact.evidence_id = evidence.evidence_id
        path = self.models_dir / f"{model_id}.json"
        write_json(path, artifact_to_dict(artifact))
        self.catalog.update_model_status(
            model_id,
            "active",
            {
                "market": artifact.market.value,
                "horizon": artifact.horizon,
                "target": artifact.target.value,
                "evidence_id": artifact.evidence_id,
                "dataset_version": artifact.dataset_version,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
            },
        )
        return artifact

    def suspend(self, model_id: str, reason: str = "") -> None:
        artifact = self.load(model_id)
        artifact.status = "suspended"
        artifact.metadata["suspension_reason"] = reason
        write_json(self.models_dir / f"{model_id}.json", artifact_to_dict(artifact))
        self.catalog.register_model(model_id, self.models_dir / f"{model_id}.json", "suspended", {"reason": reason})


def predict_proba(artifact: ModelArtifact, frame) -> np.ndarray:
    from .preprocessing import FeatureTransformer

    transformer = FeatureTransformer.from_dict(artifact.transformer)
    X = transformer.transform(frame)
    coef = np.asarray(artifact.coefficients, dtype=float)
    if X.shape[1] != len(coef):
        raise DataError(f"model expects {len(coef)} transformed columns, got {X.shape[1]}")
    p = 1.0 / (1.0 + np.exp(-np.clip(X @ coef + artifact.intercept, -60.0, 60.0)))
    if artifact.calibrator:
        from .calibrate import PlattCalibrator

        p = PlattCalibrator.from_dict(artifact.calibrator).predict(p)
    return p
