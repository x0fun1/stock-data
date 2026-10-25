"""Pre-registered candidate comparison for the two 52-week research arms.

This script compares frozen candidates against the incumbent on the same decision
dates. It never switches production: the output is evidence for a later decision,
and the decision rule is fixed before the run rather than chosen after seeing the
result.

Arms
----
- ``ml.feature_set``: ``full_v1`` (incumbent, 9 features) vs ``compact_v1``
  (3 features: return_5, return_20, volatility_20). Both use the production replay
  function, the same absolute refit anchors and the same 252-session training cap.
- ``quant.shrinkage``: incumbent conditional frequency vs the pre-registered
  ``(wins + 12 * p0) / (n + 12)`` shrinkage recorded in each Research A OOS row.
  That row-level shadow only exists for dates where the path produced a forecast.

Usage
-----
    python research_candidates.py --snapshot-dir <snapshot_dir> [--snapshot-dir ...]

The primary metric is the paired Brier difference (candidate minus incumbent) on
common decision dates. The interval is a moving-block bootstrap over the actual
trading-date axis (block = horizon + 1), not an IID interval over rows.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from quant_research.contracts import HORIZONS
from quant_research.market_data import market_bars
from quant_research.researchers import ml, quant
from quant_research.snapshot import load_snapshot

MINIMUM_EVALUATION_SESSIONS = 120
RELATIVE_BRIER_IMPROVEMENT = -0.02
MAX_COVERAGE_DROP = 0.05
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20261022

PREREGISTRATION = {
    "primary_metric": "paired Brier difference, candidate minus incumbent, on common decision dates",
    "improvement_rule": (
        f"mean relative Brier difference <= {RELATIVE_BRIER_IMPROVEMENT:.0%} and the 95% moving-block "
        "bootstrap upper bound < 0"
    ),
    "coverage_rule": f"candidate coverage may not fall more than {MAX_COVERAGE_DROP:.0%} below incumbent",
    "minimum_sessions": MINIMUM_EVALUATION_SESSIONS,
    "multiple_comparisons": "two pre-registered arms; no other candidate may be added after seeing results",
    "switch_policy": "never automatic; a supported candidate still requires a separate prospective confirmation",
}


def _brier(probability: float, target: int) -> float:
    return (probability - target) ** 2


def _block_interval(values: list[float], block: int, *,
                    iterations: int = BOOTSTRAP_ITERATIONS, seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    if len(values) < block * 2:
        return {"status": "insufficient_for_block_bootstrap", "observations": len(values)}
    rng = random.Random(seed)  # noqa: S311 - deterministic bootstrap, not a security primitive
    samples: list[float] = []
    for _ in range(iterations):
        start = rng.randrange(0, max(1, len(values) - block + 1))
        sample = values[start:start + block]
        while len(sample) < len(values):
            start = rng.randrange(0, max(1, len(values) - block + 1))
            sample.extend(values[start:start + block])
        samples.append(sum(sample) / len(sample))
    samples.sort()
    return {
        "status": "estimated",
        "observations": len(values),
        "block_sessions": block,
        "iterations": iterations,
        "mean": sum(values) / len(values),
        "low": samples[int(0.025 * len(samples))],
        "high": samples[int(0.975 * len(samples)) - 1],
    }


def _verdict(paired: list[dict[str, Any]], incumbent_brier: float, coverage_delta: float) -> dict[str, Any]:
    if len(paired) < MINIMUM_EVALUATION_SESSIONS:
        return {
            "status": "indeterminate",
            "reason": f"{len(paired)} common scoring sessions < pre-registered {MINIMUM_EVALUATION_SESSIONS}",
        }
    differences = [row["difference"] for row in paired]
    interval = _block_interval(differences, block=paired[0]["horizon"] + 1)
    if interval.get("status") != "estimated":
        return {"status": "indeterminate", "reason": interval["status"], "interval": interval}
    relative = interval["mean"] / incumbent_brier if incumbent_brier else None
    supported = (
        relative is not None
        and relative <= RELATIVE_BRIER_IMPROVEMENT
        and interval["high"] < 0
        and coverage_delta >= -MAX_COVERAGE_DROP
    )
    return {
        "status": "candidate_supported" if supported else "not_supported",
        "relative_brier_difference": relative,
        "interval": interval,
        "coverage_delta": coverage_delta,
    }


def compare_ml(bars: list[dict[str, Any]], horizon_sessions: int) -> dict[str, Any]:
    datasets = {name: ml._dataset(
        {"data": {"bars": bars}, "_data_validation": {"forecast_eligible": True}},
        horizon_sessions, feature_set=name,
    )
                for name in ("full_v1", "compact_v1")}
    arms: dict[str, dict[str, dict[str, Any]]] = {}
    coverage: dict[str, float] = {}
    for name, dataset in datasets.items():
        records, _, _ = ml._replay(dataset, horizon_sessions, ml._Fitter())
        arms[name] = {record["decision_session"]: record for record in records}
        actionable = max(1, dataset.n - dataset.warmup - horizon_sessions - ml.MINIMUM_TRAINING_ROWS)
        coverage[name] = len(records) / actionable
    common = sorted(set(arms["full_v1"]) & set(arms["compact_v1"]))
    paired = [
        {
            "decision_session": date,
            "horizon": horizon_sessions,
            "incumbent": arms["full_v1"][date]["probability"],
            "candidate": arms["compact_v1"][date]["probability"],
            "target": arms["full_v1"][date]["target"],
            "difference": _brier(arms["compact_v1"][date]["probability"], arms["full_v1"][date]["target"])
            - _brier(arms["full_v1"][date]["probability"], arms["full_v1"][date]["target"]),
        }
        for date in common
    ]
    incumbent_brier = (
        sum(_brier(row["incumbent"], row["target"]) for row in paired) / len(paired) if paired else 0.0
    )
    return {
        "arm": "ml.feature_set full_v1 -> compact_v1",
        "common_sessions": len(common),
        "incumbent_sessions": len(arms["full_v1"]),
        "candidate_sessions": len(arms["compact_v1"]),
        "coverage": {"incumbent": coverage["full_v1"], "candidate": coverage["compact_v1"]},
        "paired": paired,
        "verdict": _verdict(paired, incumbent_brier, coverage["compact_v1"] - coverage["full_v1"]),
    }


def compare_quant(bars: list[dict[str, Any]], horizon_sessions: int) -> dict[str, Any]:
    horizon_name = next(name for name, value in HORIZONS.items() if value == horizon_sessions)
    manifest = {"ticker": "SYN", "horizon": horizon_name, "snapshot_id": "candidate-eval", "market": "US"}
    market = {"data": {"bars": bars}, "_data_validation": {"forecast_eligible": True}}
    result = quant.run(None, (manifest, market))
    rows = result["validation"].get("oos_forecasts") or []
    index_by_date = {row["date"]: index for index, row in enumerate(bars)}
    paired = [
        {
            "decision_session": row["decision_session"],
            "horizon": horizon_sessions,
            "incumbent": row["raw_probability"],
            "candidate": row["probability"],
            "target": row["target"],
            "difference": _brier(row["probability"], row["target"])
            - _brier(row["raw_probability"], row["target"]),
        }
        for row in rows
        if row.get("raw_probability") is not None
        and row.get("probability") is not None
        and index_by_date.get(row["decision_session"], -1) % (horizon_sessions + 1) == 0
    ]
    incumbent_brier = (
        sum(_brier(row["incumbent"], row["target"]) for row in paired) / len(paired) if paired else 0.0
    )
    return {
        "arm": "quant.shrinkage incumbent -> (wins + 12*p0)/(n+12)",
        "status": result["status"],
        "common_sessions": len(paired),
        "coverage": {"incumbent": 1.0, "candidate": 1.0 if paired else 0.0},
        "paired": paired,
        "verdict": _verdict(paired, incumbent_brier, 0.0),
    }


def evaluate(bars: list[dict[str, Any]], horizon_sessions: int) -> dict[str, Any]:
    return {
        "preregistration": PREREGISTRATION,
        "ml": compare_ml(bars, horizon_sessions),
        "quant": compare_quant(bars, horizon_sessions),
        "switch": False,
        "note": "candidate support is not a production switch; it only justifies a prospective confirmation run",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = []
    for snapshot_dir in args.snapshot_dir:
        manifest, market = load_snapshot(snapshot_dir)
        bars = market_bars(market)
        reports.append({
            "snapshot": str(snapshot_dir),
            "ticker": manifest.get("ticker"),
            "horizon": manifest.get("horizon"),
            **evaluate(bars, HORIZONS[manifest["horizon"]]),
        })
    text = json.dumps(reports, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
