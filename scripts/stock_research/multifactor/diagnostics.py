"""Factor research metrics with date grouping and honest coverage."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from .preprocessing import raw_factor


def date_block_mean_ci(
    values: Sequence[float],
    *,
    block_days: int = 5,
    resamples: int = 250,
    seed: int = 20261107,
) -> dict:
    """Circular block bootstrap of date-level observations, not stock rows."""
    if block_days < 1 or resamples < 1:
        raise ValueError("bootstrap block_days and resamples must be positive")
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    n = len(data)
    if n < max(10, 2 * block_days):
        return {"status": "insufficient_dates", "dates": n, "lower": None, "upper": None,
                "block_days": block_days, "resamples": resamples}
    rng = np.random.default_rng(seed)
    block_count = int(np.ceil(n / block_days))
    offsets = np.arange(block_days)
    estimates = []
    for _ in range(resamples):
        starts = rng.integers(0, n, size=block_count)
        indices = ((starts[:, None] + offsets) % n).reshape(-1)[:n]
        estimates.append(float(np.mean(data[indices])))
    low, high = np.quantile(estimates, [0.025, 0.975])
    return {"status": "estimated", "dates": n, "lower": float(low), "upper": float(high),
            "block_days": block_days, "resamples": resamples,
            "method": "circular_date_block_bootstrap", "seed": seed}


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)


def factor_diagnostics(
    frame: pd.DataFrame,
    factor_col: str,
    target_col: str = "return_value",
    min_universe: int = 20,
    *,
    quantiles: int = 5,
    cost_bps: float = 10.0,
    block_days: int = 5,
    resamples: int = 250,
    seed: int = 20261107,
) -> dict:
    """Date-local Rank IC and top/bottom research spreads.

    Portfolios are only diagnostic equal-weight baskets.  They are selected
    from scores before looking at returns; missing outcomes do not trigger
    selection of a replacement stock.  No annualized strategy return, capacity
    claim, or executable recommendation is produced.
    """
    if min_universe < 3 or quantiles < 2 or cost_bps < 0:
        raise ValueError("invalid factor-diagnostic settings")
    if not {"security_id", "cutoff_date"}.issubset(frame):
        return {"status": "suspended", "reason": "missing_cross_section_identifiers", "dates": []}
    data = frame.copy()
    data["_score"] = _numeric(data, factor_col)
    data["_target"] = _numeric(data, target_col)
    if "label_status" in data:
        data["_target"] = data["_target"].where(data["label_status"] == "ok")
    data["cutoff_date"] = pd.to_datetime(data["cutoff_date"], errors="raise").dt.date.astype(str)
    if data.duplicated(["cutoff_date", "security_id"]).any():
        raise ValueError("duplicate security in factor diagnostics")
    if "security_type" in data:
        data = data.loc[~data["security_type"].astype(str).str.lower().str.strip().isin({"etf", "etn", "fund", "index", "mutual_fund"})]
    per_date = []
    previous_weights: dict[str, float] = {}
    for day, block in data.groupby("cutoff_date", sort=True):
        valid_score = block.loc[block["_score"].notna()].copy()
        valid_both = valid_score.loc[valid_score["_target"].notna()]
        entry: dict = {"date": str(day), "scheduled_equities": int(len(block)),
                       "score_available": int(len(valid_score)), "evaluable_pairs": int(len(valid_both)),
                       "rank_ic": None, "gross_spread": None, "net_spread": None, "turnover": None}
        if len(valid_both) >= min_universe and valid_both["_score"].nunique() > 1 and valid_both["_target"].nunique() > 1:
            ic = valid_both["_score"].rank(method="average").corr(valid_both["_target"].rank(method="average"))
            entry["rank_ic"] = float(ic) if np.isfinite(ic) else None
        else:
            entry["ic_reason"] = "insufficient_cross_section_or_constant_values"
        if len(valid_score) < min_universe or valid_score["_score"].nunique() < 2:
            entry["spread_reason"] = "insufficient_cross_section_or_constant_scores"
            # Missing score dates cannot be interpreted as an executed liquidation.
            previous_weights = {}
            per_date.append(entry)
            continue
        ordered = valid_score.sort_values(["_score", "security_id"], kind="stable")
        k = max(1, len(ordered) // quantiles)
        low, high = ordered.iloc[:k], ordered.iloc[-k:]
        weights = {str(sid): -1.0 / k for sid in low["security_id"]}
        weights.update({str(sid): 1.0 / k for sid in high["security_id"]})
        turnover = float(sum(abs(weights.get(sid, 0.0) - previous_weights.get(sid, 0.0)) for sid in set(weights) | set(previous_weights)))
        previous_weights = weights
        entry.update({"basket_size_each_leg": k, "turnover": turnover,
                      "cost_assumption": turnover * cost_bps / 10000.0})
        if low["_target"].isna().any() or high["_target"].isna().any():
            entry["spread_reason"] = "selected_basket_has_missing_outcome_no_replacement"
        else:
            gross = float(high["_target"].mean() - low["_target"].mean())
            entry["gross_spread"] = gross
            entry["net_spread"] = gross - entry["cost_assumption"]
        per_date.append(entry)
    ics = [entry["rank_ic"] for entry in per_date if entry["rank_ic"] is not None]
    gross = [entry["gross_spread"] for entry in per_date if entry["gross_spread"] is not None]
    net = [entry["net_spread"] for entry in per_date if entry["net_spread"] is not None]
    ic_std = float(np.std(ics, ddof=1)) if len(ics) > 1 else None
    ic_mean = float(np.mean(ics)) if ics else None
    horizons = pd.to_numeric(data["horizon"], errors="coerce").dropna() if "horizon" in data else pd.Series(dtype=float)
    overlap_note = "forward-horizon windows may overlap; these spreads and costs are not a compounded implementable strategy" if len(horizons) and horizons.max() > 1 else "single-window research baskets; execution is not validated"
    return {"status": "ok" if ics else "suspended", "factor": factor_col, "target": target_col,
            "scheduled_dates": len(per_date), "ic_dates": len(ics), "spread_dates": len(gross),
            "rank_ic_mean": ic_mean, "rank_ic_std": ic_std,
            "icir": ic_mean / ic_std if ic_std is not None and ic_std > 0 else None,
            "icir_definition": "mean date Rank IC / sample std; not annualized",
            "rank_ic_ci": date_block_mean_ci(ics, block_days=block_days, resamples=resamples, seed=seed),
            "gross_spread_mean": float(np.mean(gross)) if gross else None,
            "net_spread_mean": float(np.mean(net)) if net else None,
            "cost_bps_per_unit_turnover": cost_bps,
            "turnover_definition": "sum absolute change of research basket weights; gross exposure two",
            "scope": "research_basket_diagnostic_not_execution_or_capacity_validation", "horizon_note": overlap_note,
            "dates": per_date}


def evaluate_predictions(
    frame: pd.DataFrame,
    predictions: Sequence[float],
    target_col: str,
    *,
    min_universe: int = 20,
    block_days: int = 5,
    resamples: int = 250,
    seed: int = 20261107,
    cost_bps: float = 10.0,
) -> dict:
    """Keep every scheduled row in coverage and full-denominator hit reporting."""
    if len(predictions) != len(frame):
        raise ValueError("predictions and evaluation frame must have equal lengths")
    data = frame.copy()
    data["prediction"] = np.asarray(predictions, dtype=float)
    data["prediction"] = data["prediction"].replace([np.inf, -np.inf], np.nan)
    y = _numeric(data, target_col)
    if "label_status" in data:
        y = y.where(data["label_status"] == "ok")
    if "evaluation_eligible" in data:
        y = y.where(data["evaluation_eligible"].fillna(False).astype(bool))
    data[target_col] = y
    observed = data["prediction"].notna() & y.notna()
    hit = ((data["prediction"] > 0) == (y > 0)) & observed
    pairs = data.loc[observed].copy()
    date_mse, date_accuracy = [], []
    for _, group in pairs.groupby("cutoff_date", sort=True):
        date_mse.append(float(np.mean((group["prediction"] - group[target_col]) ** 2)))
        date_accuracy.append(float(np.mean((group["prediction"] > 0) == (group[target_col] > 0))))
    full_date_hit_rates = []
    for _, group in data.assign(_counted_hit=hit).groupby("cutoff_date", sort=True):
        full_date_hit_rates.append(float(group["_counted_hit"].mean()))
    total, n = len(data), int(observed.sum())
    metrics = {
        "scheduled_rows": total, "prediction_available_rows": int(data["prediction"].notna().sum()),
        "matured_label_rows": int(y.notna().sum()), "evaluable_rows": n,
        "coverage": n / total if total else 0.0,
        "direction_hits": int(hit.sum()),
        "direction_accuracy_full_denominator": float(hit.sum()) / total if total else None,
        "direction_accuracy_available_only": float(hit.sum()) / n if n else None,
        "full_denominator_note": "unavailable predictions or outcomes remain scheduled and contribute no counted hit",
        "date_weighted_mse": float(np.mean(date_mse)) if date_mse else None,
        "mse_available_only": float(np.mean((pairs["prediction"] - pairs[target_col]) ** 2)) if n else None,
        "mae_available_only": float(np.mean(np.abs(pairs["prediction"] - pairs[target_col]))) if n else None,
        "evaluable_dates": len(date_mse),
        "date_accuracy_ci": date_block_mean_ci(date_accuracy, block_days=block_days, resamples=resamples, seed=seed),
        "date_accuracy_ci_scope": "available-only accuracy within each date",
        "date_full_denominator_hit_rate_ci": date_block_mean_ci(full_date_hit_rates, block_days=block_days, resamples=resamples, seed=seed),
    }
    factor = factor_diagnostics(data, "prediction", target_col, min_universe,
                                cost_bps=cost_bps, block_days=block_days, resamples=resamples, seed=seed)
    metrics.update({"rank_ic_mean": factor["rank_ic_mean"], "icir": factor["icir"],
                    "rank_ic_dates": factor["ic_dates"], "rank_ic_ci": factor["rank_ic_ci"],
                    "research_baskets": factor})
    return metrics


def training_correlation_clusters(
    frame: pd.DataFrame,
    factors: Sequence[str],
    threshold: float = 0.8,
    min_pairs: int = 20,
) -> dict:
    """Connected correlation groups; first predeclared member is representative."""
    if not 0 < threshold <= 1 or min_pairs < 2:
        raise ValueError("invalid correlation settings")
    if len(set(factors)) != len(factors):
        raise ValueError("factor names must be unique")
    if "partition" in frame and any(frame["partition"].astype(str) != "train"):
        raise ValueError("correlation clustering accepts the training block only")
    values = pd.DataFrame({name: raw_factor(frame, name) for name in factors})
    corr = values.corr(min_periods=min_pairs)
    visited: set[str] = set()
    clusters = []
    for name in factors:
        if name in visited:
            continue
        pending, members = [name], []
        while pending:
            member = pending.pop(0)
            if member in visited:
                continue
            visited.add(member)
            members.append(member)
            for other in factors:
                if other in visited:
                    continue
                coefficient = corr.loc[member, other]
                if pd.notna(coefficient) and abs(float(coefficient)) >= threshold:
                    pending.append(other)
        ordered = [item for item in factors if item in members]
        clusters.append({"representative": ordered[0], "members": ordered})
    matrix = {name: {other: float(corr.loc[name, other]) if pd.notna(corr.loc[name, other]) else None for other in factors} for name in factors}
    return {"scope": "train_only", "absolute_correlation_threshold": threshold,
            "minimum_pair_observations": min_pairs,
            "clusters": clusters, "representatives": [group["representative"] for group in clusters],
            "correlation_matrix": matrix,
            "representative_rule": "first factor in predeclared order; not selected using test IC"}
