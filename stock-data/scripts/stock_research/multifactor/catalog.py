"""Predeclared factor families and a transparent comparison score.

Signs express an economic hypothesis for the equal-weight control, not an
empirically established premium.  Ambiguous trading signals and common macro
levels are deliberately absent from the default signed control.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ..features.registry import ALL_FACTORS, MACRO_FACTORS

FACTOR_DIRECTIONS: dict[str, float] = {
    "mom_60": 1.0, "mom_12_1": 1.0, "trend_diff": 1.0,
    "residual_reversal": 1.0, "relative_strength": 1.0,
    "ep_ttm": 1.0, "fcf_yield_ttm": 1.0, "bp": 1.0,
    "roa_ttm": 1.0, "accruals_ttm": 1.0, "gross_margin_ttm": 1.0,
    "revenue_growth_yoy": 1.0, "profit_improvement_yoy": 1.0,
    "leverage": -1.0, "net_debt_ratio": -1.0,
    "interest_coverage": 1.0, "share_change_yoy": -1.0,
    "roe_ttm": 1.0, "asset_growth_yoy": -1.0,
    "rv_20": -1.0, "downside_vol_20": -1.0, "idio_vol_252": -1.0,
    "size_log": -1.0, "log_mcap": -1.0,
}

_GROUP_OVERRIDES = {
    "residual_reversal": "reversal",
    "mom_60": "momentum", "mom_12_1": "momentum",
    "trend_diff": "momentum", "relative_strength": "momentum",
    "revenue_growth_yoy": "growth", "profit_improvement_yoy": "growth",
    "roa_ttm": "quality", "roe_ttm": "quality", "accruals_ttm": "quality",
    "gross_margin_ttm": "quality", "leverage": "balance_sheet",
    "net_debt_ratio": "balance_sheet", "interest_coverage": "balance_sheet",
    "share_change_yoy": "dilution", "asset_growth_yoy": "growth",
    "size_log": "size", "log_mcap": "size",
    "rv_20": "low_volatility", "downside_vol_20": "low_volatility",
    "idio_vol_252": "low_volatility", "beta_252": "risk_exposure",
    "abnormal_volume": "liquidity", "amihud_20": "liquidity",
}


def factor_group(name: str, overrides: Mapping[str, str] | None = None) -> str:
    if overrides and name in overrides:
        return str(overrides[name])
    if name.startswith("kronos_"):
        return "kronos_research"
    if name in _GROUP_OVERRIDES:
        return _GROUP_OVERRIDES[name]
    spec = ALL_FACTORS.get(name)
    return spec.group if spec else "custom"


def group_equal_weight_score(
    frame: pd.DataFrame,
    factors: Sequence[str],
    directions: Mapping[str, float] | None = None,
    *,
    column_prefix: str = "z__",
    min_groups: int = 2,
    groups: Mapping[str, str] | None = None,
    require_complete: bool = True,
    return_details: bool = False,
) -> pd.Series | dict:
    """Average signed factors within family, then average covered families.

    Missing observations stay missing; they never contribute a neutral vote.
    Equal weighting is a declared comparison control, not a probability.  The
    caller should report family coverage alongside this score.
    """
    if min_groups < 1:
        raise ValueError("min_groups must be positive")
    signs = {**FACTOR_DIRECTIONS, **dict(directions or {})}
    by_group: dict[str, list[pd.Series]] = {}
    factor_values: dict[str, pd.Series] = {}
    for name in factors:
        if name in MACRO_FACTORS:
            continue
        sign = signs.get(name)
        if sign is None or not np.isfinite(sign) or sign == 0:
            continue
        column = column_prefix + name
        values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan) if column in frame else pd.Series(np.nan, index=frame.index)
        factor_values[name] = values
        by_group.setdefault(factor_group(name, groups), []).append(values * float(sign))
    family_values = {
        group: pd.concat(values, axis=1).mean(axis=1, skipna=True)
        for group, values in by_group.items()
    }
    if not family_values:
        score = pd.Series(np.nan, index=frame.index, name="group_equal_weight_score")
        coverage = pd.DataFrame({"covered_family_count": 0, "required_family_count": 0}, index=frame.index)
        return {"score": score, "coverage": coverage, "families": [], "family_values": pd.DataFrame(index=frame.index)} if return_details else score
    panel = pd.DataFrame(family_values, index=frame.index)
    covered = panel.notna().sum(axis=1)
    allowed = covered >= min_groups
    if require_complete:
        allowed &= covered == len(family_values)
    score = panel.mean(axis=1, skipna=True).where(allowed)
    score.name = "group_equal_weight_score"
    coverage = pd.DataFrame({"covered_family_count": covered, "required_family_count": len(family_values),
                             "covered_families": panel.apply(lambda row: [name for name, value in row.items() if pd.notna(value)], axis=1),
                             "complete_family_coverage": covered == len(family_values)}, index=frame.index)
    factor_panel = pd.DataFrame(factor_values, index=frame.index)
    coverage["covered_factor_count"] = factor_panel.notna().sum(axis=1)
    coverage["required_factor_count"] = len(factor_values)
    coverage["covered_factors"] = factor_panel.apply(lambda row: [name for name, value in row.items() if pd.notna(value)], axis=1)
    score.attrs["families"] = list(family_values)
    score.attrs["require_complete"] = require_complete
    score.attrs["coverage"] = coverage.to_dict(orient="records")
    if return_details:
        return {"score": score, "coverage": coverage, "families": list(family_values), "family_values": panel,
                "weighting_policy": "fixed_family_set_complete_coverage" if require_complete else "explicit_partial_family_coverage"}
    return score
