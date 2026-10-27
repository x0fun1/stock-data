"""Research A: drift-shrunk causal base rate on the fixed 52-week / 5-session window.

Version 2 replaces the 20-session momentum hit rate with a volatility-calibrated
drift prior.  The reason is empirical, not aesthetic: measured on 15 real series
over 2018-2026, the v1 estimator put ~68% of its weight on a feature with no
5-day directional information and lost about 1% of Brier skill against simply
reporting the causal base rate, on 15 of 15 series.  Shrinking the base rate
toward a drift prior instead is pure variance reduction: it recovers that loss
without claiming predictive power, and its result is insensitive to the drift
assumption itself.

Everything remains re-derivable from the frozen bars.  The transform record
carries the base-rate label boundary, the volatility window, ``n_eff``, ``k``,
``mu``, ``p_vol`` and the matching origins, and the current prediction and every
walk-forward row are produced by the same function.  ROC20 is still computed and
reported, but only as a disclosed state variable: it no longer enters the
probability.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

from ..contracts import researcher_result
from ..forecast_policy import (
    DRIFT_MIN_VOL_SESSIONS,
    DRIFT_MU_ANNUAL,
    DRIFT_SHRINKAGE_K,
    DRIFT_SHRINKAGE_VERSION,
    DRIFT_VOL_WINDOW_SESSIONS,
    FEATURE_ROLE_IN_PROBABILITY,
    FORECAST_HORIZON_SESSIONS,
    MIN_PROSPECTIVE_NONOVERLAP,
    NONOVERLAP_GAP_SESSIONS,
    SESSIONS_PER_YEAR,
    SHRINKAGE_MIN_MATCHES,
    SHRINKAGE_PRIOR_STRENGTH,
    SHRINKAGE_VERSION,
    dividend_label_disclosure,
    model_identity,
)
from ..market_data import (
    adjusted_close_series,
    calendar_session_list,
    evaluation_sample,
    forward_returns,
    market_bars,
    non_overlapping,
    prediction_price_series,
)
from ..math_utils import mean, wilson_interval
from ..momentum import compute as compute_momentum
from ..momentum import rank_ic_diagnostic
from ..momentum import roc as momentum_roc
from ..snapshot import load_snapshot
from ..statistics import baseline_comparison, power_curve

MINIMUM_MATCHING_SAMPLES = SHRINKAGE_MIN_MATCHES
SHRINKAGE_PRIOR_OBSERVATIONS = SHRINKAGE_PRIOR_STRENGTH
ROC_WINDOW = 20


def _causal_up_rate(forward: list[float | None], decision: int, horizon: int) -> tuple[float | None, list[int]]:
    """Return the causal up-rate and the label origins that matured by ``decision``."""

    origins = [index for index in range(max(0, decision - horizon + 1)) if forward[index] is not None]
    if not origins:
        return None, []
    return sum(1 for index in origins if float(forward[index]) > 0) / len(origins), origins


def _drift_prior(closes: list[float], decision: int, horizon: int) -> dict[str, Any] | None:
    """Volatility-calibrated drift prior using returns up to the decision close.

    ``sigma`` is the sample standard deviation of daily log returns over at most
    250 sessions ending at ``decision``.  The prior probability is
    ``Phi(mu * H / (sigma * sqrt(H)))`` with ``mu`` the policy drift per session.
    No future bar is read: the last return is ``close[decision]/close[decision-1]``.
    """

    start = max(1, decision - DRIFT_VOL_WINDOW_SESSIONS + 1)
    returns: list[float] = []
    for index in range(start, decision + 1):
        previous, current = closes[index - 1], closes[index]
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))
    if len(returns) < DRIFT_MIN_VOL_SESSIONS:
        return None
    center = sum(returns) / len(returns)
    variance = sum((value - center) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    sigma = math.sqrt(variance)
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    mu_session = DRIFT_MU_ANNUAL / SESSIONS_PER_YEAR
    z_score = mu_session * horizon / (sigma * math.sqrt(horizon))
    probability = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
    return {
        "mu_annual": DRIFT_MU_ANNUAL,
        "mu_per_session": mu_session,
        "sigma_daily": sigma,
        "sigma_horizon": sigma * math.sqrt(horizon),
        "z_score": z_score,
        "p_vol": probability,
        "vol_window_sessions": DRIFT_VOL_WINDOW_SESSIONS,
        "return_count": len(returns),
        "return_window_start": start,
        "return_window_end": decision,
    }


def _drift_shrunk_probability(base_rate: float | None, n_eff: float, p_vol: float | None, k: int = DRIFT_SHRINKAGE_K) -> float | None:
    if base_rate is None or p_vol is None or n_eff < 0:
        return None
    return (n_eff * base_rate + k * p_vol) / (n_eff + k)


def _no_probability_warning(row: dict[str, Any]) -> str:
    """Describe the actual abstention reason.

    The v1 warning blamed the matching-sample floor even after v2 stopped using
    the matching sample as evidence weight, which pointed readers at the wrong
    cause.  Each reason code now names its own gap; the matching count is
    disclosed only as context.
    """

    code = str(row.get("reason_code") or "PROBABILITY_UNAVAILABLE")
    matches = row.get("matching_observations", (row.get("probability_transform") or {}).get("observations", 0))
    if code == "BASE_RATE_UNAVAILABLE":
        return (
            "No reportable probability: BASE_RATE_UNAVAILABLE (no matured 5-session label exists before the "
            f"decision close; {matches} same-state matching origins are disclosure only)."
        )
    if code == "VOLATILITY_UNAVAILABLE":
        return (
            "No reportable probability: VOLATILITY_UNAVAILABLE (fewer than "
            f"{DRIFT_MIN_VOL_SESSIONS} positive finite daily returns are available for the volatility prior)."
        )
    return f"No reportable probability: {code}."


def _matching_indices(
    closes: list[float], forward: list[float | None], decision: int, horizon: int,
    *, window: int = ROC_WINDOW, gap: int = NONOVERLAP_GAP_SESSIONS,
) -> list[int]:
    """Same-sign, matured origins accepted in chronological order at least ``gap`` apart.

    The sign rule treats a zero ROC as non-negative and is fixed for the version.
    Selection is causal: only labels whose target session is at/before the
    decision session are eligible.
    """

    current_signal = momentum_roc(closes, decision, window)
    if current_signal is None:
        return []
    current_positive = current_signal >= 0
    selected: list[int] = []
    last = None
    for prior in range(window, min(decision, len(forward))):
        if prior + horizon > decision or forward[prior] is None:
            continue
        prior_signal = momentum_roc(closes, prior, window)
        if prior_signal is None or (prior_signal >= 0) != current_positive:
            continue
        if last is not None and prior - last < gap:
            continue
        selected.append(prior)
        last = prior
    return selected


def predict_at_origin(
    dates: list[str],
    closes: list[float],
    decision: int,
    *,
    horizon: int = FORECAST_HORIZON_SESSIONS,
    window_start_exclusive: str | None = None,
    expected_window_sessions: int | None = None,
    coverage_verified: bool = False,
    gap: int = NONOVERLAP_GAP_SESSIONS,
) -> dict[str, Any]:
    """Produce the frozen-policy prediction for one decision session.

    All inputs are prefix data: the volatility prior uses daily returns whose
    last observation is ``close[decision]/close[decision-1]`` and the base rate
    uses only labels that matured at or before that close.  The returned
    ``probability_transform`` is the audit contract; auditors rebuild every field
    from bars rather than trusting the summary counts.
    """

    if not 0 <= decision < len(closes):
        raise ValueError("decision index outside the price series")
    forward = forward_returns(closes, horizon)
    current_signal = momentum_roc(closes, decision, ROC_WINDOW)
    base_rate, base_origins = _causal_up_rate(forward, decision, horizon)
    n_eff = len(base_origins) / horizon if base_origins else 0.0
    drift = _drift_prior(closes, decision, horizon)
    identity = model_identity("quant")
    transform = {
        "type": "causal_drift_shrinkage",
        "version": DRIFT_SHRINKAGE_VERSION,
        "feature": {
            "name": "roc20",
            "window_sessions": ROC_WINDOW,
            "zero_state": "non_negative",
            "used_for_probability": False,
            "role": FEATURE_ROLE_IN_PROBABILITY,
        },
        "horizon_sessions": horizon,
        "minimum_gap_sessions": gap,
        "minimum_matches": MINIMUM_MATCHING_SAMPLES,
        "matching_floor_role": "disclosure_only_not_an_eligibility_gate",
        "legacy_prior_strength": SHRINKAGE_PRIOR_OBSERVATIONS,
        "legacy_transform_version": SHRINKAGE_VERSION,
        "k": DRIFT_SHRINKAGE_K,
        "probability_formula": "p = (n_eff * base_rate + k * p_vol) / (n_eff + k)",
        "drift_prior": drift,
        "n_eff": n_eff,
        "base_rate_source": "all_matured_daily_5d_labels",
        "base_rate_label_count": len(base_origins),
        "base_rate_label_end_max": dates[base_origins[-1] + horizon] if base_origins else None,
        "matching_origins": [],
        "matching_label_ends": [],
        "policy_version": identity["policy_version"],
        "model_version": identity["model_version"],
        "feature_version": identity["feature_version"],
        "target_version": identity["target_version"],
    }
    row: dict[str, Any] = {
        "decision_session": dates[decision],
        "decision_index": decision,
        "probability": None,
        "raw_probability": None,
        "probability_transform": transform,
        "causal_base_rate": base_rate,
        "n_eff": n_eff,
        "target": int(float(forward[decision]) > 0) if forward[decision] is not None else None,
        "label_end_session": dates[decision + horizon] if decision + horizon < len(dates) else None,
        "status": "abstained",
        "reason_code": None,
        "model_identity": identity,
    }
    # The matching sample is disclosed, never used as evidence weight.  It is
    # still built for every origin so a reader can see the same-state history.
    selected = _matching_indices(closes, forward, decision, horizon, gap=gap)
    transform["matching_origins"] = [dates[index] for index in selected]
    transform["matching_label_ends"] = [dates[index + horizon] for index in selected]
    transform["matching_label_end_max"] = transform["matching_label_ends"][-1] if selected else None
    transform["base_rate_at_origin"] = base_rate
    transform["wins"] = sum(1 for index in selected if float(forward[index]) > 0)
    transform["observations"] = len(selected)
    row["matching_observations"] = len(selected)
    row["matching_wins"] = transform["wins"]
    transform["p_vol"] = (drift or {}).get("p_vol")
    if base_rate is None:
        row["reason_code"] = "BASE_RATE_UNAVAILABLE"
        return row
    if drift is None:
        row["reason_code"] = "VOLATILITY_UNAVAILABLE"
        row["volatility_returns_required"] = DRIFT_MIN_VOL_SESSIONS
        return row
    if current_signal is None:
        # Only reachable on a very short prefix; the probability itself does not
        # need ROC20, so the row stays computable and discloses the gap.
        row["feature_state"] = "unavailable"
    probability = _drift_shrunk_probability(base_rate, n_eff, drift["p_vol"])
    if probability is None:
        row["reason_code"] = "PROBABILITY_UNAVAILABLE"
        return row
    if len(selected) < MINIMUM_MATCHING_SAMPLES:
        row["matching_disclosure"] = (
            f"{len(selected)} same-state matching origins; the {MINIMUM_MATCHING_SAMPLES}-match floor is "
            "disclosed context, not an eligibility gate under v2"
        )
    else:
        row["matching_disclosure"] = None
    if not selected:
        row["matching_disclosure"] = "no same-state matching origin inside this window"
    raw = transform["wins"] / len(selected) if selected else None
    row.update(
        probability=probability,
        raw_probability=raw,
        status="matured",
        mean_forward_return=mean([float(forward[index]) for index in selected]) if selected else None,
        wilson_95_interval=list(wilson_interval(transform["wins"], len(selected))) if selected else None,
        wilson_interval_scope="raw_matching_hit_frequency_only; overlapping labels, not a confidence interval for the shrunk probability",
    )
    row["available_history_days"] = (
        (date.fromisoformat(dates[decision]) - date.fromisoformat(window_start_exclusive)).days
        if window_start_exclusive else None
    )
    row["available_history_sessions"] = decision + 1
    # ``available_history_days`` measures the calendar span from the window's left
    # edge to the decision date, which a truncated series also satisfies when the
    # left edge is derived from the first bar.  The flag therefore also requires
    # the recomputed coverage from the freeze gate: the session count must reach
    # the calendar's expected count for the whole policy window.
    row["full_52w_input"] = bool(
        coverage_verified
        and isinstance(expected_window_sessions, int)
        and expected_window_sessions > 0
        and row["available_history_sessions"] >= expected_window_sessions
        and window_start_exclusive
        and row["available_history_days"] is not None
        and row["available_history_days"] >= 364
    )
    row["evidence_scope"] = "prefix_walk_forward"
    row["target_definition"] = f"close[t+{horizon}] / close[t] - 1 > 0"
    return row


def _benchmark_series(market: dict[str, Any]) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """Extract the declared benchmark series, refusing to guess one."""

    block = (market.get("data") or {}).get("benchmark")
    if not isinstance(block, dict):
        return None, {"symbol": None, "selection": "absent", "status": "absent"}
    meta = {"symbol": block.get("symbol"), "selection": block.get("selection", "declared")}
    bars = block.get("bars")
    if not isinstance(bars, list) or not bars:
        return None, {**meta, "status": "empty"}
    try:
        dates, values, provenance = adjusted_close_series(
            {"data": {"bars": bars}, "adjustment": block.get("adjustment")}
        )
    except ValueError:
        return None, {**meta, "status": "invalid"}
    return dict(zip(dates, values, strict=True)), {**meta, "status": "ok", "price_basis": provenance["basis_status"]}


def run(snapshot_dir: Path, loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    horizon = FORECAST_HORIZON_SESSIONS
    bars = market_bars(market)
    # The production target and every feature ratio use the same declared
    # split-adjusted close, never a dividend-reinvested substitute.
    dates, closes, price_provenance = prediction_price_series(market)
    momentum_dates, momentum_closes, momentum_provenance = adjusted_close_series(market)
    benchmark_closes, benchmark_meta = _benchmark_series(market)
    policy_window = (market.get("data") or {}).get("policy_window") or {}
    window_start = policy_window.get("window_start_exclusive")
    expected_window_sessions = policy_window.get("expected_sessions")
    coverage_verified = (
        policy_window.get("coverage_status") == "complete"
        and bool(policy_window.get("coverage_recomputed"))
        and bool(policy_window.get("calendar_verified"))
    )
    sessions = calendar_session_list(market)
    current = len(bars) - 1
    current_signal = momentum_roc(closes, current, ROC_WINDOW)
    if current_signal is None:
        return researcher_result(
            "quant", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data",
            warnings=["At least 21 daily closes are required for the predeclared 20-session momentum hypothesis."],
        )

    current_row = predict_at_origin(
        dates, closes, current, horizon=horizon, window_start_exclusive=window_start,
        expected_window_sessions=expected_window_sessions, coverage_verified=coverage_verified,
    )
    probability = current_row["probability"]
    transform = current_row["probability_transform"]
    forward = forward_returns(closes, horizon)

    ledger_decisions = [
        index for index in range(ROC_WINDOW + horizon, current)
        if index + horizon <= current and forward[index] is not None
    ]
    oos_forecasts: list[dict[str, Any]] = []
    predicted_decisions: list[int] = []
    for decision in ledger_decisions:
        row = predict_at_origin(
            dates, closes, decision, horizon=horizon, window_start_exclusive=window_start,
            expected_window_sessions=expected_window_sessions, coverage_verified=coverage_verified,
        )
        row["label_available_at"] = dates[decision + horizon]
        if row["probability"] is not None:
            predicted_decisions.append(decision)
        oos_forecasts.append(row)

    matured_oos = [row for row in oos_forecasts if row["probability"] is not None]
    nonsampled = evaluation_sample(oos_forecasts, sessions)
    sample_rows = nonsampled["sampled_predictions"]
    effectiveness = baseline_comparison(matured_oos, horizon=horizon)
    warnings = [
        "Returns are gross close-to-close observations; this baseline does not model execution, spreads, slippage or fees.",
        "The replay ledger records daily predictions whose 5-session labels overlap; the effective sample size is the row count divided by 5, and records remain correlated.",
        "ROC20 is retained as a disclosed state variable only; it no longer enters the probability.",
        "The 5D target excludes cash dividends; a window spanning an ex-dividend date can flip a near-flat label downward.",
    ]
    if probability is None:
        warnings.append(_no_probability_warning(current_row))
    if not matured_oos:
        warnings.append("No matured walk-forward prediction was available on this prefix.")
    if current_row.get("full_52w_input") is False:
        warnings.append("Current decision does not yet have a full 52-week input window.")
    evidence = {"positive": [], "negative": []}
    if probability is not None:
        target = "positive" if probability >= 0.5 else "negative"
        evidence[target].append({
            "family": "base_rate_variance_reduction",
            "hypothesis": "the causal 5D up-rate is a better 5-session estimator than a momentum-conditioned hit rate; it is shrunk toward a volatility-calibrated drift prior",
            "signal_value": current_signal,
            "empirical_hit_rate": current_row["raw_probability"],
            "shrunk_probability": probability,
            "probability_basis": "drift_shrunk_causal_base_rate",
            "probability_transform_version": DRIFT_SHRINKAGE_VERSION,
            "causal_base_rate": current_row["causal_base_rate"],
            "drift_prior_probability": transform.get("p_vol"),
            "shrinkage_k": DRIFT_SHRINKAGE_K,
            "matching_observations": current_row["matching_observations"],
            "matching_wins": current_row["matching_wins"],
            "n_raw": current_row["matching_observations"],
            "n_effective": current_row.get("n_eff"),
            "wilson_95_interval": current_row["wilson_95_interval"],
            "wilson_interval_method": "Wilson score on the raw matching hit frequency; nominal IID approximation with overlapping labels; not a confidence interval for the shrunk probability",
            "wilson_coverage_validated": False,
            "mean_forward_return": current_row["mean_forward_return"],
            "rationale": (
                f"The causal base rate {current_row['causal_base_rate']:.4f} is measured on "
                f"{transform['base_rate_label_count']} matured 5D labels and shrunk toward p_vol="
                f"{transform['p_vol']:.4f} with k={DRIFT_SHRINKAGE_K}; n_eff={current_row['n_eff']:.1f}. "
                "The ROC20 state is disclosed for context only."
            ),
        })
    if (manifest.get("data_quality") or {}).get("overall") == "blocked":
        warnings.append("Snapshot data validation is blocked; this result is retained only for diagnosis.")
    momentum_block = compute_momentum(
        momentum_closes,
        momentum_dates,
        benchmark_closes,
        symbol=ticker,
        benchmark_symbol=benchmark_meta.get("symbol"),
        benchmark_selection=benchmark_meta.get("selection", "absent"),
        price_field=momentum_provenance["price_field"],
    )
    momentum_block["price_basis"] = momentum_provenance
    momentum_block["benchmark"]["status"] = benchmark_meta.get("status")
    momentum_block["diagnostic_rank_ic"] = rank_ic_diagnostic(
        momentum_closes, momentum_dates, benchmark_closes, horizon=horizon
    )
    identity = model_identity("quant")
    return researcher_result(
        "quant", ticker, horizon_name, manifest["snapshot_id"],
        status="success" if probability is not None else "insufficient_data",
        prob_up=probability,
        expected_return=current_row.get("mean_forward_return") if probability is not None else None,
        evidence=evidence,
        validation={
            "method": "drift-shrunk causal base rate on a prefix walk-forward ledger; fixed 52-week window; no fitted calibrator",
            "model_identity": identity,
            "ledger_scope": "prefix_walk_forward",
            "minimum_matching_samples": MINIMUM_MATCHING_SAMPLES,
            "matching_floor_role": "disclosure_only_not_an_eligibility_gate",
            "oos_scope": "prefix walk-forward within the frozen 52-week snapshot; daily overlapping labels; 6-session sampled subset reported separately",
            "oos_predictions": len(matured_oos),
            "prediction_days": len(matured_oos),
            "oos_evaluable_samples": len(oos_forecasts),
            "abstained_predictions": len(oos_forecasts) - len(matured_oos),
            "non_overlapping_prediction_count": len(non_overlapping(predicted_decisions, horizon)),
            "sampled_predictions": len(sample_rows),
            "sampling": nonsampled_sampling(nonsampled),
            "oos_coverage": len(matured_oos) / len(oos_forecasts) if oos_forecasts else 0.0,
            "oos_abstain_rate": 1 - len(matured_oos) / len(oos_forecasts) if oos_forecasts else None,
            "oos_forecasts": oos_forecasts,
            "brier": effectiveness.get("brier_model"),
            "effectiveness": effectiveness,
            "non_overlapping_training_labels": current_row.get("matching_observations"),
            "n_raw": current_row.get("matching_observations") if probability is not None else None,
            "n_effective": current_row.get("n_eff") if probability is not None else None,
            "base_rate_effective_samples": current_row.get("n_eff") if probability is not None else None,
            "prior_weight_share": (
                DRIFT_SHRINKAGE_K / (current_row["n_eff"] + DRIFT_SHRINKAGE_K)
                if probability is not None else None
            ),
            "evidence_power": {
                "prospective_floor": MIN_PROSPECTIVE_NONOVERLAP,
                "curve": power_curve((MIN_PROSPECTIVE_NONOVERLAP, 60, 90, 120, 250)),
                "interpretation": "pass probability of a binomial one-sided 5% test at a 50% null",
            },
            "n_oos": len(matured_oos),
            "history_span_sessions": current - min(
                (int(index) for index in range(len(dates)) if dates[index] in set(transform["matching_origins"])),
                default=current,
            ),
            "prediction_decision_session": dates[current],
            "probability_transform": transform,
            "prior_policy": {
                "method": "shrinkage of the causal base rate toward a volatility-calibrated drift prior",
                "k": DRIFT_SHRINKAGE_K,
                "mu_annual": DRIFT_MU_ANNUAL,
                "vol_window_sessions": DRIFT_VOL_WINDOW_SESSIONS,
                "time_decay": "none",
                "tuned_on_this_window": False,
            },
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0 on split-adjusted closes",
            "target_version": identity["target_version"],
            "label_basis": dividend_label_disclosure(),
            "price_basis": price_provenance,
            "leakage_audit": {
                "features_use_data_through_decision_close_only": True,
                "future_returns_used_only_as_labels": True,
                "training_labels_matured_by_decision": True,
                "base_rate_labels_matured_by_decision": True,
                "matching_gap_sessions": NONOVERLAP_GAP_SESSIONS,
                "oos_labels_overlap_disclosure": f"daily prefix ledger; adjacent {horizon}-session labels overlap",
            },
            "momentum_20d": momentum_block,
        },
        data_used=["daily split-adjusted close (provider close; dividends not reinvested)"],
        warnings=warnings,
        probability_source="drift_shrunk_causal_base_rate" if probability is not None else None,
        raw_probability=current_row.get("raw_probability") if probability is not None else None,
    )


def nonsampled_sampling(block: dict[str, Any]) -> dict[str, Any]:
    sampling = dict(block.get("sampling") or {})
    sampling.update({
        "total_predictions": block.get("total_predictions"),
        "matured_predictions": block.get("matured_predictions"),
        "nonoverlap_predictions": block.get("nonoverlap_predictions"),
        "coverage_ratio": block.get("coverage_ratio"),
    })
    return sampling
