"""Training pipeline: train-only transforms, dev selection, independent calibration, test evidence."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash
from ..common.timeutil import now_utc
from ..contracts import ExperimentProtocol, Market, ModelArtifact, Target
from ..validation.evaluation import evaluate_binary, make_evidence
from .baseline import BaseRate
from .calibrate import PlattCalibrator
from .logistic import LogisticL2
from .preprocessing import FeatureTransformer
from .registry import ModelRegistry


def target_column(target: Target | str) -> str:
    return "label_excess" if Target(target) is Target.EXCESS else "label"


def _prepare(frame: pd.DataFrame, target: Target, factors: list[str]) -> pd.DataFrame:
    label_col = target_column(target)
    if label_col not in frame.columns:
        raise ValueError(f"dataset has no {label_col}")
    data = frame.copy()
    data = data[data["label_status"] == "ok"] if "label_status" in data else data
    data = data[data[label_col].isin([0, 1])]
    if "matured_at" in data.columns:
        matured = pd.to_datetime(data["matured_at"], errors="coerce", utc=True)
        data = data[matured.notna() & (matured <= pd.Timestamp.now(tz="UTC"))]
    for name in factors:
        if f"f__{name}" not in data.columns:
            raise ValueError(f"dataset has no feature column f__{name}")
    return data


def _date_weights(dates: pd.Series) -> np.ndarray:
    counts = dates.astype(str).value_counts()
    return dates.astype(str).map(lambda d: 1.0 / counts[d]).to_numpy(dtype=float)


def train_binary_model(
    frame: pd.DataFrame,
    market: Market | str,
    horizon: int,
    target: Target | str,
    factors: list[str],
    protocol: ExperimentProtocol,
    publication_policy: dict[str, Any],
    registry: ModelRegistry,
    dataset_id: str,
    dataset_version: str,
    code_version: str = "",
) -> dict[str, Any]:
    market = Market(market)
    target = Target(target)
    if "partition" not in frame.columns:
        raise ValueError("dataset must carry partition assignments")
    data = _prepare(frame, target, factors)
    label_col = target_column(target)
    blocks = {name: data[data["partition"] == name] for name in ("train", "dev", "cal", "test")}
    if blocks["train"].empty or blocks["dev"].empty or blocks["cal"].empty or blocks["test"].empty:
        raise ValueError(f"empty partition(s): { {k: len(v) for k, v in blocks.items()} }")

    transformer = FeatureTransformer().fit(
        blocks["train"],
        factors,
        winsor_quantiles=tuple(publication_policy.get("winsor_quantiles", (0.01, 0.99))),
        clip=float(publication_policy.get("mad_clip", 3.0)),
    )
    X_train = transformer.transform(blocks["train"])
    y_train = blocks["train"][label_col].astype(float).to_numpy()
    w_train = _date_weights(blocks["train"]["cutoff_date"])
    X_dev = transformer.transform(blocks["dev"])
    y_dev = blocks["dev"][label_col].astype(float).to_numpy()

    candidates = []
    for l2 in protocol.l2_grid:
        model = LogisticL2(l2=float(l2)).fit(X_train, y_train, sample_weight=w_train)
        p_dev = model.predict_proba(X_dev)
        if len(y_dev):
            brier_dev = float(np.mean((p_dev - y_dev) ** 2))
        else:
            brier_dev = float("inf")
        candidates.append({"l2": float(l2), "brier_dev": brier_dev, "converged": model.converged_})
    best = min(candidates, key=lambda c: (c["brier_dev"], c["l2"]))

    # Refit on train + dev after the hyperparameter is chosen; calibration stays on cal.
    fit_frame = pd.concat([blocks["train"], blocks["dev"]], ignore_index=True)
    transformer = FeatureTransformer().fit(
        fit_frame,
        factors,
        winsor_quantiles=tuple(publication_policy.get("winsor_quantiles", (0.01, 0.99))),
        clip=float(publication_policy.get("mad_clip", 3.0)),
    )
    X_fit = transformer.transform(fit_frame)
    y_fit = fit_frame[label_col].astype(float).to_numpy()
    w_fit = _date_weights(fit_frame["cutoff_date"])
    model = LogisticL2(l2=best["l2"]).fit(X_fit, y_fit, sample_weight=w_fit)
    baseline = BaseRate.fit(y_fit)

    p_cal_raw = model.predict_proba(transformer.transform(blocks["cal"]))
    y_cal = blocks["cal"][label_col].astype(float).to_numpy()
    calibrator = PlattCalibrator().fit(p_cal_raw, y_cal) if len(y_cal) >= 2 else PlattCalibrator()

    test = blocks["test"].copy()
    p_test_raw = model.predict_proba(transformer.transform(test))
    p_test = calibrator.predict(p_test_raw)
    evaluation_frame = pd.DataFrame(
        {
            "security_id": test["security_id"].astype(str).to_numpy() if "security_id" in test else "",
            "cutoff_date": test["cutoff_date"].astype(str).to_numpy(),
            "label": test[label_col].astype(float).to_numpy(),
            "p": p_test,
            "base_p": baseline.rate,
            "partition": "test",
        }
    )
    evaluation = evaluate_binary(
        evaluation_frame,
        p_col="p",
        y_col="label",
        date_col="cutoff_date",
        base_col="base_p",
        bootstrap_resamples=int(publication_policy.get("bootstrap_resamples", 500)),
        block_days=int(publication_policy.get("block_days", 5)),
        seed=protocol.seed,
    )
    model_id = "m-" + canonical_hash(
        {
            "market": market.value,
            "horizon": int(horizon),
            "target": target.value,
            "factors": factors,
            "l2": best["l2"],
            "train_end": str(protocol.train_end),
            "dataset_version": dataset_version,
            "seed": protocol.seed,
        }
    )[:16]
    artifact = ModelArtifact(
        model_id=model_id,
        market=market,
        horizon=int(horizon),
        target=target,
        status="candidate",
        factor_names=transformer.output_names,
        transformer=transformer.to_dict(),
        coefficients=model.coefficients_.tolist(),
        intercept=model.intercept_,
        baseline_rate=baseline.rate,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        created_at=now_utc(),
        code_version=code_version,
        seed=protocol.seed,
        metadata={
            "model_type": "logistic_l2",
            "l2": best["l2"],
            "selected_on_dev": candidates,
            "date_weighting": "each date total weight 1",
            "calibration_block": "cal",
            "test_block": "test",
            "train_rows": int(len(blocks["train"])),
            "dev_rows": int(len(blocks["dev"])),
            "cal_rows": int(len(blocks["cal"])),
            "test_rows": int(len(blocks["test"])),
        },
        calibrator=calibrator.to_dict(),
    )
    evidence = make_evidence(model_id, market, horizon, target, evaluation, publication_policy)
    artifact.evidence_id = evidence.evidence_id
    evidence_path = registry.save_evidence(evidence)
    model_path = registry.save(artifact)
    return {
        "artifact": artifact,
        "evidence": evidence,
        "evaluation": evaluation,
        "model_path": str(model_path),
        "evidence_path": str(evidence_path),
        "candidates": candidates,
    }
