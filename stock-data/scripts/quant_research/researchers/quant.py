"""Research A: hypothesis-led empirical conditional-frequency baseline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import forward_returns, market_bars, non_overlapping
from ..math_utils import mean, wilson_interval
from ..snapshot import load_snapshot


def _momentum(values: list[float], index: int, window: int) -> float | None:
    if index < window or values[index - window] <= 0:
        return None
    return values[index] / values[index - window] - 1.0


def run(snapshot_dir: Path) -> dict[str, Any]:
    manifest, market = load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    horizon = HORIZONS[horizon_name]
    bars = market_bars(market)
    closes = [row["close"] for row in bars]
    forward = forward_returns(closes, horizon)
    current = len(bars) - 1
    current_signal = _momentum(closes, current, 20)
    if current_signal is None:
        return researcher_result("quant", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", warnings=["At least 21 daily closes are required for the predeclared 20-session momentum hypothesis."])

    matching: list[int] = []
    for index in range(20, current):
        signal = _momentum(closes, index, 20)
        if signal is not None and forward[index] is not None and (signal >= 0) == (current_signal >= 0):
            matching.append(index)
    selected = non_overlapping(matching, horizon)
    outcomes = [float(forward[index]) for index in selected if forward[index] is not None]
    wins = sum(value > 0 for value in outcomes)
    probability = wins / len(outcomes) if outcomes else None
    interval = wilson_interval(wins, len(outcomes)) if outcomes else None
    expected = mean(outcomes)

    # Expanding conditional-frequency predictions on the latest chronological holdout.
    holdout_start = max(20 + horizon, int(len(bars) * 0.8))
    oos_predictions: list[tuple[float, int]] = []
    for decision in range(holdout_start, current):
        if forward[decision] is None:
            continue
        candidates = []
        for prior in range(20, decision):
            prior_signal = _momentum(closes, prior, 20)
            if prior_signal is None or forward[prior] is None or prior + horizon > decision:
                continue
            if (prior_signal >= 0) == (_momentum(closes, decision, 20) >= 0):
                candidates.append(prior)
        candidates = non_overlapping(candidates, horizon)
        prior_outcomes = [float(forward[i]) for i in candidates if forward[i] is not None]
        if len(prior_outcomes) >= 20:
            oos_predictions.append((sum(value > 0 for value in prior_outcomes) / len(prior_outcomes), int(float(forward[decision]) > 0)))
    brier = mean([(p - y) ** 2 for p, y in oos_predictions])
    baseline_indices = non_overlapping([i for i in range(20, holdout_start) if i + horizon < holdout_start and forward[i] is not None], horizon)
    base_rate = mean([float(forward[i] > 0) for i in baseline_indices if forward[i] is not None])
    base_brier = mean([(base_rate - y) ** 2 for _, y in oos_predictions]) if base_rate is not None else None
    warnings: list[str] = []
    warnings.append("Returns are gross close-to-close observations; this baseline does not model execution, spreads, slippage or fees.")
    status = "success"
    if len(outcomes) < 30:
        probability = None
        expected = None
        status = "insufficient_data"
        warnings.append(f"Only {len(outcomes)} non-overlapping historical matches; at least 30 are required to report a conditional probability.")
    if not oos_predictions:
        status = "partial" if probability is not None else "insufficient_data"
        warnings.append("No sufficiently populated expanding-window holdout predictions were available.")
    if current_signal >= 0:
        rationale = "The predeclared 20-session momentum signal is non-negative. Historical labels are selected only from earlier sessions with the same signal sign."
    else:
        rationale = "The predeclared 20-session momentum signal is negative. Historical labels are selected only from earlier sessions with the same signal sign."
    evidence = {"positive": [], "negative": []}
    if probability is not None:
        target = "positive" if probability >= 0.5 else "negative"
        evidence[target].append({"family": "momentum", "hypothesis": "20-session momentum sign conditions the H-session return hit rate", "signal_value": current_signal, "empirical_hit_rate": probability, "non_overlapping_samples": len(outcomes), "wilson_95_interval": list(interval) if interval else None, "mean_forward_return": expected, "rationale": rationale})
    if manifest.get("data_quality", {}).get("overall") == "low":
        warnings.append("Snapshot data quality is low; treat this result as exploratory.")
    return researcher_result(
        "quant", ticker, horizon_name, manifest["snapshot_id"], status=status,
        prob_up=probability, expected_return=expected, evidence=evidence,
        validation={"method": "expanding conditional-frequency holdout", "holdout_start_session": bars[holdout_start]["date"] if holdout_start < len(bars) else None, "oos_predictions": len(oos_predictions), "brier": brier, "base_rate_brier": base_brier, "non_overlapping_training_labels": len(outcomes), "leakage_audit": {"features_use_data_through_decision_close_only": True, "future_returns_used_only_as_labels": True, "training_labels_non_overlapping": True}},
        data_used=["daily raw OHLCV close"], warnings=warnings,
        probability_source="non_overlapping_historical_conditional_frequency" if probability is not None else None,
    )
