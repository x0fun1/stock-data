//! Migrated R05 checks: transform/evidence audit, single-path consensus, veto redaction.
//!
//! Each function mirrors the corresponding `selfcheck.py` check and asserts the same
//! property against the real Rust implementation, using synthetic frozen bars.
//!
//! `T17`/`T19` keep a reduced scope until the orchestrator lands (R11); they assert the
//! consensus invariants that the Python versions read out of `analysis.json`. `F01` runs
//! the real R06 ML researcher and proves the published probability is either the current
//! model's calibrated logit or the raw probability, never the last ledger row.

use chrono::{Duration, NaiveDate};
use serde_json::{json, Map, Value};

use crate::consensus::{
    build_consensus, manifest_policy_reasons, redact_publishable_prediction, ConsensusInput,
};
use sd_audit::audit::{audit, AuditInput};
use sd_audit::probability_quality::{
    audit_current_prediction, audit_oos_result, directional_edge_assessment,
};
use sd_core::policy;
use sd_core::random::PythonRandom;
use sd_core::statistics::baseline_comparison;
use sd_research::ml;
use sd_research::quant::predict_at_origin;

use crate::selfcheck::registry::CheckFn;

const HORIZON: usize = 5;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

fn session_end() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 23).expect("frozen check end date")
}

fn weekday_sessions(count: usize) -> Vec<NaiveDate> {
    let mut days = Vec::with_capacity(count);
    let mut cursor = session_end();
    while days.len() < count {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            days.push(cursor);
        }
        cursor -= Duration::days(1);
    }
    days.reverse();
    days
}

fn dates_and_closes(count: usize) -> (Vec<String>, Vec<f64>) {
    let sessions = weekday_sessions(count);
    let dates: Vec<String> = sessions.iter().map(ToString::to_string).collect();
    let mut price = 100.0f64;
    let closes = (0..count)
        .map(|index| {
            price *= 1.0 + (((index as f64) * 0.31).sin()) * 0.006;
            (price * 1_000_000.0).round() / 1_000_000.0
        })
        .collect();
    (dates, closes)
}

fn bars_from(closes: &[f64], dates: &[String]) -> Vec<Value> {
    dates
        .iter()
        .zip(closes.iter())
        .map(|(date, close)| json!({"date": date, "close": close}))
        .collect()
}

fn predict_row(dates: &[String], closes: &[f64], index: usize) -> Value {
    predict_at_origin(dates, closes, index, HORIZON, None, None, false, 6).expect("prediction row")
}

fn quant_result(dates: &[String], closes: &[f64], index: usize) -> Value {
    let row = predict_row(dates, closes, index);
    let mut result = Map::new();
    result.insert("researcher_id".into(), Value::String("quant".into()));
    result.insert("status".into(), Value::String("success".into()));
    result.insert("prob_up".into(), row["probability"].clone());
    result.insert("raw_probability".into(), row["raw_probability"].clone());
    result.insert("expected_return".into(), Value::from(0.004));
    let mut validation = Map::new();
    validation.insert(
        "probability_transform".into(),
        row["probability_transform"].clone(),
    );
    validation.insert("n_raw".into(), row["matching_observations"].clone());
    validation.insert("sampling".into(), json!({"coverage_ratio": 0.5}));
    validation.insert(
        "leakage_audit".into(),
        json!({"future_returns_used_only_as_labels": true}),
    );
    result.insert("validation".into(), Value::Object(validation));
    Value::Object(result)
}

fn oos_rows(dates: &[String], closes: &[f64]) -> Vec<Value> {
    (25..closes.len().saturating_sub(HORIZON))
        .map(|index| predict_row(dates, closes, index))
        .filter(|row| !row["probability"].is_null())
        .collect()
}

fn with_oos(mut result: Value, rows: &[Value]) -> Value {
    if let Value::Object(map) = &mut result {
        if let Some(Value::Object(validation)) = map.get_mut("validation") {
            validation.insert("oos_forecasts".into(), Value::Array(rows.to_vec()));
        }
    }
    result
}

fn edge_record(
    index: usize,
    probability: f64,
    baseline: f64,
    target: i64,
    sampled: bool,
    sampling: &str,
) -> Value {
    json!({
        "decision_session": format!("2026-01-{:02}", index + 1),
        "status": "matured",
        "sampled": sampled,
        "sampling_status": sampling,
        "probability": probability,
        "target": target,
        "probability_transform": {"base_rate_at_origin": baseline},
    })
}

fn code_list(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

// ------------------------------------------------------------------ checks

/// T09: OOS support counts 0/11/12 effective samples.
pub fn legacy_t09() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&closes, &dates);
    let rows = oos_rows(&dates, &closes);
    if rows.len() < 60 {
        return Err(failure(format!("only {} matured OOS rows", rows.len())));
    }
    let base = quant_result(&dates, &closes, closes.len() - 1);
    for (count, expected) in [
        (0usize, Some("NO_VALID_OOS_PROBABILITY")),
        (55, Some("OOS_SUPPORT_INSUFFICIENT")),
        (60, None),
    ] {
        let result = with_oos(base.clone(), &rows[..count]);
        let audit = audit_oos_result(&result, Some(&bars), None);
        let reasons = code_list(&audit["reasons"]);
        match expected {
            Some(code) => {
                if !reasons.iter().any(|reason| reason == code) {
                    return Err(failure(format!(
                        "count {count} expected {code}, got {reasons:?}"
                    )));
                }
            }
            None => {
                if reasons
                    .iter()
                    .any(|reason| reason == "OOS_SUPPORT_INSUFFICIENT")
                {
                    return Err(failure(format!(
                        "count {count} must clear the support floor: {reasons:?}"
                    )));
                }
                if audit["effective_valid_samples"].as_i64().unwrap_or(0) < 12 {
                    return Err(failure("60 valid rows must be 12 effective samples"));
                }
            }
        }
    }
    Ok(())
}

/// T10: a directional edge needs 30 prospective non-overlapping outcomes with a positive interval.
pub fn legacy_t10() -> Result<(), String> {
    let short: Vec<Value> = (0..29)
        .map(|index| edge_record(index, 0.5, 0.5, 1, true, "anchored"))
        .collect();
    let short_result = directional_edge_assessment(&short, &[]);
    if short_result["replay_interval_excludes_zero"] != Value::Bool(false)
        || short_result["supported"] != Value::Bool(false)
    {
        return Err(failure("29 replay records must not claim support"));
    }
    let negative: Vec<Value> = (0..30)
        .map(|index| {
            edge_record(
                index,
                0.80 + ((index * index) % 7) as f64 * 0.01,
                0.5,
                0,
                true,
                "anchored",
            )
        })
        .collect();
    let negative_result = directional_edge_assessment(&negative, &[]);
    if !negative_result["replay_state"]
        .as_str()
        .unwrap_or("")
        .ends_with("excluding_zero")
        || negative_result["replay_claim"] != "replay_interval_below_zero"
    {
        return Err(failure(format!(
            "negative replay state wrong: {negative_result}"
        )));
    }
    let positive: Vec<Value> = (0..30)
        .map(|index| edge_record(index, 0.90 + index as f64 * 0.003, 0.5, 1, true, "anchored"))
        .collect();
    let replay_only = directional_edge_assessment(&positive, &[]);
    if replay_only["replay_interval_excludes_zero"] != Value::Bool(true)
        || replay_only["supported"] != Value::Bool(false)
        || replay_only["independent_supported"] != Value::Bool(false)
    {
        return Err(failure("a positive replay is not independent evidence"));
    }
    if replay_only["evidence_scope"] != "in_window_causal_prefix_replay_not_independent_holdout" {
        return Err(failure(
            "evidence scope must disclose the replay limitation",
        ));
    }
    let prospective: Vec<Value> = (0..30)
        .map(|index| {
            let (probability, target) = if index % 2 == 0 {
                (0.9 + ((index * index) % 7) as f64 * 0.01, 1)
            } else {
                (0.1 + ((index * index) % 5) as f64 * 0.01, 0)
            };
            edge_record(index, probability, 0.5, target, true, "anchored")
        })
        .collect();
    let supported = directional_edge_assessment(&positive, &prospective);
    if supported["independent_supported"] != Value::Bool(true)
        || supported["supported"] != Value::Bool(true)
    {
        return Err(failure(format!(
            "anchored prospective evidence must support: {supported}"
        )));
    }
    let unanchored: Vec<Value> = (0..30)
        .map(|index| edge_record(index, 0.9, 0.5, 1, false, "unanchored_calendar"))
        .collect();
    let unanchored_result = directional_edge_assessment(&positive, &unanchored);
    if unanchored_result["prospective_nonoverlap_count"].as_i64() != Some(0) {
        return Err(failure(
            "unanchored records must not count as forward evidence",
        ));
    }
    Ok(())
}

fn current_audit_fixture() -> Result<(Vec<Value>, Value), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&closes, &dates);
    let result = quant_result(&dates, &closes, closes.len() - 1);
    Ok((bars, result))
}

fn tampered_fails(
    bars: &[Value],
    result: &Value,
    mutate: impl Fn(&mut Value),
    label: &str,
) -> Result<(), String> {
    let mut candidate = result.clone();
    mutate(&mut candidate);
    let audit = audit_current_prediction(&candidate, Some(bars));
    if audit["pass"] != Value::Bool(false) {
        return Err(failure(format!("{label} was not detected: {audit}")));
    }
    Ok(())
}

/// T12: transform/record tampering is rejected by the independent replay.
pub fn legacy_t12() -> Result<(), String> {
    let (bars, result) = current_audit_fixture()?;
    if audit_current_prediction(&result, Some(&bars))["pass"] != Value::Bool(true) {
        return Err(failure("the untampered prediction must pass the audit"));
    }
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            candidate["prob_up"] =
                Value::from((candidate["prob_up"].as_f64().unwrap_or(0.5) + 0.1).min(0.999));
        },
        "probability",
    )?;
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            candidate["raw_probability"] = Value::from(0.01);
        },
        "raw probability",
    )?;
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            candidate["validation"]["probability_transform"]["matching_origins"] = json!([]);
        },
        "matching origins",
    )?;
    Ok(())
}

/// T13: a transform version or policy version change is rejected.
pub fn legacy_t13() -> Result<(), String> {
    let (bars, result) = current_audit_fixture()?;
    for (field, value) in [
        ("version", "shrinkage_v0"),
        ("policy_version", "old_policy"),
    ] {
        tampered_fails(
            &bars,
            &result,
            |candidate| {
                candidate["validation"]["probability_transform"][field] =
                    Value::String(value.into());
            },
            field,
        )?;
    }
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            candidate["validation"]["probability_transform"]["type"] =
                Value::String("fitted_calibrator".into());
        },
        "transform type",
    )?;
    Ok(())
}

/// T14: fixed policy parameters cannot be changed in a record.
pub fn legacy_t14() -> Result<(), String> {
    let (bars, result) = current_audit_fixture()?;
    for (field, value) in [
        ("k", 1),
        ("minimum_gap_sessions", 1),
        ("minimum_matches", 1),
        ("horizon_sessions", 1),
    ] {
        tampered_fails(
            &bars,
            &result,
            |candidate| {
                candidate["validation"]["probability_transform"][field] = Value::from(value);
            },
            field,
        )?;
    }
    Ok(())
}

/// T17: exactly one production path, shadow candidates are never production votes.
pub fn legacy_t17() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&closes, &dates);
    let quant = quant_result(&dates, &closes, closes.len() - 1);
    let researchers = vec![
        quant,
        json!({"researcher_id": "factor", "status": "success", "prob_up": 0.51, "validation": {}}),
        json!({"researcher_id": "ml", "status": "success", "prob_up": 0.49, "validation": {}}),
        json!({"researcher_id": "factor_backtest", "status": "success", "horizon": "5D",
               "validation": {"information_coefficient": 0.02, "holdout_sample_count": 30}}),
    ];
    let manifest = json!({"snapshot_id": "s", "policy": {"policy_version": policy::POLICY_VERSION,
        "coverage_status": "complete", "calendar_verified": true,
        "target_session": "2026-11-02", "price_basis": "split_adjusted"},
        "data_validation": {"policy": {"price_basis": "split_adjusted"}}});
    let consensus = build_consensus(&ConsensusInput {
        researchers: &researchers,
        manifest: &manifest,
        assessments: None,
        bars: Some(&bars),
        sessions: None,
        prospective_records: &[],
    });
    if consensus["production_path_count"].as_i64() != Some(1)
        || consensus["aggregation_method"] != "single_path"
    {
        return Err(failure(format!(
            "consensus must be single-path: {consensus}"
        )));
    }
    let production: Vec<String> = consensus["paths"]
        .as_array()
        .expect("paths")
        .iter()
        .filter(|row| row["role"] == "production")
        .filter_map(|row| row["path"].as_str().map(str::to_string))
        .collect();
    if production != vec!["quant".to_string()] {
        return Err(failure(format!(
            "production path must be only quant, got {production:?}"
        )));
    }
    for row in consensus["shadow_predictions"].as_array().expect("shadow") {
        if row["role"] != "shadow" || row["production_probability"] != Value::Bool(false) {
            return Err(failure(format!("shadow row must stay shadow-only: {row}")));
        }
    }
    if consensus["confidence_score"] != Value::Null {
        return Err(failure("a confidence score must not be published"));
    }
    Ok(())
}

/// T18: a shadow probability never replaces or blends the production probability.
pub fn legacy_t18() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&closes, &dates);
    let quant = with_oos(
        quant_result(&dates, &closes, closes.len() - 1),
        &oos_rows(&dates, &closes),
    );
    let production_probability = quant["prob_up"].as_f64().expect("production probability");
    let shadow = json!({"researcher_id": "factor", "result_role": "shadow", "status": "success",
        "prob_up": 0.99, "raw_probability": 0.99, "probability_source": "shadow", "validation": {}});
    let manifest = json!({"snapshot_id": "s", "policy": {"policy_version": policy::POLICY_VERSION,
        "coverage_status": "complete", "calendar_verified": true,
        "target_session": "2026-11-02", "price_basis": "split_adjusted"},
        "data_validation": {"policy": {"price_basis": "split_adjusted"}}});
    let consensus = build_consensus(&ConsensusInput {
        researchers: &[quant, shadow],
        manifest: &manifest,
        assessments: None,
        bars: Some(&bars),
        sessions: None,
        prospective_records: &[],
    });
    let consensus_probability = consensus["prob_up"].as_f64().unwrap_or_default();
    if (consensus_probability - production_probability).abs() >= 1e-12 {
        return Err(failure(format!(
            "shadow probability changed the production probability: {consensus_probability} vs {production_probability}"
        )));
    }
    let has_shadow = consensus["shadow_predictions"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .any(|row| row["path"] == "factor" && row["probability"].as_f64() == Some(0.99))
        })
        .unwrap_or(false);
    if !has_shadow {
        return Err(failure("the shadow candidate must still be recorded"));
    }
    Ok(())
}

/// T19: a failed shadow path does not block production.
///
/// The orchestrator path itself is exercised in R11; here the same invariant is asserted
/// on the consensus builder, which is what the orchestrator feeds.
pub fn legacy_t19() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&closes, &dates);
    let quant = with_oos(
        quant_result(&dates, &closes, closes.len() - 1),
        &oos_rows(&dates, &closes),
    );
    let failed_shadow = json!({"researcher_id": "factor", "result_role": "shadow",
        "status": "failed", "prob_up": null, "validation": {}, "warnings": ["shadow exploded"]});
    let manifest = json!({"snapshot_id": "s", "policy": {"policy_version": policy::POLICY_VERSION,
        "coverage_status": "complete", "calendar_verified": true,
        "target_session": "2026-11-02", "price_basis": "split_adjusted"},
        "data_validation": {"policy": {"price_basis": "split_adjusted"}}});
    let consensus = build_consensus(&ConsensusInput {
        researchers: &[quant, failed_shadow],
        manifest: &manifest,
        assessments: None,
        bars: Some(&bars),
        sessions: None,
        prospective_records: &[],
    });
    if consensus["prob_up"].is_null() {
        return Err(failure(
            "a failed shadow path must not block the production probability",
        ));
    }
    Ok(())
}

/// T24: without the production path the result stays unavailable.
pub fn legacy_t24() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&closes, &dates);
    let shadow = json!({"researcher_id": "ml", "result_role": "shadow", "status": "success",
        "prob_up": 0.8, "raw_probability": 0.8, "probability_source": "shadow", "validation": {}});
    let manifest = json!({"snapshot_id": "s", "policy": {"policy_version": policy::POLICY_VERSION,
        "coverage_status": "complete", "calendar_verified": true,
        "target_session": "2026-11-02", "price_basis": "split_adjusted"},
        "data_validation": {"policy": {"price_basis": "split_adjusted"}}});
    let consensus = build_consensus(&ConsensusInput {
        researchers: &[shadow],
        manifest: &manifest,
        assessments: None,
        bars: Some(&bars),
        sessions: None,
        prospective_records: &[],
    });
    if !consensus["prob_up"].is_null()
        || consensus["production_probability_eligible"] != Value::Bool(false)
        || consensus["status"] != "unavailable"
    {
        return Err(failure(format!(
            "a shadow-only run must stay unavailable: {consensus}"
        )));
    }
    Ok(())
}

/// T29: deliberate formula mutations must be caught by the audit.
pub fn legacy_t29() -> Result<(), String> {
    let (bars, result) = current_audit_fixture()?;
    if audit_current_prediction(&result, Some(&bars))["pass"] != Value::Bool(true) {
        return Err(failure("the honest prediction must first pass"));
    }
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            candidate["validation"]["probability_transform"]["k"] = Value::from(0);
        },
        "prior->0",
    )?;
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            let flipped = 1.0 - candidate["prob_up"].as_f64().unwrap_or(0.5);
            candidate["prob_up"] = Value::from(flipped);
        },
        "sign flip",
    )?;
    let last_date = bars
        .last()
        .and_then(|bar| bar["date"].as_str())
        .unwrap_or("")
        .to_string();
    let previous_date = bars
        .get(bars.len().saturating_sub(2))
        .and_then(|bar| bar["date"].as_str())
        .unwrap_or("")
        .to_string();
    tampered_fails(
        &bars,
        &result,
        |candidate| {
            let current = candidate["validation"]["probability_transform"]
                ["base_rate_label_end_max"]
                .as_str()
                .unwrap_or("")
                .to_string();
            let replacement = if current != last_date {
                last_date.clone()
            } else {
                previous_date.clone()
            };
            candidate["validation"]["probability_transform"]["base_rate_label_end_max"] =
                Value::String(replacement);
        },
        "label shift",
    )?;
    Ok(())
}

/// T30: effectiveness is reported against the causal base rate and against 0.5.
pub fn legacy_t30() -> Result<(), String> {
    let build = |probability: f64| -> Vec<Value> {
        (0..60)
            .map(|index| {
                json!({"decision_session": format!("2026-01-{:02}", index + 1),
                       "status": "matured",
                       "probability": probability + (index % 5) as f64 * 0.002,
                       "target": if index % 10 < 3 { 0 } else { 1 },
                       "probability_transform": {"base_rate_at_origin": 0.5}})
            })
            .collect()
    };
    let rows = build(0.20);
    let comparison = baseline_comparison(&rows, 5, None);
    if comparison["brier_model"].as_f64().unwrap_or(0.0)
        <= comparison["brier_causal_base_rate"].as_f64().unwrap_or(0.0)
    {
        return Err(failure(
            "a badly calibrated model must have the higher Brier score",
        ));
    }
    if comparison["skill"]["vs_causal_base_rate"]
        .as_f64()
        .unwrap_or(0.0)
        >= 0.0
    {
        return Err(failure("negative skill must be reported"));
    }
    if comparison["auc"].is_null() {
        return Err(failure(
            "AUC must be computed when both classes are present",
        ));
    }
    if comparison["baselines"].get("constant_half").is_none() {
        return Err(failure("the constant-0.5 baseline must be disclosed"));
    }
    let edge = directional_edge_assessment(&rows, &[]);
    if edge["state"] != "negative_skill_with_estimated_interval_excluding_zero" {
        return Err(failure(format!("negative edge state expected, got {edge}")));
    }
    let better = baseline_comparison(&build(0.72), 5, None);
    if better["skill"]["vs_causal_base_rate"]
        .as_f64()
        .unwrap_or(0.0)
        <= 0.0
    {
        return Err(failure("a well-calibrated model must beat the base rate"));
    }
    let verdict = better["verdict"].as_str().unwrap_or("");
    if verdict != "insufficient_evidence"
        && verdict != "positive_skill_with_estimated_interval_excluding_zero"
    {
        return Err(failure(format!(
            "unexpected verdict for point-positive skill: {verdict}"
        )));
    }
    Ok(())
}

/// T38: an audit veto leaves no probability or lean anywhere in the payload.
pub fn legacy_t38() -> Result<(), String> {
    let mut consensus = json!({
        "prob_up": 0.61,
        "raw_probability": 0.61,
        "reportable_probability": 0.61,
        "model_lean": "up",
        "direction": "bullish",
        "probability_basis": "drift_shrunk_causal_base_rate",
        "production": {"prob_up": 0.61, "raw_probability": 0.61, "model_lean": "up",
                       "probability_transform": {"version": "drift_shrinkage_v2", "n_eff": 49.0}},
        "prediction": {"status": "available", "prob_up": 0.61, "model_lean": "up"}
    });
    let audit = json!({"reason_codes": ["CALENDAR_UNVERIFIED"], "vetoes": ["calendar missing"]});
    redact_publishable_prediction(&mut consensus, &audit);
    for (label, container) in [
        ("root", &consensus),
        ("production", &consensus["production"]),
        ("prediction", &consensus["prediction"]),
    ] {
        for key in [
            "prob_up",
            "raw_probability",
            "reportable_probability",
            "calibrated_probability",
        ] {
            if !container[key].is_null() {
                return Err(failure(format!(
                    "{label}.{key} still carries a value after a veto"
                )));
            }
        }
    }
    if consensus["model_lean"] != "unavailable"
        || consensus["prediction"]["model_lean"] != "unavailable"
    {
        return Err(failure("the lean must be unavailable after a veto"));
    }
    if consensus["production"]
        .get("probability_transform")
        .is_some()
    {
        return Err(failure(
            "the transform must move to audit_only after a veto",
        ));
    }
    if consensus["audit_only"]["publishable"] != Value::Bool(false)
        || consensus["redaction"]["publishable"] != Value::Bool(false)
    {
        return Err(failure(
            "audit-only and redaction blocks must mark nothing publishable",
        ));
    }
    Ok(())
}

/// T33: a probability against an undetermined target session is vetoed.
pub fn legacy_t33() -> Result<(), String> {
    let mut manifest = json!({
        "snapshot_id": "SYN_TEST",
        "source_payload_sha256": "a".repeat(64),
        "request": {"intent": "forecast"},
        "policy": {"policy_version": policy::POLICY_VERSION, "coverage_status": "complete",
                   "calendar_verified": true, "target_session": null},
        "data_validation": {"policy": {"price_basis": "split_adjusted"}},
        "data_quality": {"overall": "complete"},
    });
    if !manifest_policy_reasons(&manifest)
        .iter()
        .any(|code| code == "TARGET_SESSION_UNRESOLVED")
    {
        return Err(failure(
            "an unresolved target session must be a policy reason",
        ));
    }
    let consensus = json!({
        "prob_up": 0.55, "production_probability_eligible": true, "computable": true,
        "current_prediction_integrity_pass": true, "oos_integrity_pass": true,
        "directional_edge_supported": false, "aggregation_method": "single_path",
        "production_path_count": 1, "confidence_score": null,
        "assessment": {"current_prediction_integrity_pass": true, "oos_integrity_pass": true,
                       "production_probability_eligible": true, "directional_edge_supported": false},
        "shadow_predictions": [], "evidence": {},
    });
    let result = audit(&AuditInput {
        manifest: &manifest,
        researchers: &[],
        consensus: &consensus,
        news: None,
        prospective_records: None,
    });
    if !code_list(&result["reason_codes"])
        .iter()
        .any(|code| code == "TARGET_SESSION_UNRESOLVED")
    {
        return Err(failure(format!(
            "TARGET_SESSION_UNRESOLVED missing from audit: {}",
            result["reason_codes"]
        )));
    }
    if result["may_report_probability"] != Value::Bool(false) {
        return Err(failure("an unresolved target must veto publication"));
    }
    manifest["policy"]["target_session"] = Value::String("2026-01-09".into());
    if manifest_policy_reasons(&manifest)
        .iter()
        .any(|code| code == "TARGET_SESSION_UNRESOLVED")
    {
        return Err(failure("a resolved target must clear the reason"));
    }
    Ok(())
}

/// F01: the current probability is not the last ledger row overwriting a refit prediction.
pub fn legacy_f01() -> Result<(), String> {
    // `synth_bars(640, 11)` from selfcheck.py: same deterministic price walk.
    let mut rng = PythonRandom::new(11);
    let mut price = 100.0f64;
    let mut day = NaiveDate::from_ymd_opt(2023, 1, 2).expect("start date");
    let mut bars: Vec<Value> = Vec::new();
    while bars.len() < 640 {
        if day.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            price *= rng.gauss(0.0005, 0.018).exp();
            let round4 = |value: f64| (value * 10_000.0).round() / 10_000.0;
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
    let market = json!({
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
    });
    let manifest = json!({
        "ticker": "SYN",
        "horizon": "5D",
        "snapshot_id": "synth_640",
        "market": "US",
    });
    let result = ml::run(&manifest, &market)?;
    let status = result["status"].as_str().unwrap_or("");
    if status != "success" && status != "partial" {
        return Err(failure(format!(
            "the ML path must publish a probability on this prefix, got status={status}"
        )));
    }
    let rows = result["validation"]["oos_forecasts"]
        .as_array()
        .ok_or_else(|| failure("the ML result must carry the OOS ledger"))?;
    if rows.is_empty() {
        return Err(failure("the OOS ledger must not be empty"));
    }
    let calibration = &result["validation"]["current_prediction_calibration"];
    let raw = result["raw_probability"]
        .as_f64()
        .ok_or_else(|| failure("the ML result must publish a raw probability"))?;
    let expected = if calibration["applied"].as_bool() == Some(true) {
        let intercept = calibration["intercept"]
            .as_f64()
            .ok_or_else(|| failure("applied calibration must publish the intercept"))?;
        let slope = calibration["slope"]
            .as_f64()
            .ok_or_else(|| failure("applied calibration must publish the slope"))?;
        let logit = (raw / (1.0 - raw)).ln();
        1.0 / (1.0 + (-(intercept + slope * logit)).exp())
    } else {
        raw
    };
    let published = result["prob_up"]
        .as_f64()
        .ok_or_else(|| failure("the ML result must publish prob_up"))?;
    if (published - expected).abs() >= 1e-9 {
        return Err(failure(format!(
            "published probability {published} != expected {expected} from the current refit"
        )));
    }
    if let Some(last) = rows.last().and_then(|row| row["probability"].as_f64()) {
        if (published - last).abs() <= 1e-12 {
            return Err(failure(
                "prob_up equals the last ledger row: the current prediction was clobbered",
            ));
        }
    }
    Ok(())
}

/// Implemented R05 checks (T17/T19 assert the consensus invariant until R11).
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T09", legacy_t09 as CheckFn),
        ("T10", legacy_t10 as CheckFn),
        ("T12", legacy_t12 as CheckFn),
        ("T13", legacy_t13 as CheckFn),
        ("T14", legacy_t14 as CheckFn),
        ("T17", legacy_t17 as CheckFn),
        ("T18", legacy_t18 as CheckFn),
        ("T19", legacy_t19 as CheckFn),
        ("T24", legacy_t24 as CheckFn),
        ("T29", legacy_t29 as CheckFn),
        ("T30", legacy_t30 as CheckFn),
        ("T38", legacy_t38 as CheckFn),
        ("T33", legacy_t33 as CheckFn),
        ("F01", legacy_f01 as CheckFn),
    ]
}
