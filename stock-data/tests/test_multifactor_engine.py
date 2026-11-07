from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_research.multifactor import group_equal_weight_score, prepare_cross_section
from stock_research.multifactor.diagnostics import evaluate_predictions, factor_diagnostics, training_correlation_clusters
from stock_research.multifactor.study import load_candidate, normalize_dataset_admission, run_multifactor_study


def cross_section(day="2024-01-02", n=40):
    rng = np.random.default_rng(18)
    size = np.linspace(20, 27, n) + rng.normal(0, 0.2, n)
    industry = np.where(np.arange(n) % 2 == 0, "software", "semiconductors")
    return pd.DataFrame({
        "security_id": [f"US:XNAS:T{i:02}" for i in range(n)],
        "cutoff_date": day, "security_type": "common_stock",
        "industry": industry, "log_mcap": size,
        "industry_pit_confirmed": True, "size_pit_confirmed": True,
        "f__mom_60": 0.4 * size + (industry == "software") + rng.normal(0, 0.4, n),
        "f__ep_ttm": rng.normal(0, 0.03, n), "f__size_log": size,
        "f__curve_10y_2y": 0.5,
        "m__mom_60": 0, "m__ep_ttm": 0,
    })


def historical_panel(n=20, days=140):
    rng = np.random.default_rng(73)
    rows = []
    dates = pd.date_range("2024-01-02", periods=days, freq="D")
    for day in dates:
        mom, ep = rng.normal(size=(2, n))
        returns = 0.025 * mom + 0.015 * ep + rng.normal(0, 0.001, n)
        for i in range(n):
            label_day = day + pd.Timedelta(days=1)
            rows.append({
                "security_id": f"US:XNAS:T{i:02}", "cutoff_date": day.date().isoformat(),
                "entry_date": label_day.date().isoformat(), "end_date": label_day.date().isoformat(),
                "matured_at": (label_day + pd.Timedelta(hours=21)).tz_localize("UTC").isoformat(),
                "market": "US", "horizon": 1, "target": "absolute", "label_status": "ok",
                "return_value": returns[i], "f__mom_60": mom[i], "f__ep_ttm": ep[i],
                "m__mom_60": 0, "m__ep_ttm": 0, "security_type": "common_stock",
            })
    return pd.DataFrame(rows)


def protocol():
    return {
        "name": "synthetic-factor-behavior-check", "market": "US", "horizon": 1,
        "target": "absolute", "factors": ["mom_60", "ep_ttm"],
        "train_start": "2024-01-02", "train_end": "2024-03-01",
        "dev_start": "2024-03-04", "dev_end": "2024-03-28",
        "cal_start": "2024-04-01", "cal_end": "2024-04-18",
        "test_start": "2024-04-22", "test_end": "2024-05-20",
        "seed": 21, "bootstrap_resamples": 20, "block_days": 3,
        "ridge_grid": [0.01, 0.1], "dataset_admission": {
            "status": "synthetic_behavior_test", "evidence": "test fixture; no investment accuracy claim"},
    }


def test_small_universe_is_suspended_without_neutral_zero():
    result = prepare_cross_section(cross_section(n=7), ["mom_60", "ep_ttm"])
    assert result["diagnostics"]["status"] == "suspended"
    assert result["frame"][["z__mom_60", "n__mom_60", "z__ep_ttm", "n__ep_ttm"]].isna().all().all()


def test_cross_section_is_date_local_and_filters_etf_and_missing_flag():
    first = cross_section()
    first.loc[0, "m__mom_60"] = 1
    etf = first.iloc[[0]].copy()
    etf["security_id"], etf["security_type"], etf["f__mom_60"] = "US:ARCX:SPY", "etf", 1e9
    future = cross_section("2024-01-03")
    future["f__mom_60"] *= 1e6
    reference = prepare_cross_section(first, ["mom_60"], neutralize=False)["frame"]
    result = prepare_cross_section(pd.concat([first, etf, future], ignore_index=True), ["mom_60"], neutralize=False)
    observed = result["frame"].loc[result["frame"]["cutoff_date"] == "2024-01-02"]
    assert result["diagnostics"]["excluded_non_equity_rows"] == 1
    assert pd.isna(observed.loc[0, "z__mom_60"])
    np.testing.assert_allclose(reference["z__mom_60"], observed["z__mom_60"], equal_nan=True)


def test_neutralization_removes_industry_size_only_when_point_in_time_confirmed():
    frame = cross_section()
    result = prepare_cross_section(frame, ["mom_60"])
    residual = result["frame"]["n__mom_60"]
    assert residual.notna().all()
    assert abs(residual.corr(frame["log_mcap"])) < 1e-10
    assert np.max(np.abs(residual.groupby(frame["industry"]).mean())) < 1e-10
    frame["industry_pit_confirmed"] = "false"
    suspended = prepare_cross_section(frame, ["mom_60"])["frame"]
    assert suspended["z__mom_60"].notna().all()
    assert suspended["n__mom_60"].isna().all()


def test_rank_deficient_controls_suspend_and_size_does_not_remove_itself():
    frame = cross_section()
    frame["log_mcap"] = np.where(frame["industry"] == "software", 20.0, 25.0)
    result = prepare_cross_section(frame, ["mom_60"])
    assert result["frame"]["n__mom_60"].isna().all()
    result = prepare_cross_section(cross_section(), ["size_log"])
    assert result["frame"]["n__size_log"].notna().all()
    assert result["frame"]["n__size_log"].std() > 0.9


def test_macro_level_remains_context_and_duplicate_equity_is_rejected():
    result = prepare_cross_section(cross_section(), ["curve_10y_2y"])
    assert result["frame"]["f__curve_10y_2y"].eq(0.5).all()
    assert result["frame"]["z__curve_10y_2y"].isna().all()
    with pytest.raises(ValueError, match="duplicate"):
        prepare_cross_section(pd.concat([cross_section(), cross_section().iloc[[0]]]), ["mom_60"])


def test_equal_group_scores_preserve_missing_and_fixed_family_coverage():
    frame = pd.DataFrame({"z__mom_60": [2.0, 2.0], "z__ep_ttm": [0.0, np.nan]})
    result = group_equal_weight_score(frame, ["mom_60", "ep_ttm"], return_details=True)
    assert result["score"].iloc[0] == 1.0
    assert pd.isna(result["score"].iloc[1])
    assert result["coverage"]["covered_family_count"].tolist() == [2, 1]
    partial = group_equal_weight_score(frame, ["mom_60", "ep_ttm"], min_groups=1, require_complete=False)
    assert partial.iloc[1] == 2.0
    assert partial.attrs["require_complete"] is False


def test_factor_basket_does_not_replace_missing_future_return():
    frame = cross_section()
    frame["signal"] = np.arange(len(frame), dtype=float)
    frame["return_value"] = frame["signal"] / 1000
    frame.loc[len(frame) - 1, "return_value"] = np.nan
    result = factor_diagnostics(frame, "signal")
    assert result["rank_ic_mean"] == pytest.approx(1.0)
    assert result["dates"][0]["gross_spread"] is None
    assert result["dates"][0]["spread_reason"] == "selected_basket_has_missing_outcome_no_replacement"
    assert result["dates"][0]["turnover"] == pytest.approx(2.0)


def test_full_denominator_and_date_grouped_accuracy():
    frame = pd.DataFrame({"cutoff_date": ["2024-01-02"] * 3, "security_id": ["A", "B", "C"],
                          "return_value": [0.1, -0.1, np.nan], "label_status": ["ok"] * 3})
    result = evaluate_predictions(frame, [0.1, np.nan, 0.1], "return_value")
    assert result["scheduled_rows"] == 3
    assert result["evaluable_rows"] == 1
    assert result["direction_accuracy_full_denominator"] == pytest.approx(1 / 3)
    assert result["direction_accuracy_available_only"] == 1.0
    assert result["rank_ic_mean"] is None
    assert result["date_accuracy_ci"]["status"] == "insufficient_dates"


def test_correlation_representative_uses_training_and_predeclared_order():
    frame = pd.DataFrame({"f__mom_60": np.arange(100), "f__ep_ttm": np.arange(100) * 3, "partition": "train"})
    result = training_correlation_clusters(frame, ["ep_ttm", "mom_60"])
    assert result["representatives"] == ["ep_ttm"]
    frame.loc[0, "partition"] = "test"
    with pytest.raises(ValueError, match="training block"):
        training_correlation_clusters(frame, ["ep_ttm", "mom_60"])


def test_native_ridge_really_fits_and_seals_forecasts_before_outcomes(tmp_path):
    result = run_multifactor_study(historical_panel(), protocol(), ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])
    selected = json.loads((Path(result["artifact_directory"]) / "selected.json").read_text(encoding="utf-8"))
    assert np.linalg.norm(selected["state"]["coefficients"]) > 0.015
    assert result["test_metrics"]["selected"]["direction_accuracy_available_only"] > 0.9
    assert result["test_metrics"]["selected"]["date_weighted_mse"] < result["test_metrics"]["mean_control"]["date_weighted_mse"]
    forecasts = pd.read_csv(Path(result["artifact_directory"]) / "test-predictions.csv")
    assert "return_value" not in forecasts
    assert "label" not in forecasts
    assert result["forecast_seal"]["contains_holdout_outcomes"] is False
    assert result["probability_verified"] is False and result["activated"] is False
    assert result["dataset_admission"]["status"] == "synthetic_behavior_test"
    model = load_candidate(Path(result["artifact_directory"]) / "selected.json",
                           result["forecast_seal"]["model_files"]["selected.json"])
    test_rows = historical_panel().loc[lambda data: (data["cutoff_date"] >= "2024-04-22") & (data["cutoff_date"] <= "2024-05-20")]
    np.testing.assert_allclose(model.predict(test_rows), forecasts["selected_score"], rtol=1e-12, atol=1e-12)


def test_future_holdout_labels_cannot_change_fit_selection_or_forecasts(tmp_path):
    first = historical_panel()
    changed = first.copy()
    changed.loc[changed["cutoff_date"] >= "2024-04-22", "return_value"] *= -10
    a = run_multifactor_study(first, protocol(), ["mom_60", "ep_ttm"], tmp_path / "a", candidates=["ridge"])
    b = run_multifactor_study(changed, protocol(), ["mom_60", "ep_ttm"], tmp_path / "b", candidates=["ridge"])
    state_a = json.loads((Path(a["artifact_directory"]) / "selected.json").read_text(encoding="utf-8"))
    state_b = json.loads((Path(b["artifact_directory"]) / "selected.json").read_text(encoding="utf-8"))
    assert state_a == state_b
    assert a["selected_parameters"] == b["selected_parameters"]
    assert a["forecast_seal"]["prediction_sha256"] == b["forecast_seal"]["prediction_sha256"]
    assert a["data_sha256"] != b["data_sha256"]


def test_holdout_ledger_rejects_repeated_test_even_with_changed_candidates(tmp_path):
    frame = historical_panel()
    run_multifactor_study(frame, protocol(), ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])
    changed_protocol = {**protocol(), "name": "try-to-select-again", "ridge_grid": [0.5]}
    with pytest.raises(ValueError, match="already reserved or evaluated"):
        run_multifactor_study(frame, changed_protocol, ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])


def test_study_rebuilds_partitions_and_purges_label_maturity(tmp_path):
    frame = historical_panel()
    frame["partition"] = "train"
    affected = frame["cutoff_date"] == "2024-03-01"
    frame.loc[affected, "matured_at"] = "2024-03-04T21:00:00Z"
    result = run_multifactor_study(frame, protocol(), ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])
    assert result["temporal_checks"]["eligible_counts"]["train"] < result["temporal_checks"]["partition_counts"]["train"]
    assert result["temporal_checks"]["partition_counts"]["test"] > 0


def test_study_rejects_outcome_as_feature_unsealed_kronos_and_late_features(tmp_path):
    frame = historical_panel()
    with pytest.raises(ValueError, match="explanatory factors"):
        run_multifactor_study(frame, {**protocol(), "factors": ["return_value"]}, ["return_value"], tmp_path)
    kronos = frame.assign(f__kronos_vote=0.5)
    with pytest.raises(ValueError, match="out-of-fold"):
        run_multifactor_study(kronos, {**protocol(), "factors": ["mom_60", "kronos_vote"]}, ["mom_60", "kronos_vote"], tmp_path)
    late = frame.assign(feature_cutoff_at=frame["cutoff_date"] + "T21:00:00Z",
                        feature_available_at=frame["cutoff_date"] + "T22:00:00Z")
    with pytest.raises(ValueError, match="unavailable at the requested cutoff"):
        run_multifactor_study(late, protocol(), ["mom_60", "ep_ttm"], tmp_path)


def test_missing_admission_never_claims_verified_point_in_time(tmp_path):
    unverified = protocol()
    unverified.pop("dataset_admission")
    result = run_multifactor_study(historical_panel(), unverified, ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])
    assert result["dataset_admission"]["status"] == "research_development_unverified"
    assert result["evidence_status"] == "research_only"


@pytest.mark.parametrize("evidence", [
    {"evidence": "audit/point-in-time.json"},
    {"evidence": {"path": "audit/point-in-time.json", "sha256": "not-a-locator"}},
    {"evidence": [{"locator": "audit/point-in-time.json"}]},
    {"evidence": {"prices": {"path": "audit/point-in-time.json"}}},
    {"evidence": {"prices": [{"path": "audit/point-in-time.json"}]}},
    {"evidence_paths": "audit/point-in-time.json"},
    {"evidence_paths": [" audit/point-in-time.json ", "audit/point-in-time.json"]},
])
def test_admission_accepts_canonical_and_legacy_evidence_locators(evidence):
    normalized = normalize_dataset_admission({"status": "verified", **evidence})
    assert normalized["status"] == "verified_point_in_time"
    assert normalized["has_evidence_locator"] is True
    assert normalized["evidence_locators"] == ["audit/point-in-time.json"]
    assert normalize_dataset_admission(normalized) == normalized


@pytest.mark.parametrize("evidence", [None, "", "   ", [], {}, False, {"verified": True}, [{"valid": True}], {"note": "download succeeded"}, {"notes": ["download succeeded"]}])
def test_empty_or_flag_only_evidence_downgrades_admission(evidence):
    normalized = normalize_dataset_admission({"status": "verified_point_in_time", "evidence": evidence})
    assert normalized["status"] == "research_development_unverified"
    assert normalized["has_evidence_locator"] is False
    assert normalized["evidence_locators"] == []


def test_admission_paths_preserved_by_engine_without_activating_probability(tmp_path):
    admitted = {**protocol(), "dataset_admission": {"status": "verified", "evidence_paths": ["fixture-audit.json"]}}
    result = run_multifactor_study(historical_panel(), admitted, ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])
    assert result["dataset_admission"]["status"] == "verified_point_in_time"
    assert result["dataset_admission"]["evidence_locators"] == ["fixture-audit.json"]
    assert result["evidence_status"] == "research_only"
    assert result["probability_verified"] is False


def test_all_missing_current_inputs_suspend_prediction_and_remain_in_denominator(tmp_path):
    frame = historical_panel()
    missing = (frame["cutoff_date"] >= "2024-04-22") & (frame["security_id"] == "US:XNAS:T00")
    frame.loc[missing, ["m__mom_60", "m__ep_ttm"]] = 1
    result = run_multifactor_study(frame, protocol(), ["mom_60", "ep_ttm"], tmp_path, candidates=["ridge"])
    predictions = pd.read_csv(Path(result["artifact_directory"]) / "test-predictions.csv")
    assert predictions.loc[predictions["security_id"] == "US:XNAS:T00", "selected_score"].isna().all()
    assert result["test_metrics"]["selected"]["prediction_available_rows"] < result["test_metrics"]["selected"]["scheduled_rows"]


def test_calibration_feature_changes_cannot_change_fit_or_frozen_test(tmp_path):
    reference = historical_panel()
    changed = reference.copy()
    cal = (changed["cutoff_date"] >= "2024-04-01") & (changed["cutoff_date"] <= "2024-04-18")
    changed.loc[cal, "f__mom_60"] *= -1000
    a = run_multifactor_study(reference, protocol(), ["mom_60", "ep_ttm"], tmp_path / "a", candidates=["ridge"])
    b = run_multifactor_study(changed, protocol(), ["mom_60", "ep_ttm"], tmp_path / "b", candidates=["ridge"])
    assert a["forecast_seal"]["model_files"]["selected.json"] == b["forecast_seal"]["model_files"]["selected.json"]
    assert a["forecast_seal"]["prediction_sha256"] == b["forecast_seal"]["prediction_sha256"]
    assert a["calibration_diagnostics"]["selected"]["date_weighted_mse"] != b["calibration_diagnostics"]["selected"]["date_weighted_mse"]


def test_unavailable_lightgbm_is_reported_without_substituting_ridge(tmp_path, monkeypatch):
    import builtins

    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "lightgbm":
            raise ModuleNotFoundError("test missing local dependency", name="lightgbm")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = run_multifactor_study(historical_panel(), protocol(), ["mom_60", "ep_ttm"], tmp_path,
                                  candidates=["ridge", "lightgbm"])
    attempts = result["selection"]["candidate_attempts"]
    assert all(attempt["status"] == "unavailable" for attempt in attempts if attempt["model_type"] == "lightgbm")
    assert result["selected_model_type"] == "ridge"


def test_development_prediction_coverage_gate_rejects_sparse_candidate(tmp_path):
    frame = historical_panel()
    dev = (frame["cutoff_date"] >= "2024-03-04") & (frame["cutoff_date"] <= "2024-03-28")
    missing = dev & frame["security_id"].isin([f"US:XNAS:T{i:02}" for i in range(10)])
    frame.loc[missing, "m__ep_ttm"] = 1
    result = run_multifactor_study(frame, protocol(), ["mom_60", "ep_ttm"], tmp_path,
                                  candidates=["equal_weight", "ridge"])
    equal = next(attempt for attempt in result["selection"]["candidate_attempts"] if attempt["model_type"] == "equal_weight")
    assert equal["status"] == "insufficient_development_prediction_coverage"
    assert equal["dev_prediction_coverage"] == pytest.approx(0.5)
    assert equal["dev_metrics"]["scheduled_rows"] == 500
    assert equal["dev_metrics"]["prediction_available_rows"] == 250
    assert result["selected_model_type"] == "ridge"
    assert result["selection"]["dev_min_prediction_coverage"] == 0.9


def test_retrospective_kronos_evidence_keeps_actual_time_and_is_not_admitted(tmp_path):
    frame = historical_panel().assign(f__kronos_vote=0.5)
    frame["kronos_input_cutoff_at"] = frame["cutoff_date"] + "T21:00:00Z"
    frame["kronos_model_trained_through"] = "2023-12-31T21:00:00Z"
    frame["kronos_forecast_sealed_at"] = "2026-11-07T00:00:00Z"
    spec = {**protocol(), "factors": ["mom_60", "kronos_vote"],
            "kronos_oof_manifest": {"sealed": True, "out_of_fold": True, "frozen_model": True,
                                   "evidence": {"path": "replay-receipt.json"}}}
    with pytest.raises(ValueError, match="do not backdate"):
        run_multifactor_study(frame, spec, ["mom_60", "kronos_vote"], tmp_path)


def test_lightgbm_uses_real_native_booster_if_installed(tmp_path):
    pytest.importorskip("lightgbm")
    spec = {**protocol(), "lightgbm_grid": [{"num_boost_round": 30, "num_leaves": 7, "max_depth": 3}]}
    result = run_multifactor_study(historical_panel(), spec, ["mom_60", "ep_ttm"], tmp_path, candidates=["lightgbm"])
    directory = Path(result["artifact_directory"])
    assert (directory / "selected-lightgbm.txt").is_file()
    fitted = json.loads((directory / "selected.json").read_text(encoding="utf-8"))
    assert fitted["state"]["library_version"]
    assert result["test_metrics"]["selected"]["date_weighted_mse"] < result["test_metrics"]["mean_control"]["date_weighted_mse"]
    assert b"\r\n" not in (directory / "selected-lightgbm.txt").read_bytes()
    model = load_candidate(directory / "selected.json", result["forecast_seal"]["model_files"]["selected.json"])
    test_rows = historical_panel().loc[lambda data: (data["cutoff_date"] >= "2024-04-22") & (data["cutoff_date"] <= "2024-05-20")]
    np.testing.assert_allclose(model.predict(test_rows), pd.read_csv(directory / "test-predictions.csv")["selected_score"], rtol=1e-10, atol=1e-10)
    unicode_directory = directory / "模型工件"
    unicode_directory.mkdir()
    for name in ("selected.json", "selected-lightgbm.txt"):
        (unicode_directory / name).write_bytes((directory / name).read_bytes())
    reloaded = load_candidate(unicode_directory / "selected.json", result["forecast_seal"]["model_files"]["selected.json"])
    np.testing.assert_allclose(reloaded.predict(test_rows), model.predict(test_rows), rtol=1e-12, atol=1e-12)
    tree_path = directory / "selected-lightgbm.txt"
    tree_path.write_text(tree_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tree model checksum mismatch"):
        load_candidate(directory / "selected.json", result["forecast_seal"]["model_files"]["selected.json"])
