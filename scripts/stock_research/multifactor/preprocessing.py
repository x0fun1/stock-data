"""Date-local robust normalization and conditional risk neutralization."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from ..features.registry import MACRO_FACTORS

_NON_EQUITY_TYPES = {"etf", "etn", "fund", "index", "mutual_fund"}
_SIZE_FACTORS = {"size_log", "log_mcap", "market_cap", "market_cap_log"}


def confirmed(value) -> bool:
    """Parse confirmation flags without treating the string 'false' as true."""
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(np.isfinite(value) and value == 1)
    return isinstance(value, str) and value.strip().lower() == "true"


def raw_factor(frame: pd.DataFrame, name: str) -> pd.Series:
    column = "f__" + name if "f__" + name in frame else name
    values = pd.to_numeric(frame[column], errors="coerce") if column in frame else pd.Series(np.nan, index=frame.index, dtype=float)
    values = values.replace([np.inf, -np.inf], np.nan)
    if "m__" + name in frame:
        missing = pd.to_numeric(frame["m__" + name], errors="coerce").fillna(1) != 0
        values = values.mask(missing)
    return values


def _robust_z(values: pd.Series, mad_limit: float = 5.0) -> tuple[pd.Series, dict]:
    observed = values.dropna()
    median = float(observed.median())
    mad = float(np.median(np.abs(observed - median)))
    scale = 1.4826 * mad
    method = "median_mad"
    if not np.isfinite(scale) or scale <= 0:
        iqr = float(observed.quantile(0.75) - observed.quantile(0.25))
        scale = iqr / 1.349 if iqr > 0 else float(observed.std(ddof=1))
        method = "median_iqr" if iqr > 0 else "median_std"
    if not np.isfinite(scale) or scale <= 0:
        return pd.Series(np.nan, index=values.index), {"status": "suspended", "reason": "constant_factor"}
    lower, upper = median - mad_limit * scale, median + mad_limit * scale
    result = (values.clip(lower, upper) - median) / scale
    return result, {"status": "ok", "method": method, "median": median,
                    "scale": scale, "lower": lower, "upper": upper,
                    "winsorized_count": int(((observed < lower) | (observed > upper)).sum())}


def _control_matrix(frame: pd.DataFrame, *, omit_size: bool) -> tuple[np.ndarray | None, dict]:
    required = {"industry", "industry_pit_confirmed", "size_pit_confirmed"}
    if not required.issubset(frame):
        return None, {"status": "suspended", "reason": "missing_point_in_time_risk_controls"}
    if not frame["industry_pit_confirmed"].map(confirmed).all() or not frame["size_pit_confirmed"].map(confirmed).all():
        return None, {"status": "suspended", "reason": "unconfirmed_point_in_time_risk_controls"}
    industry = frame["industry"].astype("string")
    if industry.isna().any() or (industry.str.strip() == "").any():
        return None, {"status": "suspended", "reason": "missing_industry"}
    counts = industry.value_counts()
    if len(counts) < 2 or int(counts.min()) < 3:
        return None, {"status": "suspended", "reason": "insufficient_industry_groups",
                      "industry_counts": {str(k): int(v) for k, v in counts.items()}}
    dummies = pd.get_dummies(industry, drop_first=True, dtype=float).to_numpy(dtype=float)
    columns = [np.ones((len(frame), 1)), dummies]
    if not omit_size:
        size_column = next((x for x in ("log_mcap", "f__size_log", "f__log_mcap") if x in frame), None)
        if size_column is None:
            return None, {"status": "suspended", "reason": "missing_log_market_cap"}
        size = pd.to_numeric(frame[size_column], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(size).all() or np.std(size) <= 0:
            return None, {"status": "suspended", "reason": "invalid_log_market_cap"}
        size = (size - size.mean()) / size.std()
        columns.append(size.reshape(-1, 1))
    design = np.concatenate(columns, axis=1)
    rank = int(np.linalg.matrix_rank(design))
    dof = len(frame) - design.shape[1]
    if rank != design.shape[1] or dof < 10:
        return None, {"status": "suspended", "reason": "insufficient_control_rank_or_degrees_of_freedom",
                      "rank": rank, "parameters": int(design.shape[1]), "degrees_of_freedom": dof}
    condition = float(np.linalg.cond(design))
    if not np.isfinite(condition) or condition > 1e8:
        return None, {"status": "suspended", "reason": "ill_conditioned_risk_controls", "condition_number": condition}
    return design, {"status": "ok", "parameters": int(design.shape[1]), "rank": rank,
                    "degrees_of_freedom": dof, "size_control": not omit_size,
                    "condition_number": condition}


def prepare_cross_section(
    frame: pd.DataFrame,
    factors: Sequence[str],
    neutralize: bool = True,
    min_universe: int = 20,
) -> dict:
    """Normalize only a contemporaneous equity cross-section.

    Return ``{'frame': DataFrame, 'diagnostics': dict}``.  ``z__`` columns are
    robust date-local scores; ``n__`` columns are residual scores when explicitly
    requested and estimable.  Suspended factors remain NaN.  With
    ``neutralize=False``, n__ equals z__ and is labelled unneutralized.
    """
    if min_universe < 3:
        raise ValueError("min_universe must be at least 3")
    if len(set(factors)) != len(factors):
        raise ValueError("factor names must be unique")
    if not {"cutoff_date", "security_id"}.issubset(frame):
        if frame.empty:
            empty = frame.copy()
            for name in factors:
                empty["z__" + name] = np.nan
                empty["n__" + name] = np.nan
            return {"frame": empty, "diagnostics": {"status": "suspended", "reason": "empty_universe", "dates": {}}}
        raise ValueError("cross-section requires cutoff_date and security_id")
    out = frame.copy().reset_index(drop=True)
    dates = pd.to_datetime(out["cutoff_date"], errors="coerce")
    if dates.isna().any() or out["security_id"].isna().any():
        raise ValueError("cross-section identifiers and dates must be valid")
    out["cutoff_date"] = dates.dt.date.astype(str)
    if out.duplicated(["cutoff_date", "security_id"]).any():
        raise ValueError("duplicate security in the same cross-section")
    excluded = 0
    if "security_type" in out:
        eligible = ~out["security_type"].astype(str).str.lower().str.strip().isin(_NON_EQUITY_TYPES)
        excluded = int((~eligible).sum())
        out = out.loc[eligible].copy().reset_index(drop=True)
    for name in factors:
        out["z__" + name] = np.nan
        out["n__" + name] = np.nan
    diagnostics: dict = {"status": "suspended", "input_rows": int(len(frame)),
                         "equity_rows": int(len(out)), "excluded_non_equity_rows": excluded,
                         "min_universe": min_universe, "neutralize_requested": bool(neutralize),
                         "dates": {}, "common_macro_policy": "context_not_cross_sectional_score"}
    for day, block in out.groupby("cutoff_date", sort=True):
        day_diag: dict = {"equities": int(len(block)), "factors": {}}
        for name in factors:
            diag: dict = {"status": "suspended", "observed": 0}
            day_diag["factors"][name] = diag
            if name in MACRO_FACTORS:
                diag["reason"] = "common_macro_context"
                continue
            values = raw_factor(block, name)
            valid = values.notna()
            diag["observed"] = int(valid.sum())
            if len(block) < min_universe or int(valid.sum()) < min_universe:
                diag["reason"] = "insufficient_equity_cross_section"
                continue
            z, norm = _robust_z(values)
            diag["normalization"] = norm
            if norm["status"] != "ok":
                diag["reason"] = norm["reason"]
                continue
            out.loc[block.index, "z__" + name] = z
            diag["status"] = "ok"
            if not neutralize:
                out.loc[block.index, "n__" + name] = z
                diag["neutralization"] = {"status": "disabled", "reason": "unneutralized_by_request"}
                continue
            controls, control_diag = _control_matrix(block.loc[valid], omit_size=name in _SIZE_FACTORS)
            diag["neutralization"] = control_diag
            if controls is None:
                continue
            observed_z = z.loc[valid].to_numpy(dtype=float)
            coefficients, _, _, _ = np.linalg.lstsq(controls, observed_z, rcond=None)
            residuals = observed_z - controls @ coefficients
            scale = float(np.std(residuals, ddof=1))
            if scale <= 1e-12 or not np.isfinite(scale):
                diag["neutralization"] = {**control_diag, "status": "suspended", "reason": "no_residual_variation"}
                continue
            out.loc[block.index[valid], "n__" + name] = residuals / scale
        diagnostics["dates"][str(day)] = day_diag
    if any(item["status"] == "ok" for day in diagnostics["dates"].values() for item in day["factors"].values()):
        diagnostics["status"] = "ok"
    diagnostics["normalization_status"] = diagnostics["status"]
    if not neutralize:
        diagnostics["neutralization_status"] = "disabled"
    else:
        ready = sum(int(out["n__" + name].notna().any()) for name in factors if name not in MACRO_FACTORS)
        normalizable = sum(int(out["z__" + name].notna().any()) for name in factors if name not in MACRO_FACTORS)
        diagnostics["neutralization_status"] = "ok" if ready and ready == normalizable else "partial" if ready else "suspended"
    if out.empty:
        diagnostics["reason"] = "empty_equity_universe"
    return {"frame": out, "diagnostics": diagnostics}
