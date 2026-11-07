"""Research factor-risk attribution, separate from expected-return prediction."""
from __future__ import annotations

import numpy as np
import pandas as pd


def estimate_factor_risk(return_panel: pd.DataFrame, factor_returns: pd.DataFrame,
                         cutoff, factors=("MKT", "SMB", "HML", "MOM"),
                         min_observations: int = 120, lookback: int = 252,
                         shrinkage: float = .1, strict_pit: bool = False,
                         annualization: int = 252) -> dict:
    """Estimate r_i-RF = a_i + B_i f + e_i, Sigma = B F B' + D.

    The fixed diagonal shrinkage is a registered engineering choice, not a
    fitted prediction model.  Latest-vintage attribution is explicitly labelled
    retrospective.  Call strict_pit=True to reject unknown available_at.
    """
    if not 0 <= shrinkage <= 1 or min_observations <= len(factors) + 1:
        raise ValueError("invalid shrinkage or observation threshold")
    if return_panel.empty or factor_returns.empty:
        return {"status": "unavailable", "reason": "no_factor_returns_or_security_returns"}
    required = {"date", "security_id", "return_value"}
    if not required <= set(return_panel) or not {"date", "RF", *factors} <= set(factor_returns):
        raise ValueError("risk inputs have missing required columns")
    if return_panel.duplicated(["date", "security_id"]).any() or factor_returns.duplicated("date").any():
        raise ValueError("duplicate dated risk input")
    cutoff_ts = pd.Timestamp(cutoff)
    if cutoff_ts.tzinfo is None:
        cutoff_ts = cutoff_ts.tz_localize("UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
    cutoff_ts = cutoff_ts.tz_convert("UTC")
    panel, common = return_panel.copy(), factor_returns.copy()
    availability_verified = True
    for frame in (panel, common):
        frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.normalize()
        frame.drop(frame[frame["date"] > cutoff_ts.normalize()].index, inplace=True)
        available = pd.to_datetime(frame.get("available_at", pd.Series(None, index=frame.index)), utc=True, errors="coerce")
        if available.isna().any():
            availability_verified = False
            if strict_pit:
                return {"status": "unavailable", "reason": "historical_available_at_unknown"}
        frame.drop(frame[available.notna() & (available > cutoff_ts)].index, inplace=True)
    common = common.sort_values("date").tail(lookback)
    common = common.dropna(subset=["RF", *factors])
    if len(common) < min_observations:
        return {"status": "unavailable", "reason": "insufficient_common_history", "observations": len(common)}
    f = common[list(factors)].to_numpy(float)
    if not np.isfinite(f).all():
        raise ValueError("common factor returns must be finite")
    covariance = np.cov(f, rowvar=False, ddof=1)
    covariance = (1 - shrinkage) * covariance + shrinkage * np.diag(np.diag(covariance))
    exposures, excluded = [], []
    for security_id, security in panel.groupby("security_id", sort=True):
        joined = security.merge(common[["date", "RF", *factors]], on="date", validate="many_to_one").dropna(subset=["return_value", "RF", *factors])
        if len(joined) < min_observations:
            excluded.append({"security_id": str(security_id), "reason": "insufficient_overlap", "observations": len(joined)})
            continue
        X = np.column_stack([np.ones(len(joined)), joined[list(factors)].to_numpy(float)])
        y = joined["return_value"].to_numpy(float) - joined["RF"].to_numpy(float)
        if not np.isfinite(y).all() or np.linalg.matrix_rank(X) < X.shape[1]:
            excluded.append({"security_id": str(security_id), "reason": "invalid_or_rank_deficient_regression"})
            continue
        coefficients = np.linalg.lstsq(X, y, rcond=None)[0]
        residual = y - X @ coefficients
        variance = float(residual @ residual / (len(y) - X.shape[1]))
        total = float(np.sum((y - y.mean()) ** 2))
        exposures.append({"security_id": str(security_id), "observations": len(y),
                          "data_start": joined["date"].min().date().isoformat(),
                          "data_end": joined["date"].max().date().isoformat(),
                          "loadings": dict(zip(factors, coefficients[1:].tolist())),
                          "intercept_daily_descriptive": float(coefficients[0]),
                          "residual_variance_daily": variance,
                          "specific_vol_annualized": float(np.sqrt(max(variance, 0) * annualization)),
                          "r_squared_descriptive": 1 - float(residual @ residual) / total if total > 0 else None})
    if not exposures:
        return {"status": "unavailable", "reason": "no_usable_security_regression", "excluded": excluded}
    B = np.array([[x["loadings"][f] for f in factors] for x in exposures])
    D = np.diag([max(x["residual_variance_daily"], 0) for x in exposures])
    Sigma = (B @ covariance @ B.T + D) * annualization
    return {"status": "descriptive", "method": "FF3_plus_Momentum_OLS_with_diagonal_covariance_shrinkage",
            "formula": "Sigma = B F B^T + D", "factors": list(factors),
            "common_observations": len(common), "common_data_end": common["date"].max().date().isoformat(),
            "annualization": annualization, "diagonal_shrinkage": shrinkage,
            "securities": [x["security_id"] for x in exposures], "exposures": exposures,
            "factor_covariance_daily": covariance.tolist(), "covariance_annualized": Sigma.tolist(),
            "min_covariance_eigenvalue": float(np.linalg.eigvalsh(Sigma).min()),
            "historical_availability_verified": availability_verified,
            "publication": "risk_attribution_only", "excluded": excluded,
            "limitations": ["Retrospective realised-factor attribution; not a forecast or commercial Barra model.",
                            "Idiosyncratic covariance is diagonal; sector factors and crowding channels are not yet included.",
                            "Latest-vintage factors with unknown release times are excluded from predictive training."]}
