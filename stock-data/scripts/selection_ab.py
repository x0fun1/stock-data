"""Compare the two Research B factor-selection rules on a registered pool.

The question is narrow on purpose: does keeping every matured selection label and reporting
a block-bootstrap interval (``overlapping_block``) produce better out-of-sample probability
scores than the spaced point-estimate rule (``non_overlapping_point``)? Nothing else changes,
because both arms run the same production function with the same data and the same evaluation
boundary; only the selection estimator differs.

The pool shares one trading calendar, so dependence is handled by resampling session blocks
and averaging across symbols inside each block. Treating symbol-days as independent would
understate the interval, because the cross-section moves together.

The decision rule is in ``PREREGISTRATION`` and is written into the report verbatim.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quant_research.contracts import normalize_request
from quant_research.data_checks import check_market
from quant_research.researchers import factor as factor_path

PREREGISTRATION: dict[str, Any] = {
    "question": (
        "Does dependency-aware factor selection (overlapping labels with a moving-block "
        "interval plus a direction-stability requirement) change out-of-sample Brier "
        "versus the spaced point-estimate rule?"
    ),
    "arms": {"A": "non_overlapping_point", "B": "overlapping_block"},
    "horizon": "5D",
    "primary_metric": "mean_per_session_brier_delta_B_minus_A",
    "bootstrap": {"unit": "session_block", "block_length": "horizon + 1", "replicates": 500, "seed": 0},
    "decision": {
        "B_better": "interval entirely below zero",
        "A_better": "interval entirely above zero",
        "indistinguishable": "interval contains zero (the expected outcome on this sample)",
    },
    "pairing": "decision_session, only sessions where both arms produced a prediction",
    "coverage": "reported, never used to discard hard sessions from the score comparison",
    "default_if_indistinguishable": "keep the incumbent rule",
}


def _load_pool(pool_path: Path, collections: Path, *, asof: str) -> list[dict[str, Any]]:
    document = json.loads(pool_path.read_text())
    entries = document["symbols"] if isinstance(document, dict) else document
    pool: list[dict[str, Any]] = []
    for entry in entries:
        symbol = entry["symbol"] if isinstance(entry, dict) else str(entry)
        asset_type = entry.get("asset_type", "equity") if isinstance(entry, dict) else "equity"
        path = collections / f"{symbol}.json"
        if not path.exists():
            continue
        market = json.loads(path.read_text())["market"]
        bars = market.get("data", {}).get("bars") or []
        if len(bars) < 200:
            continue
        request = normalize_request({
            "ticker": symbol,
            "market": "US",
            "horizon": "5D",
            "asof": asof,
            "mode": "standard",
            "intent": "forecast",
            "asset_type": "etf" if asset_type != "equity" else "stock",
        })
        captured = market.get("fetched_at_utc")
        if not captured:
            continue
        gate = check_market(market, request, captured_at=captured, allow_library_calendar=True)
        if gate.get("errors"):
            continue
        market["_data_validation"] = gate
        if not gate.get("forecast_eligible", False):
            continue
        pool.append({"symbol": symbol, "asset_type": asset_type, "market": market, "bar_count": len(bars)})
    return pool


def _forecasts(result: dict[str, Any]) -> dict[str, tuple[float, int]]:
    out: dict[str, tuple[float, int]] = {}
    for record in result["validation"].get("oos_forecasts") or []:
        probability = record.get("probability")
        target = record.get("target")
        session = record.get("decision_session")
        if probability is None or target is None or session is None:
            continue
        out[str(session)] = (float(probability), int(target))
    return out


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs) if pairs else None


def _session_block_interval(series: list[float], *, block_length: int, replicates: int = 500, seed: int = 0) -> dict[str, Any]:
    count = len(series)
    if count < max(20, 4 * block_length):
        return {"status": "insufficient_samples", "interval": None, "observations": count}
    rng = random.Random(seed)  # noqa: S311 - seeded statistical bootstrap
    means: list[float] = []
    while len(means) < replicates:
        sample: list[float] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(series[(start + offset) % count] for offset in range(block_length))
        sample = sample[:count]
        means.append(sum(sample) / len(sample))
    means.sort()
    return {
        "status": "exploratory",
        "observations": count,
        "block_length": block_length,
        "replicates": len(means),
        "interval": [means[int(0.025 * (len(means) - 1))], means[int(0.975 * (len(means) - 1))]],
        "coverage_validated": False,
    }


def _symbol_cluster_interval(values: list[float], *, replicates: int = 500, seed: int = 0) -> dict[str, Any]:
    """Resample symbols, not symbol-days.

    This answers "would the rule travel to other names", not "is the difference real on
    this calendar". The two questions differ because the cross-section shares a market
    factor, so the interval here is narrower than a defensible time-series interval and is
    reported as disclosure only, never as the decision input.
    """
    count = len(values)
    if count < 8:
        return {"status": "insufficient_samples", "interval": None, "observations": count}
    rng = random.Random(seed)  # noqa: S311 - seeded statistical bootstrap
    means: list[float] = []
    for _ in range(replicates):
        sample = [values[rng.randrange(count)] for _ in range(count)]
        means.append(sum(sample) / count)
    means.sort()
    return {
        "status": "exploratory",
        "observations": count,
        "replicates": len(means),
        "interval": [means[int(0.025 * (len(means) - 1))], means[int(0.975 * (len(means) - 1))]],
        "unit": "symbol_cluster",
        "understates_cross_sectional_dependence": True,
        "used_for_decision": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--collections-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--horizon", default="5D")
    parser.add_argument("--asof", default="2026-10-18T17:00:00Z")
    args = parser.parse_args()

    pool = _load_pool(Path(args.pool), Path(args.collections_dir), asof=args.asof)
    horizon_sessions = {"1D": 1, "5D": 5, "20D": 20}[args.horizon]
    block_length = horizon_sessions + 1

    per_symbol: list[dict[str, Any]] = []
    deltas_by_session: dict[str, list[float]] = defaultdict(list)
    pairs_a: list[tuple[float, int]] = []
    pairs_b: list[tuple[float, int]] = []
    for entry in pool:
        manifest = {"ticker": entry["symbol"], "horizon": args.horizon, "snapshot_id": f"selection-ab:{entry['symbol']}"}
        loaded = (manifest, entry["market"])
        record: dict[str, Any] = {"symbol": entry["symbol"], "asset_type": entry["asset_type"], "bar_count": entry["bar_count"]}
        arms: dict[str, dict[str, tuple[float, int]]] = {}
        for arm, metric in PREREGISTRATION["arms"].items():
            result = factor_path.run(Path("."), loaded, selection_override=metric)
            arms[arm] = _forecasts(result)
            record[f"{arm}_coverage"] = len(arms[arm])
            record[f"{arm}_status"] = result.get("status")
            record[f"{arm}_prob_up"] = result.get("prob_up")
            record[f"{arm}_factors"] = [item["factor_name"] for item in result["validation"]["selected_factors"]]
        common = sorted(set(arms["A"]) & set(arms["B"]))
        record["common_dates"] = len(common)
        if common:
            values_a = [arms["A"][session] for session in common]
            values_b = [arms["B"][session] for session in common]
            record["brier_A"] = _brier(values_a)
            record["brier_B"] = _brier(values_b)
            record["delta_B_minus_A"] = (record["brier_B"] - record["brier_A"])
            pairs_a.extend(values_a)
            pairs_b.extend(values_b)
            for session in common:
                probability_a, outcome = arms["A"][session]
                probability_b, _ = arms["B"][session]
                deltas_by_session[session].append((probability_b - outcome) ** 2 - (probability_a - outcome) ** 2)
        per_symbol.append(record)

    session_series = [sum(values) / len(values) for _, values in sorted(deltas_by_session.items()) if values]
    symbol_series = [item["delta_B_minus_A"] for item in per_symbol if "delta_B_minus_A" in item]
    pooled_delta = sum(session_series) / len(session_series) if session_series else None
    interval = _session_block_interval(session_series, block_length=block_length)
    low, high = (interval.get("interval") or [None, None])
    if low is None:
        verdict = "indeterminate"
    elif high < 0:
        verdict = "new_selection_better"
    elif low > 0:
        verdict = "incumbent_selection_better"
    else:
        verdict = "indistinguishable"

    report = {
        "preregistration": PREREGISTRATION,
        "pool": {
            "symbols": len(pool),
            "equities": sum(1 for item in pool if item["asset_type"] in {"stock", "equity"}),
            "etfs": sum(1 for item in pool if item["asset_type"] not in {"stock", "equity"}),
            "shared_calendar": True,
        },
        "pooled": {
            "scored_pairs_arm_A": len(pairs_a),
            "scored_pairs_arm_B": len(pairs_b),
            "brier_arm_A": _brier(pairs_a),
            "brier_arm_B": _brier(pairs_b),
            "mean_per_session_delta_B_minus_A": pooled_delta,
            "session_block_interval": interval.get("interval"),
            "session_block_interval_status": interval.get("status"),
            "sessions_scored": len(session_series),
            "delta_definition": "positive means the new rule scored worse",
        },
        "secondary_disclosure": {
            "symbol_cluster": _symbol_cluster_interval(symbol_series),
            "symbols_scored": len(symbol_series),
            "note": (
                "Answers a different question than the registered test and is reported only so "
                "the reader can see whether the sign is stable across names."
            ),
        },
        "decision": {
            "verdict": verdict,
            "rule": PREREGISTRATION["decision"],
            "applies_to": "this registered pool and this horizon only",
        },
        "per_symbol": per_symbol,
        "limitations": [
            "Both arms share the same 52-week window per symbol; this compares selection estimators, not added history.",
            "The pool shares one market factor, so session-block resampling is the honest unit and the interval is wide by design.",
            "Brier on ten to twenty forecasts per symbol is a coarse instrument; the pooled figure is what the decision uses.",
            "No calibration, no trading cost and no position sizing are evaluated here.",
        ],
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "selection_ab.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    lines = [
        "# Research B selection-rule comparison",
        "",
        f"- pool: {report['pool']['symbols']} symbols "
        f"({report['pool']['equities']} equities, {report['pool']['etfs']} ETFs), shared calendar",
        f"- horizon: {args.horizon}; block length: {block_length} sessions",
        f"- scored pairs: arm A {len(pairs_a)}, arm B {len(pairs_b)}",
        f"- Brier arm A (spaced point estimate): {report['pooled']['brier_arm_A']}",
        f"- Brier arm B (overlapping block): {report['pooled']['brier_arm_B']}",
        f"- mean per-session delta (B - A): {pooled_delta}",
        f"- session-block 95% interval: {interval.get('interval')} ({interval.get('status')})",
        f"- **verdict: {verdict}**",
        "",
        "Positive delta means the new rule scored worse. The interval is the decision input;",
        "the point estimate is reported for completeness only.",
        "",
        "## Preregistration",
        "",
        "```json",
        json.dumps(PREREGISTRATION, indent=2, sort_keys=True),
        "```",
    ]
    (output / "selection_ab.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({key: report[key] for key in ("pool", "pooled", "decision")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
