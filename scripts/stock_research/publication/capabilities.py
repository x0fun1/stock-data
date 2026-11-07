"""Target-level capability decisions and frozen inference."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..contracts import (
    CapabilityDecision,
    CapabilityState,
    DataLevel,
    Market,
    ModelArtifact,
    QuantitativeResult,
    Target,
    ValidationEvidence,
    ValidationState,
)
from ..models.registry import ModelRegistry, predict_proba


def decide(
    market: Market | str,
    horizon: int,
    target: Target | str,
    data_ready: bool,
    data_reason: str,
    evidence: ValidationEvidence | None,
    artifact: ModelArtifact | None,
    policy: dict[str, Any],
    security_id: str = "",
) -> CapabilityDecision:
    market = Market(market)
    target = Target(target)
    if not data_ready:
        return CapabilityDecision(
            market=market,
            horizon=int(horizon),
            target=target,
            state=CapabilityState.INSUFFICIENT_DATA,
            data_level=DataLevel.UNAVAILABLE,
            reason=data_reason or "data_not_ready",
            security_id=security_id,
        )
    if artifact is None or evidence is None:
        return CapabilityDecision(
            market=market,
            horizon=int(horizon),
            target=target,
            state=CapabilityState.NOT_EXECUTED,
            data_level=DataLevel.DESCRIPTIVE,
            reason="no_active_verified_model",
            security_id=security_id,
            model_id=artifact.model_id if artifact else "",
        )
    if evidence.status is ValidationState.FAILED:
        state = CapabilityState.FAILED
        reason = evidence.notes or "validation_failed"
    elif evidence.status is ValidationState.INVALID:
        state = CapabilityState.FAILED
        reason = evidence.notes or "validation_invalid"
    elif evidence.status is ValidationState.VERIFIED:
        state = CapabilityState.VERIFIED
        reason = "verified_out_of_sample"
    else:
        state = CapabilityState.UNVERIFIED
        reason = evidence.notes or "unverified_out_of_sample"
    return CapabilityDecision(
        market=market,
        horizon=int(horizon),
        target=target,
        state=state,
        data_level=DataLevel.RESEARCH,
        reason=reason,
        security_id=security_id,
        model_id=artifact.model_id,
        evidence_id=evidence.evidence_id,
        details={
            "samples": evidence.sample_count,
            "independent_dates": evidence.independent_dates,
            "skill_ci": evidence.skill_ci,
            "calibration": evidence.calibration.get("fit") if evidence.calibration else None,
        },
    )


def decide_quantile(
    market: Market | str,
    horizon: int,
    data_ready: bool,
    data_reason: str,
    evidence: ValidationEvidence | None,
    artifact,
    security_id: str = "",
) -> CapabilityDecision:
    market = Market(market)
    if not data_ready:
        return CapabilityDecision(market, int(horizon), Target.QUANTILE, CapabilityState.INSUFFICIENT_DATA, DataLevel.UNAVAILABLE, data_reason or "data_not_ready", security_id=security_id)
    if artifact is None or evidence is None:
        return CapabilityDecision(market, int(horizon), Target.QUANTILE, CapabilityState.NOT_EXECUTED, DataLevel.DESCRIPTIVE, "no_active_verified_quantile_model", security_id=security_id)
    if evidence.status is ValidationState.FAILED:
        state = CapabilityState.FAILED
        reason = evidence.notes or "quantile_validation_failed"
    elif evidence.status is ValidationState.VERIFIED:
        state = CapabilityState.VERIFIED
        reason = "verified_out_of_sample"
    else:
        state = CapabilityState.UNVERIFIED
        reason = evidence.notes or "unverified_out_of_sample"
    return CapabilityDecision(
        market,
        int(horizon),
        Target.QUANTILE,
        state,
        DataLevel.RESEARCH,
        reason,
        security_id=security_id,
        model_id=artifact.artifact_id,
        evidence_id=evidence.evidence_id,
        details={"samples": evidence.sample_count, "independent_dates": evidence.independent_dates, "skill_ci": evidence.skill_ci},
    )


def infer_security(
    row: pd.Series,
    security_id: str,
    market: Market | str,
    horizons: list[int],
    targets: list[Target],
    registry: ModelRegistry,
    policy: dict[str, Any],
    data_ready: bool = True,
    data_reason: str = "",
) -> tuple[list[QuantitativeResult], list[CapabilityDecision]]:
    results: list[QuantitativeResult] = []
    decisions: list[CapabilityDecision] = []
    for horizon in horizons:
        for target in targets:
            if Target(target) is Target.QUANTILE:
                q_artifact = registry.load_active_quantile(market, horizon)
                q_evidence = registry.load_evidence(q_artifact.evidence_id) if q_artifact is not None and q_artifact.evidence_id else None
                decision = decide_quantile(market, horizon, data_ready, data_reason, q_evidence, q_artifact, security_id=security_id)
                decisions.append(decision)
                result = QuantitativeResult(
                    market=Market(market),
                    security_id=security_id,
                    horizon=int(horizon),
                    target=Target.QUANTILE,
                    state=decision.state,
                    data_level=decision.data_level,
                    reason=decision.reason,
                    model_id=decision.model_id,
                    evidence_id=decision.evidence_id,
                )
                if q_artifact is not None and q_evidence is not None and decision.state is CapabilityState.VERIFIED:
                    model_features = [k[3:] for k in row if k.startswith("m__")]
                    if not row or not model_features or all(int(row.get(f"m__{name}", 1)) == 1 for name in model_features):
                        result.state = CapabilityState.INSUFFICIENT_DATA
                        result.reason = "all_model_features_missing"
                    else:
                        try:
                            from ..models.quantiles import predict_quantiles

                            result.quantiles = predict_quantiles(q_artifact, pd.DataFrame([row]))
                            result.validation = {
                                "samples": q_evidence.sample_count,
                                "independent_dates": q_evidence.independent_dates,
                                "coverage": (q_evidence.metrics.get("interval_10_90") or {}).get("coverage"),
                            }
                        except Exception as exc:
                            result.state = CapabilityState.FAILED
                            result.reason = f"quantile_inference_error:{type(exc).__name__}"
                results.append(result)
                continue
            artifact = registry.load_active(market, horizon, target)
            evidence = None
            if artifact is not None and artifact.evidence_id:
                evidence = registry.load_evidence(artifact.evidence_id)
            decision = decide(market, horizon, target, data_ready, data_reason, evidence, artifact, policy, security_id=security_id)
            decisions.append(decision)
            result = QuantitativeResult(
                market=Market(market),
                security_id=security_id,
                horizon=int(horizon),
                target=Target(target),
                state=decision.state,
                data_level=decision.data_level,
                reason=decision.reason,
                model_id=decision.model_id,
                evidence_id=decision.evidence_id,
                baseline_probability=artifact.baseline_rate if artifact is not None else None,
            )
            if artifact is not None and evidence is not None and decision.state is CapabilityState.VERIFIED:
                model_features = [k[3:] for k in row if k.startswith("m__")]
                if not row or not model_features or all(int(row.get(f"m__{name}", 1)) == 1 for name in model_features):
                    result.state = CapabilityState.INSUFFICIENT_DATA
                    result.reason = "all_model_features_missing"
                    results.append(result)
                    continue
                try:
                    p = float(predict_proba(artifact, pd.DataFrame([row]))[0])
                    result.probability = p
                    result.validation = {
                        "samples": evidence.sample_count,
                        "independent_dates": evidence.independent_dates,
                        "skill_ci": evidence.skill_ci,
                        "brier": evidence.metrics.get("brier"),
                        "calibration_slope": (evidence.calibration.get("fit") or {}).get("slope"),
                    }
                except Exception as exc:  # keep the other capabilities alive
                    result.state = CapabilityState.FAILED
                    result.reason = f"inference_error:{type(exc).__name__}"
            results.append(result)
    return results, decisions


def descriptive_level(data_ready: bool) -> DataLevel:
    return DataLevel.DESCRIPTIVE if data_ready else DataLevel.UNAVAILABLE
