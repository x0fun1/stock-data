"""Out-of-sample evaluation and validation-evidence construction."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash
from ..common.timeutil import now_utc
from ..contracts import Market, Target, ValidationEvidence, ValidationState
from .metrics import (
    auc,
    brier,
    calibration_fit,
    calibration_table,
    date_block_bootstrap,
    ece,
    interval_coverage,
    log_loss,
    pinball,
)


def _finite(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    for column in columns:
        mask &= np.isfinite(pd.to_numeric(frame[column], errors="coerce"))
    return frame[mask].copy()


def evaluate_binary(
    frame: pd.DataFrame,
    p_col: str = "p",
    y_col: str = "label",
    date_col: str = "cutoff_date",
    base_col: str | None = "base_p",
    n_bins: int = 10,
    bootstrap_resamples: int = 500,
    block_days: int = 5,
    seed: int = 20261106,
) -> dict[str, Any]:
    required = [p_col, y_col, date_col]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"missing evaluation columns: {missing}")
    data = _finite(frame, [p_col, y_col] + ([base_col] if base_col and base_col in frame.columns else []))
    data = data[data[y_col].isin([0, 1])]
    if data.empty:
        return {"status": "empty", "sample_count": 0, "independent_dates": 0}
    y = data[y_col].astype(float).to_numpy()
    p = data[p_col].astype(float).to_numpy()
    dates = pd.to_datetime(data[date_col]).dt.date.to_numpy()
    metrics = {
        "brier": brier(y, p),
        "log_loss": log_loss(y, p),
        "auc": auc(y, p),
        "ece": ece(y, p, n_bins=n_bins),
    }
    calib = {
        "fit": calibration_fit(y, p),
        "bins": calibration_table(y, p, n_bins=n_bins),
    }
    skill = {"value": None, "ci": None}
    per_date_skill = None
    if base_col and base_col in data.columns:
        base_p = data[base_col].astype(float).to_numpy()
        bs_model = float(np.mean((p - y) ** 2))
        bs_base = float(np.mean((base_p - y) ** 2))
        skill["value"] = 1.0 - bs_model / bs_base if bs_base > 0 else None
        if bs_base > 0:
            tmp = pd.DataFrame({
                "date": dates,
                "model": (p - y) ** 2,
                "base": (base_p - y) ** 2,
            })
            daily = tmp.groupby("date").mean()
            daily = daily[daily["base"] > 0]
            daily_skill = 1.0 - daily["model"] / daily["base"]
            per_date_skill = daily_skill
            if len(daily_skill) >= 2:
                skill["ci"] = date_block_bootstrap(
                    daily.index,
                    daily_skill.to_numpy(),
                    n_resamples=bootstrap_resamples,
                    block_days=block_days,
                    seed=seed,
                )
    stability: dict[str, Any] = {"by_year": {}, "by_partition": {}}
    year_series = pd.to_datetime(data[date_col]).dt.year
    for year, group in data.groupby(year_series):
        gy = group[y_col].astype(float).to_numpy()
        gp = group[p_col].astype(float).to_numpy()
        stability["by_year"][str(int(year))] = {
            "n": int(len(group)),
            "brier": brier(gy, gp),
            "auc": auc(gy, gp),
        }
    if "partition" in data.columns:
        for partition, group in data.groupby("partition"):
            gy = group[y_col].astype(float).to_numpy()
            gp = group[p_col].astype(float).to_numpy()
            stability["by_partition"][str(partition)] = {
                "n": int(len(group)),
                "brier": brier(gy, gp),
                "auc": auc(gy, gp),
            }
    years = sorted(stability["by_year"])
    if years:
        worst = max(stability["by_year"].items(), key=lambda kv: (kv[1]["brier"] if np.isfinite(kv[1]["brier"]) else -1))
        stability["worst_year"] = {"year": worst[0], **worst[1]}
    return {
        "status": "ok",
        "sample_count": int(len(data)),
        "independent_dates": int(pd.Series(dates).nunique()),
        "security_count": int(data["security_id"].nunique()) if "security_id" in data else None,
        "date_start": min(dates).isoformat() if len(dates) else None,
        "date_end": max(dates).isoformat() if len(dates) else None,
        "metrics": metrics,
        "calibration": calib,
        "skill": skill,
        "stability": stability,
    }


def evaluate_quantiles(
    frame: pd.DataFrame,
    y_col: str = "return_value",
    quantile_columns: dict[float, str] | None = None,
) -> dict[str, Any]:
    quantile_columns = quantile_columns or {0.1: "q10", 0.5: "q50", 0.9: "q90"}
    required = [y_col] + list(quantile_columns.values())
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"missing quantile columns: {missing}")
    data = _finite(frame, required)
    if data.empty:
        return {"status": "empty", "n": 0}
    out = {"status": "ok", "n": int(len(data)), "pinball": {}}
    for tau, column in quantile_columns.items():
        out["pinball"][str(tau)] = pinball(data[y_col].to_numpy(), data[column].to_numpy(), tau)
    low_col, high_col = quantile_columns[0.1], quantile_columns[0.9]
    out["interval_10_90"] = interval_coverage(data[y_col].to_numpy(), data[low_col].to_numpy(), data[high_col].to_numpy())
    return out


def classify_evidence(evaluation: dict[str, Any], policy: dict[str, Any]) -> tuple[ValidationState, str]:
    if evaluation.get("status") != "ok":
        return ValidationState.UNVERIFIED, "no_out_of_sample_evaluation"
    min_samples = int(policy.get("min_samples", 200))
    min_dates = int(policy.get("min_independent_dates", 60))
    if evaluation["sample_count"] < min_samples or evaluation["independent_dates"] < min_dates:
        return ValidationState.UNVERIFIED, f"insufficient_out_of_sample_evidence:samples={evaluation['sample_count']},dates={evaluation['independent_dates']}"
    skill_ci = evaluation.get("skill", {}).get("ci") or {}
    low = skill_ci.get("low")
    if low is None or low <= float(policy.get("min_brier_skill_ci_low", 0.0)):
        return ValidationState.UNVERIFIED, "brier_skill_ci_crosses_zero_or_missing"
    fit = evaluation.get("calibration", {}).get("fit", {})
    slope = fit.get("slope")
    bounds = policy.get("calibration_slope", [0.5, 1.5])
    if slope is None or not (float(bounds[0]) <= slope <= float(bounds[1])):
        return ValidationState.FAILED, f"calibration_slope_outside_bounds:{slope}"
    if evaluation["metrics"].get("ece") is None or evaluation["metrics"]["ece"] > float(policy.get("max_ece", 0.1)):
        return ValidationState.FAILED, "ece_above_limit"
    return ValidationState.VERIFIED, "all_registered_checks_passed"


def make_evidence(
    model_id: str,
    market: Market | str,
    horizon: int,
    target: Target | str,
    evaluation: dict[str, Any],
    policy: dict[str, Any],
) -> ValidationEvidence:
    status, reason = classify_evidence(evaluation, policy)
    evidence_id = "ev-" + canonical_hash(
        {
            "model_id": model_id,
            "market": str(market),
            "horizon": horizon,
            "target": str(target),
            "evaluation": evaluation,
        }
    )[:16]
    return ValidationEvidence(
        evidence_id=evidence_id,
        model_id=model_id,
        market=Market(market),
        horizon=int(horizon),
        target=Target(target),
        status=status,
        sample_count=int(evaluation.get("sample_count", 0)),
        independent_dates=int(evaluation.get("independent_dates", 0)),
        date_start=date.fromisoformat(evaluation["date_start"]) if evaluation.get("date_start") else None,
        date_end=date.fromisoformat(evaluation["date_end"]) if evaluation.get("date_end") else None,
        metrics=evaluation.get("metrics", {}),
        calibration=evaluation.get("calibration", {}),
        skill_ci=evaluation.get("skill", {}),
        stability=evaluation.get("stability", {}),
        notes=reason,
        created_at=now_utc(),
    )
