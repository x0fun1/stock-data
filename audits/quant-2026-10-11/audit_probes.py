"""Reproduce quant audit findings with synthetic data; never market-performance evidence.

Run from the repository root: python audits/quant-2026-10-11/audit_probes.py
This file does not modify the research runtime or installed skills.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "stock-data" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from research_fixture import collection
from quant_research.audit import audit
from quant_research.consensus import build_consensus, forecast_assessment
from quant_research.market_data import non_overlapping
from quant_research.math_utils import benjamini_yekutieli
from quant_research.researchers import factor, factor_backtest, ml, quant
from quant_research.snapshot import freeze_snapshot, load_snapshot
from quant_research.validation.probability_quality import build_oos_probability_quality


def run_horizon(horizon: str, root: Path) -> dict:
    payload = collection(756)
    payload["request"]["horizon"] = horizon
    source = root / f"collection-{horizon}.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = freeze_snapshot(source, root / "snapshots")
    loaded = load_snapshot(snapshot)
    manifest, market = loaded
    results = [runner.run(snapshot, loaded) for runner in (quant, factor, ml, factor_backtest)]
    for result in results:
        result.update(forecast_assessment(result, manifest))
    consensus = build_consensus(results, manifest)
    audited = audit(manifest, results, consensus)
    active = [result for result in results if result.get("forecast_eligible")]
    active_quality = build_oos_probability_quality(active, manifest)
    factor_validation = results[1]["validation"]
    ml_validation = results[2].get("validation", {})
    ic_rows = factor_validation.get("oos_factor_inference", {}).get("factors", {})
    quant_rows = results[0].get("validation", {}).get("oos_forecasts", [])
    return {
        "input_is_synthetic": True,
        "normalized_bars": len(market["data"]["bars"]),
        "horizon": horizon,
        "paths": {
            result["researcher_id"]: {
                "status": result["status"],
                "current_probability": result.get("prob_up"),
                "forecast_eligible": result.get("forecast_eligible"),
                "oos_count": result.get("oos_validation", {}).get("sample_count"),
            } for result in results
        },
        "factor_calibration_observations": factor_validation.get("calibration_observations"),
        "factor_bucket_counts": factor_validation.get("calibration_bucket_counts"),
        "factor_holdout_predictions": factor_validation.get("holdout_predictions"),
        "ml_calibration_observations": ml_validation.get("probability_calibration", {}).get("calibration_observations"),
        "ml_calibration_status": ml_validation.get("calibration_training", {}).get("status"),
        "quant_oos_predictions_below_current_minimum_30": sum(
            row.get("probability") is not None and row.get("training_sample_count", 0) < 30
            for row in quant_rows
        ),
        "consensus": {
            "path_ids": consensus.get("path_ids"),
            "probability": consensus.get("prob_up"),
            "confidence": consensus.get("confidence"),
            "confidence_score": consensus.get("confidence_score"),
            "confidence_reasons": consensus.get("confidence_reasons"),
            "reporting_status": audited.get("reporting_status"),
            "may_report_direction": audited.get("may_report_direction"),
            "oos_quality": consensus.get("oos_probability_quality"),
            "weight_diagnostic": consensus.get("skill_weight_validation"),
        },
        "active_path_only_oos_quality": active_quality,
        "ic_inference": {
            name: {
                "observations": row["observations"],
                "block_length": row["null_test"]["block_length"],
                "permutations": row["null_test"]["permutations"],
                "p_value": row["p_value"],
                "p_value_adjusted": row["p_value_adjusted"],
                "bootstrap_status": row["block_bootstrap"]["status"],
            } for name, row in ic_rows.items()
        },
    }


def bucket_budget(n: int, horizon: int) -> dict:
    train_end, calibration_end = int(n * .5), int(n * .8)
    indices = non_overlapping(list(range(max(60, train_end), calibration_end - horizon)), horizon)
    m = len(indices)
    # Distinct continuous scores, the ordinary case for the composite.
    low_cut, high_cut = (m - 1) / 3, 2 * (m - 1) / 3
    buckets = {"low": 0, "mid": 0, "high": 0}
    for score in range(m):
        key = "low" if score <= low_cut else "high" if score >= high_cut else "mid"
        buckets[key] += 1
    return {"bars": n, "horizon_sessions": horizon, "calibration_labels": m,
            "distinct_score_bucket_counts": buckets}


def confidence_probe() -> dict:
    """Compare identical predictions/counts with good and poor reported OOS Brier."""
    manifest = {"ticker": "TEST", "horizon": "1D", "snapshot_id": "audit-probe",
                "source_payload_sha256": "synthetic-not-a-feed",
                "data_quality": {"overall": "high", "confidence_score": 100},
                "data_validation": {"forecast_eligible": True,
                                    "prediction_eligibility": {"status": "eligible"}}}
    paths = []
    for name, family in (("quant", "momentum"), ("factor", "volume")):
        paths.append({"researcher_id": name, "ticker": "TEST", "horizon": "1D",
                      "snapshot_id": "audit-probe", "status": "success", "result_role": "forecast",
                      "prob_up": .6, "probability_source": "synthetic_contract_probe",
                      "evidence": {"positive": [{"family": family}], "negative": []},
                      "validation": {"oos_predictions": 100, "holdout_predictions": 100,
                                     "holdout_start_session": "2024-01-02", "brier": .20,
                                     "base_rate_brier": .25,
                                     "leakage_audit": {"features_use_data_through_decision_close_only": True,
                                                       "future_returns_used_only_as_labels": True}}})
    bad = copy.deepcopy(paths)
    for path in bad:
        path["validation"]["brier"] = .35
    outputs = {}
    for label, values in (("beats_baseline", paths), ("fails_baseline", bad)):
        consensus = build_consensus(values, manifest)
        assessed = audit(manifest, values, consensus)
        outputs[label] = {"confidence_score": consensus["confidence_score"],
                          "confidence_label": consensus["confidence"],
                          "audit_reporting_status": assessed["reporting_status"],
                          "audit_reason_codes": assessed["reason_codes"]}
    return {"scope": "isolated scoring/contract probe; not genuine OOS records", **outputs}


def malformed_oos_probe() -> dict:
    manifest = {"ticker": "TEST", "horizon": "5D", "snapshot_id": "audit-probe",
                "latest_confirmed_close": {"session_date": "2024-02-01"}}
    rows = [{"decision_session": "2024-01-02", "label_end_session": "2024-01-02",
             "training_label_end_session": "2024-01-20", "target": 1, "probability": .8,
             "target_definition": "deliberately wrong target for 5D"}]
    paths = [{"researcher_id": name, "ticker": "TEST", "horizon": "5D",
              "snapshot_id": "audit-probe", "result_role": "forecast",
              "validation": {"oos_forecasts": rows}} for name in ("quant", "ml")]
    quality = build_oos_probability_quality(paths, manifest)
    return {"scope": "OOS validator only; no claim that stock-data emits these rows",
            "malformed_row": rows[0], "quality_status": quality["status"],
            "accepted_common_samples": quality["common_oos"]["common_oos_samples"]}


def quant_threshold_probe() -> dict:
    payload = collection(170)
    bars = payload["domains"]["market"]["data"]["bars"]
    for index, bar in enumerate(bars):
        close = 100 * 1.001 ** index
        bar.update(open=close / 1.001, high=close * 1.01, low=close * .99, close=close)
    manifest = {"ticker": "INTC", "horizon": "5D", "snapshot_id": "audit-probe"}
    market = payload["domains"]["market"]
    market["_data_validation"] = {"forecast_eligible": True}
    result = quant.run(HERE, (manifest, market))
    validation = result["validation"]
    return {"input_is_synthetic": True, "bars": 170, "horizon": "5D",
            "current_status": result["status"], "current_probability": result["prob_up"],
            "current_match_count": validation["n_raw"],
            "reported_oos_predictions": validation["oos_predictions"],
            "oos_training_counts": [row["training_sample_count"] for row in validation["oos_forecasts"]]}


def main() -> None:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="probe-", dir=HERE) as temporary:
        horizon_results = [run_horizon(h, Path(temporary)) for h in ("1D", "5D", "20D")]
    budgets = []
    for horizon in (1, 5, 20):
        budget = bucket_budget(756, horizon)
        minimum = next(n for n in range(200, 6000)
                       if min(bucket_budget(n, horizon)["distinct_score_bucket_counts"].values()) >= 20)
        budget["minimum_bars_for_all_three_distinct_score_buckets_at_20"] = minimum
        budgets.append(budget)
    # Best mathematically attainable BY q-value if all 11 tests achieve their
    # lowest attainable circular-shift p-value for a 25-observation, block=5 cohort.
    permutations = 25 - 2 * 5 + 1
    p_floor = 1 / (permutations + 1)
    optimistic_by = benjamini_yekutieli({str(i): p_floor for i in range(11)}, trials=11)
    evidence = {"audit_date_local": "2026-10-11", "timezone": "Asia/Shanghai",
                "repository_revision": revision, "python": platform.python_version(),
                "synthetic_only": True, "horizon_runs": horizon_results,
                "sample_budgets": budgets, "confidence_probe": confidence_probe(),
                "malformed_oos_probe": malformed_oos_probe(),
                "quant_threshold_probe": quant_threshold_probe(),
                "five_day_ic_resolution_bound": {
                    "observations": 25, "block_length_observations": 5,
                    "potential_offsets": permutations, "minimum_required_offsets": 19,
                    "runtime_emits_p_value": False,
                    "hypothetical_minimum_raw_p_if_sample_check_removed": p_floor,
                    "optimistic_minimum_BY_q_with_11_equal_minimum_p": min(optimistic_by.values())}}
    output = HERE / "audit_evidence.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"evidence_path": str(output), "synthetic_only": True,
                      "horizons": [{"horizon": row["horizon"], "buckets": row["factor_bucket_counts"],
                                    "current_paths": row["consensus"]["path_ids"],
                                    "complete_common_samples": row["consensus"]["oos_quality"].get("common_oos", {}).get("all_paths_valid_prediction_samples", 0)}
                                   for row in horizon_results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
