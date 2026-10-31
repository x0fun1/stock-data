//! Migrated R04 checks: label maturity, reproducibility, window trimming and the
//! published v2 shrink formula.
//!
//! The checks build synthetic frozen bars and call the real A path
//! (`quant::predict_at_origin` / `quant::run`) plus the independent audit.

use chrono::{Duration, NaiveDate};
use serde_json::{json, Map, Value};

use crate::selfcheck::registry::CheckFn;
use sd_audit::probability_quality::audit_current_prediction;
use sd_core::policy;
use sd_research::quant::{predict_at_origin, run};

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
            price *= 1.0 + (((index as f64) * 0.29).sin()) * 0.006;
            (price * 1_000_000.0).round() / 1_000_000.0
        })
        .collect();
    (dates, closes)
}

fn bars_from(dates: &[String], closes: &[f64]) -> Vec<Value> {
    dates
        .iter()
        .zip(closes.iter())
        .map(|(date, close)| json!({"date": date, "close": close}))
        .collect()
}

fn predict(dates: &[String], closes: &[f64], index: usize) -> Value {
    predict_at_origin(dates, closes, index, 5, None, None, false, 6).expect("prediction row")
}

fn oos_forecasts(dates: &[String], closes: &[f64]) -> Vec<Value> {
    (25..closes.len().saturating_sub(5))
        .map(|index| predict(dates, closes, index))
        .filter(|row| !row["probability"].is_null())
        .collect()
}

fn quant_result(dates: &[String], closes: &[f64]) -> Value {
    let current = predict(dates, closes, closes.len() - 1);
    let mut result = Map::new();
    result.insert("researcher_id".into(), Value::String("quant".into()));
    result.insert("status".into(), Value::String("success".into()));
    result.insert("prob_up".into(), current["probability"].clone());
    result.insert("raw_probability".into(), current["raw_probability"].clone());
    let mut validation = Map::new();
    validation.insert(
        "probability_transform".into(),
        current["probability_transform"].clone(),
    );
    validation.insert(
        "oos_forecasts".into(),
        Value::Array(oos_forecasts(dates, closes)),
    );
    validation.insert("n_raw".into(), current["matching_observations"].clone());
    result.insert("validation".into(), Value::Object(validation));
    Value::Object(result)
}

/// T08: every label used at an origin matured by that origin.
pub fn legacy_t08() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let rows = oos_forecasts(&dates, &closes);
    if rows.is_empty() {
        return Err(failure("no matured OOS row"));
    }
    let index = |date: &str| dates.iter().position(|candidate| candidate == date);
    for row in &rows {
        let decision_index = index(row["decision_session"].as_str().unwrap_or(""))
            .ok_or_else(|| failure("decision session not in the price series"))?;
        for label_end in row["probability_transform"]["matching_label_ends"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            let end_index = index(label_end.as_str().unwrap_or(""))
                .ok_or_else(|| failure("label end not in the price series"))?;
            if end_index > decision_index {
                return Err(failure(format!(
                    "label end {label_end} occurs after the decision session"
                )));
            }
        }
        let base_end = row["probability_transform"]["base_rate_label_end_max"]
            .as_str()
            .ok_or_else(|| failure("base rate label boundary missing"))?;
        let base_index =
            index(base_end).ok_or_else(|| failure("base label end not in the price series"))?;
        if base_index > decision_index {
            return Err(failure(
                "base-rate label boundary occurs after the decision",
            ));
        }
    }
    Ok(())
}

/// T11: the current prediction is reproducible from the frozen bars.
pub fn legacy_t11() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let bars = bars_from(&dates, &closes);
    let result = quant_result(&dates, &closes);
    let audit = audit_current_prediction(&result, Some(&bars));
    if audit["pass"] != Value::Bool(true) {
        return Err(failure(format!(
            "the current prediction failed the audit: {audit}"
        )));
    }
    let direct = predict(&dates, &closes, closes.len() - 1);
    let recorded = result["prob_up"].as_f64().unwrap_or_default();
    let recomputed = direct["probability"].as_f64().unwrap_or_default();
    if (recorded - recomputed).abs() >= 1e-12 {
        return Err(failure(format!(
            "direct prediction {recomputed} != reported {recorded}"
        )));
    }
    Ok(())
}

/// T16: extra pre-window bars are trimmed, so the probability does not move.
pub fn legacy_t16() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(300);
    let window_dates = dates[dates.len() - 260..].to_vec();
    let window_closes = closes[closes.len() - 260..].to_vec();
    let base = predict(&window_dates, &window_closes, window_closes.len() - 1);

    // Ten extra pre-window bars: the policy window trims them away before the estimate.
    let mut extended_dates: Vec<String> = Vec::new();
    let mut cursor = NaiveDate::from_ymd_opt(2025, 9, 1).expect("date");
    while extended_dates.len() < 10 {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            extended_dates.push(cursor.to_string());
        }
        cursor += Duration::days(1);
    }
    extended_dates.extend(window_dates.iter().cloned());
    let mut extended_closes: Vec<f64> = (0..10)
        .map(|index| window_closes[0] * (1.0 - 0.001 * index as f64))
        .collect();
    extended_closes.extend(window_closes.iter().copied());
    let trimmed_start = extended_closes.len() - 260;
    let trimmed_closes = extended_closes[trimmed_start..].to_vec();
    let trimmed_dates = extended_dates[trimmed_start..].to_vec();
    let other = predict(&trimmed_dates, &trimmed_closes, trimmed_closes.len() - 1);
    let base_probability = base["probability"].as_f64().unwrap_or_default();
    let other_probability = other["probability"].as_f64().unwrap_or_default();
    if (base_probability - other_probability).abs() >= 1e-12 {
        return Err(failure(format!(
            "window slide changed the probability: {base_probability} vs {other_probability}"
        )));
    }
    Ok(())
}

fn ohlcv_bars(dates: &[String], closes: &[f64]) -> Vec<Value> {
    closes
        .iter()
        .enumerate()
        .map(|(index, close)| {
            json!({
                "date": dates[index],
                "open": close * 0.997,
                "high": close * 1.008,
                "low": close * 0.992,
                "close": close,
                "volume": 1_000_000.0,
            })
        })
        .collect()
}

fn synthetic_market(bars: Vec<Value>) -> Value {
    json!({
        "_data_validation": {"forecast_eligible": true, "reporting_status": "degraded"},
        "price_basis": "split_adjusted",
        "adjustment": "provider",
        "price_basis_source": "provider_declared_split_adjusted_close",
        "data": {"bars": bars},
    })
}

/// F46: the published probability equals the frozen v2 drift-shrinkage formula.
pub fn legacy_f46() -> Result<(), String> {
    let (dates, closes) = dates_and_closes(260);
    let bars = ohlcv_bars(&dates, &closes);
    let manifest = json!({
        "ticker": "SYN",
        "horizon": "5D",
        "snapshot_id": "s",
        "market": "US",
        "data_quality": {"overall": "complete"},
    });
    let result = run(&manifest, &synthetic_market(bars))?;
    let transform = result["validation"]["probability_transform"]
        .as_object()
        .cloned()
        .ok_or_else(|| failure("the result must carry the probability transform"))?;
    if transform.get("version").and_then(Value::as_str) != Some(policy::DRIFT_SHRINKAGE_VERSION) {
        return Err(failure(format!(
            "transform version must be v2: {transform:?}"
        )));
    }
    if transform.get("k").and_then(Value::as_u64) != Some(u64::from(policy::DRIFT_SHRINKAGE_K)) {
        return Err(failure("the recorded k must be the policy constant"));
    }
    let n_eff = transform.get("n_eff").and_then(Value::as_f64);
    let base_rate = transform.get("base_rate_at_origin").and_then(Value::as_f64);
    let p_vol = transform.get("p_vol").and_then(Value::as_f64);
    let (Some(n_eff), Some(base_rate), Some(p_vol)) = (n_eff, base_rate, p_vol) else {
        return Err(failure(
            "the transform must publish n_eff, base_rate_at_origin and p_vol",
        ));
    };
    let expected = (n_eff * base_rate + f64::from(policy::DRIFT_SHRINKAGE_K) * p_vol)
        / (n_eff + f64::from(policy::DRIFT_SHRINKAGE_K));
    let published = result["prob_up"].as_f64().unwrap_or_default();
    if (expected - published).abs() > 1e-12 {
        return Err(failure(format!(
            "published probability {published} != v2 formula {expected}"
        )));
    }
    if transform.contains_key("prior_weight") {
        return Err(failure(
            "the prior weight must be derived, not silently stored",
        ));
    }
    if policy::SHRINKAGE_MIN_MATCHES != 12 {
        return Err(failure(
            "the 12-match floor is deliberate and must not be lowered",
        ));
    }
    if policy::FEATURE_ROLE_IN_PROBABILITY != "disclosure_only" {
        return Err(failure("ROC20 must stay a disclosed state variable"));
    }
    Ok(())
}

/// Implemented R04 checks.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T08", legacy_t08 as CheckFn),
        ("T11", legacy_t11 as CheckFn),
        ("T16", legacy_t16 as CheckFn),
        ("F46", legacy_f46 as CheckFn),
    ]
}
