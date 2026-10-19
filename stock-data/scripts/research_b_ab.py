"""Pre-registered Research B candidate comparison on a fixed pool.

Two independent questions, declared before any result is read:

  Q1 selection  P (legacy 5-factor, spaced IC)          vs  S (restricted 3-factor,
                                                             one per family, overlapping IC)
  Q2 buckets    P (three buckets, terciles)             vs  B (two buckets, median split)

The production arm P is the reference for both. Arms share the same frozen bars, the same
horizon, the same anchored ledger phase and the same qualification floors, so a difference
cannot come from a different sample. Scoring uses the paired per-session Brier difference
against the same-date historical base rate, resampled in session blocks.

Nothing here switches production. A candidate is only eligible to be considered for a
switch after its interval clears zero on the pre-declared date minimum in a future run;
this run is expected to be underpowered and that is a valid answer, not a failure.
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
from quant_research.market_data import forward_returns
from quant_research.researchers import factor as factor_path

PREREGISTRATION: dict[str, Any] = {
    "protocol": "research-b-candidates-v1",
    "registered": "2026-10-19",
    "reference_arm": "production_legacy_three",
    "arms": {
        "production_legacy_three": {"selection_profile": "legacy", "bucket_mode": "three"},
        "restricted_selection_three": {"selection_profile": "restricted", "bucket_mode": "three"},
        "legacy_two_bucket": {"selection_profile": "legacy", "bucket_mode": "two"},
        "restricted_two_bucket": {"selection_profile": "restricted", "bucket_mode": "two"},
    },
    "questions": {
        "Q1_selection": ("production_legacy_three", "restricted_selection_three"),
        "Q2_bucket": ("production_legacy_three", "legacy_two_bucket"),
    },
    "metric": "mean_paired_per_session_brier_delta_candidate_minus_reference",
    "bootstrap": {"unit": "session_block", "block_length": "horizon + 1", "replicates": 500, "seed": 0},
    "minimum_scorable_sessions_for_interval": 24,
    "decision": {
        "candidate_better": "whole interval below zero",
        "candidate_worse": "whole interval above zero",
        "indeterminate": "interval contains zero, or too few scorable sessions",
    },
    "switch_policy": "no switch this run; a switch requires a pre-registered re-run that clears the rule",
    "thresholds_are_not_adjusted_after_seeing_results": True,
}


def load_pool(pool_path: Path, collections: Path, *, asof: str) -> list[dict[str, Any]]:
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
            "ticker": symbol, "market": "US", "horizon": "5D", "asof": asof, "mode": "standard",
            "intent": "forecast", "asset_type": "etf" if asset_type != "equity" else "stock",
        })
        captured = market.get("fetched_at_utc")
        if not captured:
            continue
        gate = check_market(market, request, captured_at=captured, allow_library_calendar=True)
        if gate.get("errors") or not gate.get("forecast_eligible", False):
            continue
        market["_data_validation"] = gate
        pool.append({"symbol": symbol, "market": market, "bar_count": len(bars)})
    return pool


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs) if pairs else None


def _session_block_interval(series: list[float], *, block_length: int, replicates: int = 500, seed: int = 0) -> dict[str, Any]:
    count = len(series)
    if count < max(20, 4 * block_length):
        return {"status": "insufficient_samples", "interval": None, "observations": count,
                "required_sessions": max(20, 4 * block_length)}
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
        "status": "exploratory", "observations": count, "block_length": block_length,
        "replicates": len(means),
        "interval": [means[int(0.025 * (len(means) - 1))], means[int(0.975 * (len(means) - 1))]],
        "coverage_validated": False,
    }


def _verdict(record: dict[str, Any]) -> str:
    if record.get("interval_status") != "exploratory":
        return "indeterminate"
    low, high = record["interval"]
    if high < 0:
        return "candidate_better"
    if low > 0:
        return "candidate_worse"
    return "indeterminate"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--collections-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--horizon", default="5D")
    parser.add_argument("--asof", default="2026-10-18T17:00:00Z")
    args = parser.parse_args()

    horizon_sessions = {"1D": 1, "5D": 5, "20D": 20}[args.horizon]
    block_length = horizon_sessions + 1
    pool = load_pool(Path(args.pool), Path(args.collections_dir), asof=args.asof)

    per_symbol: list[dict[str, Any]] = []
    for entry in pool:
        symbol = entry["symbol"]
        bars = entry["market"]["data"]["bars"]
        close = [float(bar["close"]) for bar in bars]
        labels = forward_returns(close, horizon_sessions)
        manifest = {"ticker": symbol, "horizon": args.horizon, "snapshot_id": f"research-b:{symbol}"}
        loaded = (manifest, entry["market"])
        record: dict[str, Any] = {"symbol": symbol, "bar_count": entry["bar_count"]}
        for arm, cfg in PREREGISTRATION["arms"].items():
            result = factor_path.run(Path("."), loaded, selection_profile=cfg["selection_profile"],
                                     bucket_mode=cfg["bucket_mode"])
            validation = result["validation"]
            predictions: dict[str, tuple[float, int]] = {}
            for row in validation.get("oos_forecasts") or []:
                probability = row.get("probability")
                session = row.get("decision_session")
                if probability is None or session is None:
                    continue
                index = next((i for i, bar in enumerate(bars) if bar["date"] == session), None)
                if index is None or labels[index] is None:
                    continue
                predictions[session] = (float(probability), int(labels[index] > 0))
            record[arm] = {
                "prob_up": result.get("prob_up"),
                "status": result.get("status"),
                "predictions": len(predictions),
                "brier": _brier(list(predictions.values())),
                "factors": len(validation.get("selected_factors") or []),
                "bucket_counts": validation.get("calibration_bucket_counts"),
                "route": (validation.get("qualification") or {}).get("route"),
            }
            record[f"{arm}__pairs"] = predictions
        per_symbol.append(record)

    comparisons: dict[str, Any] = {}
    for name, (reference, candidate) in PREREGISTRATION["questions"].items():
        deltas: dict[str, list[float]] = defaultdict(list)
        for record in per_symbol:
            ref = record[f"{reference}__pairs"]
            cand = record[f"{candidate}__pairs"]
            for session in sorted(set(ref) & set(cand)):
                ref_probability, outcome = ref[session]
                cand_probability, _ = cand[session]
                deltas[session].append((cand_probability - outcome) ** 2 - (ref_probability - outcome) ** 2)
        series = [sum(values) / len(values) for _, values in sorted(deltas.items()) if values]
        interval = _session_block_interval(series, block_length=block_length)
        comparisons[name] = {
            "reference": reference,
            "candidate": candidate,
            "sessions": len(series),
            "mean_delta": (sum(series) / len(series)) if series else None,
            "interval": interval.get("interval"),
            "interval_status": interval.get("status"),
            "required_sessions": interval.get("required_sessions", max(20, 4 * block_length)),
            "delta_definition": "positive means the candidate scored worse than production",
        }
        comparisons[name]["verdict"] = _verdict(comparisons[name])

    report = {
        "preregistration": PREREGISTRATION,
        "pool": {"symbols": len(pool), "horizon": args.horizon},
        "comparisons": comparisons,
        "production_unchanged": True,
        "per_symbol": [{key: value for key, value in record.items() if not key.endswith("__pairs")}
                       for record in per_symbol],
        "limitations": [
            "Every arm shares the same 52-week window and the same anchored ledger phase; this compares designs, not added history.",
            "The pool shares one market factor, so session-block resampling is the honest unit and the interval is wide by design.",
            "A 52-week ledger yields few scorable sessions; an indeterminate verdict is the expected outcome, not a failure.",
            "No calibration, trading cost, turnover or position sizing is evaluated.",
        ],
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "research_b_ab.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Research B candidate comparison (pre-registered)",
        "",
        f"- pool: {len(pool)} symbols, horizon {args.horizon}, block length {block_length} sessions",
        "- reference arm: production (legacy selection, three buckets)",
        "",
        "| question | candidate | sessions | mean delta (Brier) | 95% interval | verdict |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, record in comparisons.items():
        interval = record["interval"]
        shown = f"[{interval[0]:+.4f}, {interval[1]:+.4f}]" if interval else "n/a"
        mean = f"{record['mean_delta']:+.4f}" if record["mean_delta"] is not None else "n/a"
        lines.append(f"| {name} | {record['candidate']} | {record['sessions']} | {mean} | {shown} | {record['verdict']} |")
    lines += [
        "",
        f"Interval needs at least {comparisons[next(iter(comparisons))]['required_sessions']} scorable sessions; "
        "below that the verdict is indeterminate by rule.",
        "",
        "Positive delta means the candidate scored worse than production. Production is unchanged either way.",
        "",
        "## Preregistration",
        "",
        "```json",
        json.dumps(PREREGISTRATION, indent=2, sort_keys=True),
        "```",
    ]
    (output / "research_b_ab.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"pool": len(pool), "comparisons": {
        name: {"sessions": record["sessions"], "mean_delta": record["mean_delta"],
               "interval_status": record["interval_status"], "verdict": record["verdict"]}
        for name, record in comparisons.items()
    }}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
