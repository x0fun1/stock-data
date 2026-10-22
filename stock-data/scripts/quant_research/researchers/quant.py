"""Research A: hypothesis-led empirical conditional-frequency baseline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import adjusted_close_series, forward_returns, market_bars, non_overlapping
from ..math_utils import mean, wilson_interval
from ..momentum import compute as compute_momentum
from ..momentum import rank_ic_diagnostic
from ..momentum import roc as momentum_roc
from ..snapshot import load_snapshot

# Minimum non-overlapping same-sign matured matches required to report a conditional
# probability; calibrated so that the one-year default research window can support the
# path (the previous value of 30 implicitly required roughly two years of sessions).
MINIMUM_MATCHING_SAMPLES = 12

# Pre-registered shrinkage candidate for Research A: the conditional frequency is
# pulled toward the causal unconditional up-rate with 12 prior observations. The
# prior is a fixed design choice, not a value tuned on this repository's results.
SHRINKAGE_PRIOR_OBSERVATIONS = 12


def _causal_up_rate(forward: list[float | None], decision: int, horizon: int) -> float | None:
    matured = [
        value for index, value in enumerate(forward[: max(0, decision - horizon + 1)])
        if value is not None
    ]
    return sum(1 for value in matured if value > 0) / len(matured) if matured else None


def _shrunk_probability(wins: int, observations: int, base_rate: float | None) -> float | None:
    if base_rate is None or observations <= 0:
        return None
    return (wins + SHRINKAGE_PRIOR_OBSERVATIONS * base_rate) / (observations + SHRINKAGE_PRIOR_OBSERVATIONS)


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


def _matured_matching_indices(
    closes: list[float], forward: list[float | None], decision: int, horizon: int, *, window: int = 20,
) -> list[int]:
    """Return only prior same-signal samples whose complete labels are observable at decision close."""
    current_signal = momentum_roc(closes, decision, window)
    if current_signal is None:
        return []
    matching: list[int] = []
    for prior in range(window, min(decision, len(forward))):
        label_end = prior + horizon
        if label_end > decision or forward[prior] is None:
            continue
        prior_signal = momentum_roc(closes, prior, window)
        if prior_signal is not None and (prior_signal >= 0) == (current_signal >= 0):
            matching.append(prior)
    return non_overlapping(matching, horizon)


def run(snapshot_dir: Path, loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    # The orchestrator may pass the once-validated in-memory snapshot. Keep the
    # path-only entrypoint for direct use and backward compatibility.
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    horizon = HORIZONS[horizon_name]
    bars = market_bars(market)
    closes = [row["close"] for row in bars]
    # Composite momentum feature: computed on the adjusted basis and reported next to the
    # baseline. It never touches ``prob_up`` -- the plan forbids using the score as a new
    # sample filter, so the probability still comes from the plain ROC20 sign.
    momentum_dates, momentum_closes, price_provenance = adjusted_close_series(market)
    benchmark_closes, benchmark_meta = _benchmark_series(market)
    forward = forward_returns(closes, horizon)
    current = len(bars) - 1
    current_signal = momentum_roc(closes, current, 20)
    if current_signal is None:
        return researcher_result("quant", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", warnings=["At least 21 daily closes are required for the predeclared 20-session momentum hypothesis."])

    selected = _matured_matching_indices(closes, forward, current, horizon)
    outcomes = [float(forward[index]) for index in selected if forward[index] is not None]
    wins = sum(value > 0 for value in outcomes)
    raw_probability = wins / len(outcomes) if outcomes else None
    current_base_rate = _causal_up_rate(forward, current, horizon)
    probability = (
        _shrunk_probability(wins, len(outcomes), current_base_rate)
        if raw_probability is not None else None
    )
    interval = wilson_interval(wins, len(outcomes)) if outcomes else None
    expected = mean(outcomes)

    # Full-history daily causal walk-forward ledger: every matured decision session records what
    # the frozen policy would have predicted at that close, rebuilding its matching history from
    # that boundary. Adjacent horizon labels overlap across sessions; conservative non-overlapping
    # counts and block-based inference treat the ledger accordingly (see validation metadata).
    holdout_start = max(20 + horizon, int(len(bars) * 0.8))
    oos_forecasts: list[dict[str, Any]] = []
    ledger_decisions = [i for i in range(20 + horizon, current) if forward[i] is not None and i + horizon <= current]
    predicted_decisions: list[int] = []
    for decision in ledger_decisions:
        candidates = _matured_matching_indices(closes, forward, decision, horizon)
        prior_outcomes = [float(forward[i]) for i in candidates if forward[i] is not None]
        probability_at_decision = (
            sum(value > 0 for value in prior_outcomes) / len(prior_outcomes)
            if len(prior_outcomes) >= MINIMUM_MATCHING_SAMPLES else None
        )
        training_label_end = max((i + horizon for i in candidates), default=None)
        causal_base = _causal_up_rate(forward, decision, horizon)
        shadow_shrunk = _shrunk_probability(
            sum(1 for value in prior_outcomes if value > 0), len(prior_outcomes), causal_base,
        )
        reported_at_decision = shadow_shrunk if probability_at_decision is not None else None
        if reported_at_decision is not None:
            predicted_decisions.append(decision)
        oos_forecasts.append({
            "decision_session": bars[decision]["date"],
            "label_end_session": bars[decision + horizon]["date"],
            "target": int(float(forward[decision]) > 0),
            "probability": reported_at_decision,
            "raw_probability": probability_at_decision,
            "shrinkage_applied": reported_at_decision is not None,
            "training_label_end_session": bars[training_label_end]["date"] if training_label_end is not None else None,
            "training_sample_count": len(prior_outcomes),
            "causal_base_rate": causal_base,
            "shadow_shrunk_probability": shadow_shrunk,
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0",
        })
    oos_predictions = [
        (float(row["probability"]), int(row["target"]))
        for row in oos_forecasts if row["probability"] is not None
    ]
    brier = mean([(p - y) ** 2 for p, y in oos_predictions])
    base_rate = mean([float(value > 0) for value in outcomes]) if outcomes else None
    base_brier = mean([(base_rate - y) ** 2 for _, y in oos_predictions]) if base_rate is not None else None
    warnings: list[str] = []
    warnings.append("Returns are gross close-to-close observations; this baseline does not model execution, spreads, slippage or fees.")
    status = "success"
    if len(outcomes) < MINIMUM_MATCHING_SAMPLES:
        probability = None
        expected = None
        status = "insufficient_data"
        warnings.append(f"Only {len(outcomes)} non-overlapping historical matches; at least {MINIMUM_MATCHING_SAMPLES} are required to report a conditional probability.")
    shadow_shrunk_probability = (
        _shrunk_probability(sum(1 for value in outcomes if value > 0), len(outcomes), current_base_rate)
        if len(outcomes) >= MINIMUM_MATCHING_SAMPLES else None
    )
    if not oos_predictions:
        status = "partial" if probability is not None else "insufficient_data"
        warnings.append("No sufficiently populated expanding-window ledger predictions were available.")
    warnings.append("The walk-forward ledger records daily overlapping predictions; effective independent samples are fewer than prediction days and skill intervals use block resampling.")
    if current_signal >= 0:
        rationale = "The predeclared 20-session momentum signal is non-negative. Historical labels are selected only from earlier sessions with the same signal sign."
    else:
        rationale = "The predeclared 20-session momentum signal is negative. Historical labels are selected only from earlier sessions with the same signal sign."
    evidence = {"positive": [], "negative": []}
    if probability is not None:
        target = "positive" if probability >= 0.5 else "negative"
        evidence[target].append({"family": "momentum", "hypothesis": "20-session momentum sign conditions the H-session return hit rate", "signal_value": current_signal, "empirical_hit_rate": raw_probability, "shrunk_probability": probability, "probability_basis": "empirical_bayes_shrunk_conditional_frequency", "non_overlapping_samples": len(outcomes), "n_raw": len(outcomes), "n_effective": len(outcomes), "wilson_95_interval": list(interval) if interval else None, "wilson_interval_method": "Wilson score; nominal IID approximation, time-series coverage not validated", "wilson_coverage_validated": False, "mean_forward_return": expected, "rationale": rationale})
    if manifest.get("data_quality", {}).get("overall") == "low":
        warnings.append("Snapshot data quality is low; treat this result as exploratory.")
    momentum_block = compute_momentum(
        momentum_closes,
        momentum_dates,
        benchmark_closes,
        symbol=ticker,
        benchmark_symbol=benchmark_meta.get("symbol"),
        benchmark_selection=benchmark_meta.get("selection", "absent"),
        price_field=price_provenance["price_field"],
    )
    momentum_block["price_basis"] = price_provenance
    momentum_block["benchmark"]["status"] = benchmark_meta.get("status")
    if price_provenance["basis_status"] == "unverified":
        momentum_block["warnings"].append("price_basis_unverified")
    momentum_block["diagnostic_rank_ic"] = rank_ic_diagnostic(
        momentum_closes, momentum_dates, benchmark_closes, horizon=horizon
    )
    data_used = ["daily raw OHLCV close"]
    if benchmark_closes is not None:
        data_used.append(f"benchmark adjusted close ({benchmark_meta.get('symbol')})")
    return researcher_result(
        "quant", ticker, horizon_name, manifest["snapshot_id"], status=status,
        prob_up=probability, expected_return=expected, evidence=evidence,
        validation={
            "method": "full-history daily causal walk-forward ledger on the frozen snapshot; expanding conditional frequency; no Bayesian prior or time decay",
            "minimum_matching_samples": MINIMUM_MATCHING_SAMPLES,
            "holdout_start_session": bars[holdout_start]["date"] if holdout_start < len(bars) else None,
            "oos_scope": "full-history daily walk-forward ledger; adjacent labels overlap; non-overlapping count and effective size are the conservative references for thresholds; skill intervals use block resampling",
            "oos_predictions": len(oos_predictions),
            "prediction_days": len(oos_predictions),
            "oos_evaluable_samples": len(oos_forecasts),
            "non_overlapping_prediction_count": len(non_overlapping(predicted_decisions, horizon)),
            "effective_sample_size_estimate": round(len(oos_predictions) / (horizon + 1)),
            "oos_coverage": len(oos_predictions) / len(oos_forecasts) if oos_forecasts else 0.0,
            "oos_abstain_rate": 1 - len(oos_predictions) / len(oos_forecasts) if oos_forecasts else None,
            "oos_forecasts": oos_forecasts,
            "brier": brier,
            "base_rate_brier": base_brier,
            "non_overlapping_training_labels": len(outcomes),
            "n_raw": len(outcomes),
            "n_effective": len(outcomes),
            "n_oos": len(oos_predictions),
            "history_span_sessions": current - selected[0] if selected else 0,
            "training_label_end_boundary": bars[max((i + horizon for i in selected), default=current)]["date"] if selected else None,
            "prediction_decision_session": bars[current]["date"],
            "prior_policy": {
                "method": "empirical-Bayes shrinkage toward the causal unconditional up-rate",
                "bayesian_shrinkage": "implemented",
                "prior_observations": SHRINKAGE_PRIOR_OBSERVATIONS,
                "time_decay": "none",
            },
            "quant_shrinkage_candidate": {
                "used_for_reported_probability": True,
                "status": "applied" if shadow_shrunk_probability is not None else "abstained",
                "prior_observations": SHRINKAGE_PRIOR_OBSERVATIONS,
                "causal_base_rate": current_base_rate,
                "observations": len(outcomes),
                "wins": sum(1 for value in outcomes if value > 0),
                "incumbent_probability": raw_probability,
                "candidate_probability": shadow_shrunk_probability,
            },
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0",
            "leakage_audit": {"features_use_data_through_decision_close_only": True, "future_returns_used_only_as_labels": True, "training_labels_non_overlapping": True, "training_labels_matured_by_decision": True, "oos_labels_non_overlapping": False, "oos_labels_overlap_disclosure": f"daily walk-forward ledger; adjacent {horizon}-session labels overlap; effective independent size is smaller than the prediction-day count"},
            "momentum_20d": momentum_block,
        },
        data_used=data_used, warnings=warnings,
        probability_source="empirical_bayes_shrunk_conditional_frequency" if probability is not None else None,
        raw_probability=raw_probability if probability is not None else None,
    )
