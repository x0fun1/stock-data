//! Migrated R06 checks: Research B label boundary, Research C refit-at-every-origin,
//! reliability accounting, capacity/timing disclosure, prefix invariance and the
//! policy-disabled shadow calibration.
//!
//! These checks run the real researchers (`factor::run_with_options`, `ml::run`), the
//! reliability layer and the feasibility disclosures on deterministic synthetic bars
//! (the same generators `selfcheck.py` uses: `_policy_bars` seed 5 and `synth_bars`
//! seed 11), so a behavioural regression fails here rather than being averaged away by
//! a fixture diff.

use chrono::{Duration, NaiveDate};
use serde_json::{json, Value};

use crate::selfcheck::registry::CheckFn;
use sd_core::random::PythonRandom;
use sd_core::reliability::{
    logistic_shortfall, probabilistic_scores, qualification, qualification_thresholds,
    sample_diagnostics, ROUTE_ABSTAINED, ROUTE_LEGACY,
};
use sd_research::factor::{run_with_options, FactorOptions};
use sd_research::feasibility::{build_forecast_feasibility, build_timing};
use sd_research::ml;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

/// `synth_bars` from `selfcheck.py`: deterministic cash-price walk from 2023-01-02.
fn synth_bars(count: usize) -> Vec<Value> {
    let mut rng = PythonRandom::new(11);
    let mut price = 100.0f64;
    let mut day = NaiveDate::from_ymd_opt(2023, 1, 2).expect("start date");
    let mut bars: Vec<Value> = Vec::new();
    while bars.len() < count {
        if day.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            price *= rng.gauss(0.0005, 0.018).exp();
            bars.push(json!({
                "date": day.to_string(),
                "open": round4(price * 0.997),
                "high": round4(price * 1.012),
                "low": round4(price * 0.988),
                "close": round4(price),
                "volume": 1_000_000.0 + bars.len() as f64,
            }));
        }
        day += Duration::days(1);
    }
    bars
}

/// `_policy_bars` from `selfcheck.py`: 300 sessions ending 2026-10-23 (seed 5).
/// The frozen snapshot trims these to the 260-session policy window, so callers pass the
/// last 260 bars to the researchers, exactly like `load_snapshot` does.
fn policy_bars(count: usize) -> Vec<Value> {
    let mut rng = PythonRandom::new(5);
    let mut price = 100.0f64;
    let mut day = NaiveDate::from_ymd_opt(2026, 10, 23).expect("policy end date");
    let mut days: Vec<NaiveDate> = Vec::new();
    while days.len() < count {
        if day.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            days.push(day);
        }
        day -= Duration::days(1);
    }
    days.reverse();
    days.into_iter()
        .map(|session| {
            price *= 1.0 + rng.gauss(0.0004, 0.011);
            json!({
                "date": session.to_string(),
                "open": price * 0.995,
                "high": price * 1.012,
                "low": price * 0.988,
                "close": price,
                "volume": 1_000_000.0 + rng.randrange(200_001) as f64,
            })
        })
        .collect()
}

fn market_from(bars: &[Value]) -> Value {
    json!({
        "data": {
            "currency": "USD",
            "exchange": "US",
            "adjustment": "unadjusted",
            "bars": bars,
            "source_symbol": "SYN",
            "fetched_at_utc": "2026-01-02T00:00:00Z",
            "source_timestamp": "2026-01-02T00:00:00Z",
        },
        "_data_validation": {"forecast_eligible": true},
    })
}

fn ml_manifest(bars: &[Value]) -> Value {
    json!({
        "ticker": "SYN",
        "horizon": "5D",
        "snapshot_id": format!("synth_{}", bars.len()),
        "market": "US",
    })
}

fn ml_run(bars: &[Value]) -> Result<Value, String> {
    ml::run(&ml_manifest(bars), &market_from(bars))
}

/// The policy window is 260 sessions, so `load_snapshot` hands the researchers the last
/// 260 of the 300 generated sessions.
fn policy_window_bars() -> Vec<Value> {
    let mut bars = policy_bars(300);
    bars.split_off(300 - 260)
}

/// T20: Research B's reported probability uses post-selection labels only.
pub fn legacy_t20() -> Result<(), String> {
    let bars = policy_window_bars();
    let manifest = json!({
        "ticker": "SYN",
        "horizon": "5D",
        "snapshot_id": "policy_260",
        "market": "US",
    });
    let market = market_from(&bars);
    let options = FactorOptions {
        selection_override: None,
        sampling_mode: "anchored",
        selection_profile: Some("restricted"),
        bucket_mode: "two",
    };
    let result = run_with_options(&manifest, &market, &options)?;
    let candidate = &result["validation"]["honest_calibration_candidate"];
    if candidate["used_for_reported_probability"] != Value::Bool(true) {
        return Err(failure("the honest candidate must be the reported path"));
    }
    if result["prob_up"].is_null() {
        if candidate["status"] != "abstained" {
            return Err(failure(format!(
                "an abstained factor path must say abstained: {candidate}"
            )));
        }
    } else {
        if candidate["status"] != "candidate_available" {
            return Err(failure(format!(
                "a published factor probability needs a candidate: {candidate}"
            )));
        }
        if candidate["observations"].as_i64().unwrap_or(0) <= 0 {
            return Err(failure("a published candidate must have observations"));
        }
    }
    // The selection boundary is disclosed and strictly before the final decision.
    let boundary = candidate["selection_boundary_session"]
        .as_str()
        .ok_or_else(|| failure("the selection boundary must be disclosed"))?;
    let last = bars
        .last()
        .and_then(|bar| bar["date"].as_str())
        .ok_or_else(|| failure("bars must have dates"))?;
    if boundary >= last {
        return Err(failure("the selection boundary must precede the decision"));
    }
    Ok(())
}

/// T21: Research C refits at every origin and its calibration is policy-disabled.
pub fn legacy_t21() -> Result<(), String> {
    let bars = policy_window_bars();
    let result = ml_run(&bars)?;
    let validation = &result["validation"];
    if validation["model_hyperparameters"]["refit_sessions"].as_i64() != Some(1) {
        return Err(failure(
            "the model must refit at every origin (refit_sessions == 1)",
        ));
    }
    if validation["probability_calibration"]["status"] != "disabled_by_policy" {
        return Err(failure("calibration must be disabled by policy"));
    }
    let rows = validation["oos_forecasts"]
        .as_array()
        .ok_or_else(|| failure("the OOS ledger must be an array"))?;
    if rows.len() <= 100 {
        return Err(failure(format!(
            "expected >100 OOS rows, got {}",
            rows.len()
        )));
    }
    // `ml::anchor` is the identity function, so the replay must cover every origin without
    // gaps. This is the observable consequence of `_anchor(17) == 17` / `_anchor(18) == 18`.
    let mut previous: Option<i64> = None;
    for row in rows {
        let decision = row["decision_index"]
            .as_i64()
            .ok_or_else(|| failure("every ledger row must carry decision_index"))?;
        if let Some(previous) = previous {
            if decision != previous + 1 {
                return Err(failure(format!(
                    "the replay skipped an origin: {previous} -> {decision}"
                )));
            }
        }
        previous = Some(decision);
    }
    Ok(())
}

/// F29: the reliability layer reports samples honestly and cannot lower its own bar.
pub fn legacy_f29() -> Result<(), String> {
    let sample = sample_diagnostics(250, 60, 5, None);
    if sample["raw_sample_count"].as_i64() != Some(185)
        || sample["nonoverlap_sample_count"].as_i64() != Some(31)
    {
        return Err(failure(format!(
            "52-week sample accounting changed: {sample}"
        )));
    }
    if sample["nonoverlap_count_basis"] != "legacy_spacing" {
        return Err(failure("the fallback basis must be named: legacy_spacing"));
    }
    let coverage = sample["sample_coverage"].as_f64().unwrap_or(0.0);
    if (coverage - 185.0 / 190.0).abs() > 1e-12 {
        return Err(failure(
            "coverage must be matured labels over decision dates",
        ));
    }
    if sample["overlapping_observations_are_independent"] != Value::Bool(false) {
        return Err(failure("overlapping labels must never be independent"));
    }

    // `_golden_bars(250, seed=11)`: the 250 most recent weekdays ending 2026-01-02.
    let mut days: Vec<NaiveDate> = Vec::new();
    let mut cursor = NaiveDate::from_ymd_opt(2026, 1, 2).expect("golden end date");
    while days.len() < 250 {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            days.push(cursor);
        }
        cursor -= Duration::days(1);
    }
    days.reverse();
    let dates: Vec<String> = days.iter().map(ToString::to_string).collect();
    let anchored = sample_diagnostics(250, 60, 5, Some(&dates));
    if anchored["nonoverlap_count_basis"] != "anchored_phase" {
        return Err(failure("a dated series must use the anchored phase"));
    }
    if anchored["nonoverlap_sample_count"].as_i64().unwrap_or(0) >= 31 {
        return Err(failure(format!(
            "the anchored basis must count fewer labels: {anchored}"
        )));
    }
    if sample_diagnostics(250, 60, 20, None)["nonoverlap_sample_count"].as_i64() != Some(9) {
        return Err(failure("20D non-overlapping count changed"));
    }

    // A fitted model needs three disjoint stages, so the floor is their sum.
    let withheld = logistic_shortfall(31);
    if withheld["status"] != "not_applicable_insufficient_samples" {
        return Err(failure("31 labels cannot host a fitted model"));
    }
    let thresholds = qualification_thresholds();
    let required: i64 = thresholds["logistic"]
        .as_object()
        .map(|stages| stages.values().filter_map(Value::as_i64).sum())
        .unwrap_or(0);
    if withheld["required_non_overlapping_labels"].as_i64() != Some(required) || required != 120 {
        return Err(failure(
            "the requirement must be the sum of the three stages (120)",
        ));
    }
    if withheld["shortfall"].as_i64() != Some(120 - 31) {
        return Err(failure("the shortfall must be reported, not hidden"));
    }

    // Routing is decided by the declared floors only.
    let abstained = qualification(5, 2, 1, 3);
    if abstained["route"] != ROUTE_ABSTAINED {
        return Err(failure(format!("thin evidence must abstain: {abstained}")));
    }
    let qualified = qualification(40, 9, 3, 15);
    if qualified["route"] != ROUTE_LEGACY {
        return Err(failure(
            "sufficient evidence must route to the legacy bucket",
        ));
    }
    if qualified["thresholds_adjustable_per_symbol"] != Value::Bool(false) {
        return Err(failure("thresholds must not be adjustable per symbol"));
    }

    // A perfect forecast scores zero Brier and unit skill; copying the base rate scores zero.
    let perfect = [
        (0i64, 1.0f64, Some(0.5f64), 1i64),
        (1, 0.0, Some(0.5), 0),
        (2, 1.0, Some(0.5), 1),
    ];
    let scores = probabilistic_scores(&perfect, 5);
    if scores["brier"].as_f64() != Some(0.0) {
        return Err(failure(format!(
            "a perfect forecast must score zero Brier: {scores}"
        )));
    }
    if (scores["brier_skill_vs_base_rate"].as_f64().unwrap_or(0.0) - 1.0).abs() > 1e-12 {
        return Err(failure("a perfect forecast must score unit skill"));
    }
    let outcomes = [1i64, 0, 1, 0, 1, 0];
    let copied: Vec<(i64, f64, Option<f64>, i64)> = outcomes
        .iter()
        .enumerate()
        .map(|(index, outcome)| (index as i64, 0.4f64, Some(0.4f64), *outcome))
        .collect();
    let copied_scores = probabilistic_scores(&copied, 5);
    if copied_scores["brier_skill_vs_base_rate"]
        .as_f64()
        .map(|skill| skill.abs() > 1e-12)
        .unwrap_or(true)
    {
        return Err(failure("copying the baseline cannot earn skill"));
    }
    if copied_scores["brier_difference_interval_status"] != "insufficient_samples" {
        return Err(failure(
            "a short pair series must not claim a bootstrap interval",
        ));
    }
    Ok(())
}

/// F34: capacity is declared before the run and timing never guesses executability.
pub fn legacy_f34() -> Result<(), String> {
    let bars = synth_bars(250);
    let last_session = bars
        .last()
        .and_then(|bar| bar["date"].as_str())
        .ok_or_else(|| failure("bars must have dates"))?
        .to_string();
    let manifest = json!({
        "horizon": "20D",
        "asof_timestamp": "2026-10-18T12:00:00Z",
        "data_validation": {"latest_confirmed_close": {
            "session_date": last_session,
            "close_at": "2026-10-16T20:00:00+00:00",
        }},
    });
    let block = build_forecast_feasibility(&manifest, &bars, None);
    if block["paths"]["quant"]["capacity_matured_non_overlapping"].as_i64() != Some(10) {
        return Err(failure("250 bars at H=20 must cap the quant ledger at 10"));
    }
    if block["paths"]["quant"]["status"] != "capacity_insufficient" {
        return Err(failure(
            "10 labels cannot satisfy the 12-match quant minimum",
        ));
    }
    if block["paths"]["factor"]["status"] != "capacity_insufficient" {
        return Err(failure(
            "the selection segment cannot host ten labels at H=20",
        ));
    }
    if block["paths"]["ml"]["status"] != "feasible" {
        return Err(failure(
            "the logistic path uses daily observations and remains feasible",
        ));
    }

    let mut five_day_manifest = manifest
        .as_object()
        .cloned()
        .ok_or_else(|| failure("the manifest must be an object"))?;
    five_day_manifest.insert("horizon".into(), Value::String("5D".into()));
    let five_day = build_forecast_feasibility(&Value::Object(five_day_manifest), &bars, None);
    if five_day["paths"]["quant"]["status"] != "feasible" {
        return Err(failure(
            "the 5D quant path must be feasible on a 52-week window",
        ));
    }
    if !five_day["paths"]["quant"]["observed"].is_null() {
        return Err(failure(
            "observed labels require results and must not be invented",
        ));
    }

    let timing = build_timing(&manifest, &bars);
    if timing["decision_at"] != "2026-10-16T20:00:00+00:00" {
        return Err(failure(
            "timing must carry the confirmed close, not the as-of clock",
        ));
    }
    if timing["label_basis"] != "close_to_close" || timing["entry_basis"] != "not_modelled" {
        return Err(failure(
            "timing must state the label basis and unmodelled entry",
        ));
    }
    if !timing["first_executable_at"].is_null() {
        return Err(failure("first executable time must stay unresolved"));
    }
    Ok(())
}

fn oos_rows(result: &Value) -> Result<&Vec<Value>, String> {
    result["validation"]["oos_forecasts"]
        .as_array()
        .ok_or_else(|| failure("the ML result must carry the OOS ledger"))
}

/// F02: a truncated history reproduces the same probabilities for the shared prefix.
pub fn legacy_f02() -> Result<(), String> {
    let bars = synth_bars(640);
    let result = ml_run(&bars)?;
    let full = oos_rows(&result)?;
    let cut = 560;
    let truncated = ml_run(&bars[..cut + 1])?;
    let truncated_rows = oos_rows(&truncated)?;
    let by_date: std::collections::HashMap<&str, &Value> = truncated_rows
        .iter()
        .filter_map(|row| row["decision_session"].as_str().map(|date| (date, row)))
        .collect();
    let mut checked = 0usize;
    for row in full {
        let Some(date) = row["decision_session"].as_str() else {
            continue;
        };
        let Some(other) = by_date.get(date) else {
            continue;
        };
        checked += 1;
        for key in [
            "probability",
            "raw_probability",
            "calibration_applied",
            "calibration_label_end_session",
            "calibration_fit_observations",
        ] {
            if row[key] != other[key] {
                return Err(failure(format!(
                    "{date}: {key} changed on truncation: {} vs {}",
                    row[key], other[key]
                )));
            }
        }
    }
    if checked <= 50 {
        return Err(failure(format!(
            "expected >50 shared origins, compared {checked}"
        )));
    }
    Ok(())
}

/// F41: the shadow replay stays uncalibrated by policy at either window length.
pub fn legacy_f41() -> Result<(), String> {
    for count in [252usize, 640] {
        let bars = synth_bars(count);
        let result = ml_run(&bars)?;
        let validation = &result["validation"];
        let replay = &validation["production_replay"];
        let observations = replay["observations"].as_i64().unwrap_or(0);
        if observations < 20 {
            return Err(failure(format!(
                "the production replay should be populated: {observations}"
            )));
        }
        if validation["probability_calibration"]["status"] != "disabled_by_policy" {
            return Err(failure("calibration must stay disabled by policy"));
        }
        if replay["calibrated_predictions"].as_i64() != Some(0) {
            return Err(failure(
                "no shadow row may be marked calibrated under the policy",
            ));
        }
        if !result["calibrated_probability"].is_null() {
            return Err(failure(
                "a calibrated shadow probability must never be published",
            ));
        }
        if result["status"] != "success" {
            return Err(failure(format!(
                "a policy-disabled calibrator is not a partial run: {}",
                result["status"]
            )));
        }
        let warnings = result["warnings"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .unwrap_or_default();
        if !warnings.contains("disabled by policy") {
            return Err(failure(
                "the shadow artifact must disclose the policy-disabled calibrator",
            ));
        }
    }
    Ok(())
}

/// F42: the live entry and the replay agree at the same origin.
pub fn legacy_f42() -> Result<(), String> {
    let bars = synth_bars(640);
    let result = ml_run(&bars)?;
    let rows = oos_rows(&result)?;
    let target = &rows[rows.len() / 2];
    let decision_index = target["decision_index"]
        .as_i64()
        .ok_or_else(|| failure("the ledger row must carry decision_index"))?;
    let decision_session = target["decision_session"].clone();
    let replay_prefix = ml_run(&bars[..decision_index as usize + 1])?;
    let live_probability = replay_prefix["prob_up"]
        .as_f64()
        .ok_or_else(|| failure("the replay entry must publish prob_up"))?;
    let target_probability = target["probability"]
        .as_f64()
        .ok_or_else(|| failure("the target row must publish a probability"))?;
    if (live_probability - target_probability).abs() > 1e-9 {
        return Err(failure(format!(
            "the live entry and the replay disagree at {decision_session}: \
             {live_probability} vs {target_probability}"
        )));
    }
    let live_raw = replay_prefix["raw_probability"]
        .as_f64()
        .ok_or_else(|| failure("the replay entry must publish raw_probability"))?;
    let target_raw = target["raw_probability"]
        .as_f64()
        .ok_or_else(|| failure("the target row must publish raw_probability"))?;
    if (live_raw - target_raw).abs() > 1e-9 {
        return Err(failure("the raw logits must agree at the same origin"));
    }
    Ok(())
}

/// Implemented R06 checks.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T20", legacy_t20 as CheckFn),
        ("T21", legacy_t21 as CheckFn),
        ("F29", legacy_f29 as CheckFn),
        ("F34", legacy_f34 as CheckFn),
        ("F02", legacy_f02 as CheckFn),
        ("F41", legacy_f41 as CheckFn),
        ("F42", legacy_f42 as CheckFn),
    ]
}
