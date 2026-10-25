"""Research A: causal empirical shrinkage on the fixed 52-week / 5-session window.

The estimator is deliberately unchanged from the pre-policy candidate: the state
is the sign of the 20-session rate of change, matching historical origins must
have matured by the decision close and be at least six sessions apart, the prior
strength is 12 and at least 12 matches are required.  What changes is that every
number is now re-derivable from the frozen bars: the probability transform
record carries the exact matching origins, the base-rate label boundary and the
formula version, and the current prediction and every walk-forward row are
produced by the same function.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..contracts import researcher_result
from ..forecast_policy import (
    FORECAST_HORIZON_SESSIONS,
    NONOVERLAP_GAP_SESSIONS,
    SHRINKAGE_MIN_MATCHES,
    SHRINKAGE_PRIOR_STRENGTH,
    SHRINKAGE_VERSION,
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

MINIMUM_MATCHING_SAMPLES = SHRINKAGE_MIN_MATCHES
SHRINKAGE_PRIOR_OBSERVATIONS = SHRINKAGE_PRIOR_STRENGTH
ROC_WINDOW = 20


def _causal_up_rate(forward: list[float | None], decision: int, horizon: int) -> tuple[float | None, list[int]]:
    """Return the causal up-rate and the label origins that matured by ``decision``."""

    origins = [index for index in range(max(0, decision - horizon + 1)) if forward[index] is not None]
    if not origins:
        return None, []
    return sum(1 for index in origins if float(forward[index]) > 0) / len(origins), origins


def _shrunk_probability(wins: int, observations: int, base_rate: float | None) -> float | None:
    if base_rate is None or observations <= 0:
        return None
    return (wins + SHRINKAGE_PRIOR_OBSERVATIONS * base_rate) / (observations + SHRINKAGE_PRIOR_OBSERVATIONS)


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
    gap: int = NONOVERLAP_GAP_SESSIONS,
) -> dict[str, Any]:
    """Produce the frozen-policy prediction for one decision session.

    All inputs are prefix data: the feature uses closes up to ``decision`` and the
    base rate uses only labels that matured at or before that close.  The returned
    ``probability_transform`` is the audit contract; auditors rebuild the sample
    from bars rather than trusting the summary counts.
    """

    if not 0 <= decision < len(closes):
        raise ValueError("decision index outside the price series")
    current_signal = momentum_roc(closes, decision, ROC_WINDOW)
    base_rate, base_origins = _causal_up_rate(forward_returns(closes, horizon), decision, horizon)
    forward = forward_returns(closes, horizon)
    identity = model_identity("quant")
    transform = {
        "type": "causal_empirical_shrinkage",
        "version": SHRINKAGE_VERSION,
        "feature": {"name": "roc20", "window_sessions": ROC_WINDOW, "zero_state": "non_negative"},
        "horizon_sessions": horizon,
        "minimum_gap_sessions": gap,
        "minimum_matches": MINIMUM_MATCHING_SAMPLES,
        "prior_strength": SHRINKAGE_PRIOR_OBSERVATIONS,
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
        "target": int(float(forward[decision]) > 0) if forward[decision] is not None else None,
        "label_end_session": dates[decision + horizon] if decision + horizon < len(dates) else None,
        "status": "abstained",
        "reason_code": None,
        "model_identity": identity,
    }
    if current_signal is None:
        row["reason_code"] = "FEATURE_UNAVAILABLE"
        return row
    if base_rate is None:
        row["reason_code"] = "BASE_RATE_UNAVAILABLE"
        return row
    selected = _matching_indices(closes, forward, decision, horizon, gap=gap)
    transform["matching_origins"] = [dates[index] for index in selected]
    transform["matching_label_ends"] = [dates[index + horizon] for index in selected]
    transform["matching_label_end_max"] = transform["matching_label_ends"][-1] if selected else None
    transform["base_rate_at_origin"] = base_rate
    transform["wins"] = sum(1 for index in selected if float(forward[index]) > 0)
    transform["observations"] = len(selected)
    if len(selected) < MINIMUM_MATCHING_SAMPLES:
        row["reason_code"] = "MATCHING_SAMPLES_INSUFFICIENT"
        row["matching_observations"] = len(selected)
        return row
    probability = _shrunk_probability(transform["wins"], len(selected), base_rate)
    raw = transform["wins"] / len(selected)
    row.update(
        probability=probability,
        raw_probability=raw,
        matching_observations=len(selected),
        matching_wins=transform["wins"],
        status="matured",
        mean_forward_return=mean([float(forward[index]) for index in selected]),
        wilson_95_interval=list(wilson_interval(transform["wins"], len(selected))),
        wilson_interval_scope="raw_matching_hit_frequency_only; overlapping labels, not a confidence interval for the shrunk probability",
    )
    row["available_history_days"] = (
        (date.fromisoformat(dates[decision]) - date.fromisoformat(window_start_exclusive)).days
        if window_start_exclusive else None
    )
    row["available_history_sessions"] = decision + 1
    row["full_52w_input"] = bool(
        window_start_exclusive and row["available_history_days"] is not None and row["available_history_days"] >= 364
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
    window_start = ((market.get("data") or {}).get("policy_window") or {}).get("window_start_exclusive")
    sessions = calendar_session_list(market)
    current = len(bars) - 1
    current_signal = momentum_roc(closes, current, ROC_WINDOW)
    if current_signal is None:
        return researcher_result(
            "quant", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data",
            warnings=["At least 21 daily closes are required for the predeclared 20-session momentum hypothesis."],
        )

    current_row = predict_at_origin(dates, closes, current, horizon=horizon, window_start_exclusive=window_start)
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
        row = predict_at_origin(dates, closes, decision, horizon=horizon, window_start_exclusive=window_start)
        row["label_available_at"] = dates[decision + horizon]
        if row["probability"] is not None:
            predicted_decisions.append(decision)
        oos_forecasts.append(row)

    matured_oos = [row for row in oos_forecasts if row["probability"] is not None]
    nonsampled = evaluation_sample(oos_forecasts, sessions)
    sample_rows = nonsampled["sampled_predictions"]
    brier = mean([(float(row["probability"]) - int(row["target"])) ** 2 for row in matured_oos]) if matured_oos else None
    warnings = [
        "Returns are gross close-to-close observations; this baseline does not model execution, spreads, slippage or fees.",
        "The replay ledger records daily predictions whose 5-session labels overlap; the 6-session sample is a conservative policy, and its records remain correlated.",
    ]
    if probability is None:
        warnings.append(
            f"No reportable probability: {current_row.get('reason_code')} "
            f"({current_row.get('matching_observations', transform.get('observations', 0))} matches, "
            f"{MINIMUM_MATCHING_SAMPLES} required)."
        )
    if not matured_oos:
        warnings.append("No matured walk-forward prediction was available on this prefix.")
    if current_row.get("full_52w_input") is False:
        warnings.append("Current decision does not yet have a full 52-week input window.")
    evidence = {"positive": [], "negative": []}
    if probability is not None:
        target = "positive" if probability >= 0.5 else "negative"
        evidence[target].append({
            "family": "momentum",
            "hypothesis": "20-session momentum sign conditions the 5-session return hit rate",
            "signal_value": current_signal,
            "empirical_hit_rate": current_row["raw_probability"],
            "shrunk_probability": probability,
            "probability_basis": "causal_empirical_shrinkage",
            "probability_transform_version": SHRINKAGE_VERSION,
            "matching_observations": current_row["matching_observations"],
            "matching_wins": current_row["matching_wins"],
            "n_raw": current_row["matching_observations"],
            "n_effective": current_row["matching_observations"],
            "wilson_95_interval": current_row["wilson_95_interval"],
            "wilson_interval_method": "Wilson score on the raw matching hit frequency; nominal IID approximation with overlapping labels; not a confidence interval for the shrunk probability",
            "wilson_coverage_validated": False,
            "mean_forward_return": current_row["mean_forward_return"],
            "rationale": (
                "The predeclared 20-session momentum signal is "
                f"{'non-negative' if current_signal >= 0 else 'negative'}. Historical labels are selected only from "
                "earlier sessions with the same signal sign, matured by the decision close and at least six sessions apart."
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
            "method": "causal empirical shrinkage on a prefix walk-forward ledger; fixed 52-week window; no fitted calibrator",
            "model_identity": identity,
            "ledger_scope": "prefix_walk_forward",
            "minimum_matching_samples": MINIMUM_MATCHING_SAMPLES,
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
            "brier": brier,
            "non_overlapping_training_labels": current_row.get("matching_observations"),
            "n_raw": current_row.get("matching_observations") if probability is not None else None,
            "n_effective": current_row.get("matching_observations") if probability is not None else None,
            "n_oos": len(matured_oos),
            "history_span_sessions": current - min(
                (int(index) for index in range(len(dates)) if dates[index] in set(transform["matching_origins"])),
                default=current,
            ),
            "prediction_decision_session": dates[current],
            "probability_transform": transform,
            "prior_policy": {
                "method": "empirical-Bayes shrinkage toward the causal unconditional up-rate",
                "prior_observations": SHRINKAGE_PRIOR_OBSERVATIONS,
                "time_decay": "none",
                "tuned_on_this_window": False,
            },
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0 on split-adjusted closes",
            "target_version": identity["target_version"],
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
        probability_source="causal_empirical_shrinkage" if probability is not None else None,
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
