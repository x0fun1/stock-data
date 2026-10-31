//! Audit of probability transforms, evidence support and predictive skill.
//!
//! Port of `validation/probability_quality.py`. Two independent jobs that must never be
//! traded for one another:
//!
//! 1. **Correctness / reproducibility** — rebuild the reported probability from the
//!    frozen split-adjusted closes. This module deliberately does *not* call the
//!    researcher's implementation: an audit sharing the producer's function can only
//!    detect record tampering, never a wrong formula.
//! 2. **Effectiveness** — compare rebuilt probabilities with two naive baselines and
//!    report Brier skill, AUC and a paired loss-improvement interval.
//!
//! `sd-audit` does not depend on `sd-research`, so sharing the estimator is structurally
//! impossible; the arithmetic below is an independent re-implementation on purpose.

use chrono::NaiveDate;
use serde_json::{Map, Value};

use sd_core::float_sum::python_sum;
use sd_core::market_data::fixed_phase_sample;
use sd_core::policy;
use sd_core::statistics::{
    baseline_comparison, block_lengths_for, bootstrap_mean_interval, paired_rows,
};

/// Absolute tolerance when comparing a recomputed probability with the record.
pub const PROBABILITY_TOLERANCE: f64 = 1e-10;
/// Default Brier-skill epsilon of the legacy compatibility shim.
pub const DEFAULT_BRIER_SKILL_EPSILON: f64 = 1e-9;
/// Effectiveness verdict: interval excludes zero on the positive side.
pub const EFFECTIVENESS_VERDICT_POSITIVE: &str =
    "positive_skill_with_estimated_interval_excluding_zero";
/// Effectiveness verdict: interval excludes zero on the negative side.
pub const EFFECTIVENESS_VERDICT_NEGATIVE: &str =
    "negative_skill_with_estimated_interval_excluding_zero";
/// Effectiveness verdict: not enough evidence.
pub const EFFECTIVENESS_VERDICT_INCONCLUSIVE: &str = "insufficient_evidence";

fn float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

fn string(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        _ => None,
    }
}

fn object(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn close_diff(left: Option<&Value>, right: Option<&Value>) -> bool {
    match (float(left), float(right)) {
        (Some(left), Some(right)) => (left - right).abs() <= PROBABILITY_TOLERANCE,
        _ => false,
    }
}

fn record_reasons(row: &Map<String, Value>, required: &[&str]) -> Vec<String> {
    let mut fields: Vec<&str> = required.to_vec();
    fields.sort_unstable();
    fields
        .into_iter()
        .filter(|field| !row.contains_key(*field))
        .map(|field| format!("MISSING_FIELD:{field}"))
        .collect()
}

/// Ordered date/close series with the legacy validation contract.
fn series(bars: &[Value]) -> Result<(Vec<String>, Vec<f64>), Vec<String>> {
    let mut dates = Vec::with_capacity(bars.len());
    let mut closes = Vec::with_capacity(bars.len());
    for bar in bars {
        let Some(close) = float(bar.get("close")) else {
            return Err(vec!["PRICE_SERIES_INVALID".into()]);
        };
        let date = string(bar.get("date")).unwrap_or_default();
        let date = date.get(..10).unwrap_or(&date).to_string();
        dates.push(date);
        closes.push(close);
    }
    let mut seen: Vec<&String> = Vec::new();
    for date in &dates {
        if seen.contains(&date) {
            return Err(vec!["PRICE_SERIES_INVALID".into()]);
        }
        seen.push(date);
    }
    if dates.windows(2).any(|pair| pair[0] > pair[1]) {
        return Err(vec!["PRICE_SERIES_INVALID".into()]);
    }
    Ok((dates, closes))
}

fn forward_return(closes: &[f64], index: i64, horizon: i64) -> Option<f64> {
    let target = index + horizon;
    if index < 0 || target >= closes.len() as i64 || closes[index as usize] <= 0.0 {
        return None;
    }
    Some(closes[target as usize] / closes[index as usize] - 1.0)
}

fn roc(closes: &[f64], index: i64, window: i64) -> Option<f64> {
    let prior = index - window;
    if prior < 0 || closes[prior as usize] <= 0.0 {
        return None;
    }
    Some(closes[index as usize] / closes[prior as usize] - 1.0)
}

fn recompute_v2(dates: &[String], closes: &[f64], index: i64, horizon: i64) -> Value {
    let origin_high = 0.max(index - horizon + 1);
    let origins: Vec<i64> = (0..origin_high)
        .filter(|origin| forward_return(closes, *origin, horizon).is_some())
        .collect();
    let base_rate = if origins.is_empty() {
        None
    } else {
        let wins = origins
            .iter()
            .filter(|origin| forward_return(closes, **origin, horizon).unwrap_or(0.0) > 0.0)
            .count();
        Some(wins as f64 / origins.len() as f64)
    };
    let n_eff = if origins.is_empty() {
        0.0
    } else {
        origins.len() as f64 / horizon as f64
    };
    let start = 1.max(index - i64::from(policy::DRIFT_VOL_WINDOW_SESSIONS) + 1);
    let mut returns: Vec<f64> = Vec::new();
    for position in start..=index {
        if closes[(position - 1) as usize] > 0.0 && closes[position as usize] > 0.0 {
            returns.push((closes[position as usize] / closes[(position - 1) as usize]).ln());
        }
    }
    let mut drift = Value::Null;
    if returns.len() >= policy::DRIFT_MIN_VOL_SESSIONS as usize {
        let centre = python_sum(returns.iter().copied()) / returns.len() as f64;
        let variance = python_sum(
            returns
                .iter()
                .map(|value| (value - centre) * (value - centre)),
        ) / (returns.len() - 1) as f64;
        if variance > 0.0 {
            let sigma = variance.sqrt();
            let z_score = (policy::DRIFT_MU_ANNUAL / policy::SESSIONS_PER_YEAR) * horizon as f64
                / (sigma * (horizon as f64).sqrt());
            let mut map = Map::new();
            map.insert("mu_annual".into(), Value::from(policy::DRIFT_MU_ANNUAL));
            map.insert(
                "mu_per_session".into(),
                Value::from(policy::DRIFT_MU_ANNUAL / policy::SESSIONS_PER_YEAR),
            );
            map.insert("sigma_daily".into(), Value::from(sigma));
            map.insert(
                "sigma_horizon".into(),
                Value::from(sigma * (horizon as f64).sqrt()),
            );
            map.insert("z_score".into(), Value::from(z_score));
            map.insert(
                "p_vol".into(),
                Value::from(0.5 * (1.0 + erf(z_score / std::f64::consts::SQRT_2))),
            );
            map.insert(
                "vol_window_sessions".into(),
                Value::from(policy::DRIFT_VOL_WINDOW_SESSIONS),
            );
            map.insert("return_count".into(), Value::from(returns.len()));
            map.insert("return_window_start".into(), Value::from(start));
            map.insert("return_window_end".into(), Value::from(index));
            drift = Value::Object(map);
        }
    }
    let p_vol = drift.get("p_vol").and_then(Value::as_f64);
    let probability = match (base_rate, p_vol) {
        (Some(base_rate), Some(p_vol)) => Some(
            (n_eff * base_rate + f64::from(policy::DRIFT_SHRINKAGE_K) * p_vol)
                / (n_eff + f64::from(policy::DRIFT_SHRINKAGE_K)),
        ),
        _ => None,
    };
    let mut result = Map::new();
    result.insert(
        "base_rate".into(),
        base_rate.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert("base_rate_label_count".into(), Value::from(origins.len()));
    result.insert(
        "base_rate_label_end_max".into(),
        origins
            .last()
            .and_then(|origin| dates.get((origin + horizon) as usize))
            .map(|date| Value::String(date.clone()))
            .unwrap_or(Value::Null),
    );
    result.insert("n_eff".into(), Value::from(n_eff));
    result.insert("drift".into(), drift);
    result.insert(
        "p_vol".into(),
        p_vol.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "probability".into(),
        probability.map(Value::from).unwrap_or(Value::Null),
    );
    Value::Object(result)
}

/// Local `erf`, identical in intent to the production one but owned by the audit.
fn erf(value: f64) -> f64 {
    let x = value.abs();
    if x < 2.5 {
        let mut term = value;
        let mut sum = value;
        let mut n = 1usize;
        while n < 60 {
            term *= -(value * value) / (n as f64);
            let contribution = term / (2 * n + 1) as f64;
            sum += contribution;
            if contribution.abs() < f64::EPSILON * sum.abs() {
                break;
            }
            n += 1;
        }
        return 2.0 / std::f64::consts::PI.sqrt() * sum;
    }
    let mut result = 0.0f64;
    let mut c: f64 = 1.0;
    let mut d: f64 = 0.0;
    for k in 1..200 {
        let factor = k as f64 * 0.5;
        d = x + d * factor;
        d = 1.0 / d;
        c = x + factor / c.max(f64::MIN_POSITIVE);
        let delta = c * d;
        result = delta;
        if (delta - 1.0).abs() < 1e-17 {
            break;
        }
    }
    let erfc = (-x * x).exp() / (std::f64::consts::PI.sqrt() * x) * result;
    if value >= 0.0 {
        1.0 - erfc
    } else {
        erfc - 1.0
    }
}

fn recompute_matching(
    dates: &[String],
    closes: &[f64],
    index: i64,
    horizon: i64,
    gap: i64,
) -> Value {
    let Some(current) = roc(closes, index, 20) else {
        let mut map = Map::new();
        map.insert("origins".into(), Value::Array(Vec::new()));
        map.insert("label_ends".into(), Value::Array(Vec::new()));
        map.insert("wins".into(), Value::from(0));
        map.insert("observations".into(), Value::from(0));
        return Value::Object(map);
    };
    let positive = current >= 0.0;
    let mut selected: Vec<i64> = Vec::new();
    let mut last: Option<i64> = None;
    let limit = index.min(closes.len() as i64);
    for prior in 20..limit {
        if prior + horizon > index {
            continue;
        }
        if forward_return(closes, prior, horizon).is_none() {
            continue;
        }
        let Some(prior_signal) = roc(closes, prior, 20) else {
            continue;
        };
        if (prior_signal >= 0.0) != positive {
            continue;
        }
        if last.map(|last| prior - last < gap).unwrap_or(false) {
            continue;
        }
        selected.push(prior);
        last = Some(prior);
    }
    let wins = selected
        .iter()
        .filter(|prior| forward_return(closes, **prior, horizon).unwrap_or(0.0) > 0.0)
        .count();
    let mut map = Map::new();
    map.insert(
        "origins".into(),
        Value::Array(
            selected
                .iter()
                .filter_map(|prior| dates.get(*prior as usize).cloned().map(Value::String))
                .collect(),
        ),
    );
    map.insert(
        "label_ends".into(),
        Value::Array(
            selected
                .iter()
                .filter_map(|prior| {
                    dates
                        .get((prior + horizon) as usize)
                        .cloned()
                        .map(Value::String)
                })
                .collect(),
        ),
    );
    map.insert("wins".into(), Value::from(wins));
    map.insert("observations".into(), Value::from(selected.len()));
    Value::Object(map)
}

fn recompute_legacy_v1(
    dates: &[String],
    closes: &[f64],
    index: i64,
    horizon: i64,
    gap: i64,
) -> Value {
    let origin_high = 0.max(index - horizon + 1);
    let origins: Vec<i64> = (0..origin_high)
        .filter(|origin| forward_return(closes, *origin, horizon).is_some())
        .collect();
    let base_rate = if origins.is_empty() {
        None
    } else {
        let wins = origins
            .iter()
            .filter(|origin| forward_return(closes, **origin, horizon).unwrap_or(0.0) > 0.0)
            .count();
        Some(wins as f64 / origins.len() as f64)
    };
    let matching = recompute_matching(dates, closes, index, horizon, gap);
    let observations = matching
        .get("observations")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let wins = matching.get("wins").and_then(Value::as_i64).unwrap_or(0);
    let reason = if observations > 0 {
        Some(wins as f64 / observations as f64)
    } else {
        None
    };
    let probability = match (base_rate, reason) {
        (Some(base_rate), Some(_)) if observations >= i64::from(policy::SHRINKAGE_MIN_MATCHES) => {
            Some(
                (wins as f64 + f64::from(policy::SHRINKAGE_PRIOR_STRENGTH) * base_rate)
                    / (observations as f64 + f64::from(policy::SHRINKAGE_PRIOR_STRENGTH)),
            )
        }
        _ => None,
    };
    let mut result = Map::new();
    result.insert(
        "base_rate".into(),
        base_rate.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert("base_rate_label_count".into(), Value::from(origins.len()));
    result.insert(
        "base_rate_label_end_max".into(),
        origins
            .last()
            .and_then(|origin| dates.get((origin + horizon) as usize))
            .map(|date| Value::String(date.clone()))
            .unwrap_or(Value::Null),
    );
    result.insert("matching".into(), matching);
    result.insert(
        "probability".into(),
        probability.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "raw_probability".into(),
        reason.map(Value::from).unwrap_or(Value::Null),
    );
    Value::Object(result)
}

fn int_field(transform: &Map<String, Value>, key: &str, default: i64) -> i64 {
    match transform.get(key) {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64))
            .unwrap_or(default),
        Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(default),
        _ => default,
    }
}

/// Rebuild one shrinkage prediction from the frozen series and compare.
pub fn audit_shrinkage_record(
    row: &Map<String, Value>,
    dates: &[String],
    closes: &[f64],
    require_full_52w: bool,
) -> Value {
    let Some(Value::Object(transform)) = row.get("probability_transform") else {
        let mut map = Map::new();
        map.insert("ok".into(), Value::Bool(false));
        map.insert(
            "reasons".into(),
            Value::Array(vec![Value::String("MISSING_PROBABILITY_TRANSFORM".into())]),
        );
        map.insert("recomputed".into(), Value::Null);
        return Value::Object(map);
    };
    let kind = transform.get("type").and_then(Value::as_str).unwrap_or("");
    if kind == "causal_empirical_shrinkage" {
        return audit_legacy_v1(row, dates, closes, transform, require_full_52w);
    }
    if kind != "causal_drift_shrinkage" {
        let mut map = Map::new();
        map.insert("ok".into(), Value::Bool(false));
        map.insert(
            "reasons".into(),
            Value::Array(vec![Value::String(format!(
                "UNKNOWN_TRANSFORM_TYPE:{}",
                sd_core::contracts::py_repr(transform.get("type").unwrap_or(&Value::Null))
            ))]),
        );
        map.insert("recomputed".into(), Value::Null);
        return Value::Object(map);
    }
    let mut reasons: Vec<String> = Vec::new();
    if transform.get("version").and_then(Value::as_str) != Some(policy::DRIFT_SHRINKAGE_VERSION) {
        reasons.push(format!(
            "UNKNOWN_TRANSFORM_VERSION:{}",
            sd_core::contracts::py_repr(transform.get("version").unwrap_or(&Value::Null))
        ));
    }
    if transform.get("policy_version").and_then(Value::as_str) != Some(policy::POLICY_VERSION) {
        reasons.push("TRANSFORM_POLICY_VERSION_MISMATCH".into());
    }
    if int_field(transform, "k", -1) != i64::from(policy::DRIFT_SHRINKAGE_K) {
        reasons.push("PRIOR_STRENGTH_TAMPERED".into());
    }
    if int_field(transform, "minimum_gap_sessions", -1)
        != i64::from(policy::NONOVERLAP_GAP_SESSIONS)
    {
        reasons.push("MATCHING_GAP_TAMPERED".into());
    }
    if int_field(transform, "horizon_sessions", -1) != i64::from(policy::FORECAST_HORIZON_SESSIONS)
    {
        reasons.push("TRANSFORM_HORIZON_TAMPERED".into());
    }
    if int_field(transform, "minimum_matches", -1) != i64::from(policy::SHRINKAGE_MIN_MATCHES) {
        reasons.push("MINIMUM_MATCHES_TAMPERED".into());
    }
    let feature = object(transform.get("feature"));
    if feature.get("used_for_probability") != Some(&Value::Bool(false)) {
        reasons.push("FEATURE_ROLE_TAMPERED".into());
    }
    let Some(decision) = string(row.get("decision_session")) else {
        reasons.push("DECISION_SESSION_NOT_IN_PRICE_SERIES".into());
        return audit_shrinkage_result(None, reasons);
    };
    let Some(index) = dates
        .iter()
        .position(|date| date == &decision)
        .map(|position| position as i64)
    else {
        reasons.push("DECISION_SESSION_NOT_IN_PRICE_SERIES".into());
        return audit_shrinkage_result(None, reasons);
    };
    if let Some(recorded_index) = row.get("decision_index").and_then(Value::as_i64) {
        if recorded_index != index {
            reasons.push("DECISION_INDEX_TAMPERED".into());
        }
    }
    if require_full_52w && row.get("full_52w_input") != Some(&Value::Bool(true)) {
        reasons.push("NOT_FULL_52W_INPUT".into());
    }
    let expected = recompute_v2(
        dates,
        closes,
        index,
        i64::from(policy::FORECAST_HORIZON_SESSIONS),
    );
    let matching = recompute_matching(
        dates,
        closes,
        index,
        i64::from(policy::FORECAST_HORIZON_SESSIONS),
        i64::from(policy::NONOVERLAP_GAP_SESSIONS),
    );
    let drift = object(expected.get("drift"));
    let recorded_drift = object(transform.get("drift_prior"));
    if !close_diff(
        transform.get("base_rate_at_origin"),
        expected.get("base_rate"),
    ) {
        reasons.push("BASE_RATE_AT_ORIGIN_TAMPERED".into());
    }
    if transform.get("base_rate_label_count") != expected.get("base_rate_label_count") {
        reasons.push("BASE_RATE_SAMPLE_REWRITTEN".into());
    }
    if transform.get("base_rate_label_end_max") != expected.get("base_rate_label_end_max") {
        reasons.push("BASE_RATE_LABEL_BOUNDARY_TAMPERED".into());
    }
    if !close_diff(transform.get("n_eff"), expected.get("n_eff")) {
        reasons.push("N_EFF_TAMPERED".into());
    }
    if !close_diff(transform.get("p_vol"), expected.get("p_vol")) {
        reasons.push("P_VOL_TAMPERED".into());
    }
    for field in [
        "mu_annual",
        "mu_per_session",
        "sigma_daily",
        "sigma_horizon",
        "z_score",
        "p_vol",
    ] {
        if !close_diff(recorded_drift.get(field), drift.get(field)) {
            reasons.push(format!("DRIFT_PRIOR_{}_TAMPERED", field.to_uppercase()));
        }
    }
    for field in [
        "vol_window_sessions",
        "return_count",
        "return_window_start",
        "return_window_end",
    ] {
        if recorded_drift.get(field) != drift.get(field) {
            reasons.push(format!("DRIFT_PRIOR_{}_TAMPERED", field.to_uppercase()));
        }
    }
    if transform.get("matching_origins") != matching.get("origins") {
        reasons.push("MATCHING_SAMPLE_REWRITTEN".into());
    }
    if transform.get("matching_label_ends") != matching.get("label_ends") {
        reasons.push("MATCHING_LABEL_ENDS_TAMPERED".into());
    }
    if transform.get("wins") != matching.get("wins") {
        reasons.push("MATCHING_WINS_TAMPERED".into());
    }
    if transform.get("observations") != matching.get("observations") {
        reasons.push("MATCHING_OBSERVATIONS_TAMPERED".into());
    }
    let matching_label_end_max = matching
        .get("label_ends")
        .and_then(Value::as_array)
        .and_then(|rows| rows.last().cloned())
        .unwrap_or(Value::Null);
    if transform.get("matching_label_end_max") != Some(&matching_label_end_max) {
        reasons.push("MATCHING_LABEL_BOUNDARY_TAMPERED".into());
    }
    if transform
        .get("matching_label_ends")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter().any(|row| {
                row.as_str()
                    .map(|text| text > decision.as_str())
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false)
    {
        reasons.push("FUTURE_LABEL_IN_MATCHING_SAMPLE".into());
    }
    match expected.get("probability").and_then(Value::as_f64) {
        None => {
            if !row.get("probability").map(Value::is_null).unwrap_or(true) {
                reasons.push("PROBABILITY_PRESENT_WITHOUT_VALID_SAMPLE".into());
            }
        }
        Some(_expected_probability) => {
            if !close_diff(row.get("probability"), expected.get("probability")) {
                reasons.push("PROBABILITY_TAMPERED".into());
            }
            let observations = matching
                .get("observations")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            let wins = matching.get("wins").and_then(Value::as_i64).unwrap_or(0);
            let expected_raw = if observations > 0 {
                Some(wins as f64 / observations as f64)
            } else {
                None
            };
            match expected_raw {
                None => {
                    if !row
                        .get("raw_probability")
                        .map(Value::is_null)
                        .unwrap_or(true)
                    {
                        reasons.push("RAW_PROBABILITY_PRESENT_WITHOUT_MATCHES".into());
                    }
                }
                Some(raw) => {
                    if !close_diff(row.get("raw_probability"), Some(&Value::from(raw))) {
                        reasons.push("RAW_PROBABILITY_TAMPERED".into());
                    }
                }
            }
        }
    }
    let wins = int_field(transform, "wins", -1);
    let observations = int_field(transform, "observations", -1);
    if wins >= 0 && observations >= 0 && wins > observations {
        reasons.push("MATCHING_WINS_EXCEED_OBSERVATIONS".into());
    }
    let mut recomputed = Map::new();
    if let Value::Object(expected_map) = &expected {
        recomputed.insert(
            "probability".into(),
            expected_map
                .get("probability")
                .cloned()
                .unwrap_or(Value::Null),
        );
        recomputed.insert(
            "base_rate_at_origin".into(),
            expected_map
                .get("base_rate")
                .cloned()
                .unwrap_or(Value::Null),
        );
        recomputed.insert(
            "n_eff".into(),
            expected_map.get("n_eff").cloned().unwrap_or(Value::Null),
        );
        recomputed.insert(
            "p_vol".into(),
            expected_map.get("p_vol").cloned().unwrap_or(Value::Null),
        );
    }
    if let Value::Object(matching_map) = &matching {
        for (source, target) in [
            ("wins", "wins"),
            ("observations", "observations"),
            ("origins", "matching_origins"),
        ] {
            recomputed.insert(
                target.to_string(),
                matching_map.get(source).cloned().unwrap_or(Value::Null),
            );
        }
    }
    recomputed.insert(
        "transform_version".into(),
        Value::String(policy::DRIFT_SHRINKAGE_VERSION.into()),
    );
    audit_shrinkage_result(Some(Value::Object(recomputed)), reasons)
}

fn audit_shrinkage_result(recomputed: Option<Value>, reasons: Vec<String>) -> Value {
    let mut map = Map::new();
    map.insert("ok".into(), Value::Bool(reasons.is_empty()));
    map.insert(
        "reasons".into(),
        Value::Array(reasons.into_iter().map(Value::String).collect()),
    );
    map.insert("recomputed".into(), recomputed.unwrap_or(Value::Null));
    Value::Object(map)
}

fn audit_legacy_v1(
    row: &Map<String, Value>,
    dates: &[String],
    closes: &[f64],
    transform: &Map<String, Value>,
    require_full_52w: bool,
) -> Value {
    let mut reasons: Vec<String> = Vec::new();
    if transform.get("version").and_then(Value::as_str) != Some(policy::SHRINKAGE_VERSION) {
        reasons.push(format!(
            "UNKNOWN_TRANSFORM_VERSION:{}",
            sd_core::contracts::py_repr(transform.get("version").unwrap_or(&Value::Null))
        ));
    }
    if transform.get("policy_version").and_then(Value::as_str) != Some(policy::POLICY_VERSION) {
        reasons.push("TRANSFORM_POLICY_VERSION_MISMATCH".into());
    }
    if int_field(transform, "prior_strength", -1) != i64::from(policy::SHRINKAGE_PRIOR_STRENGTH) {
        reasons.push("PRIOR_STRENGTH_TAMPERED".into());
    }
    if int_field(transform, "minimum_gap_sessions", -1)
        != i64::from(policy::NONOVERLAP_GAP_SESSIONS)
    {
        reasons.push("MATCHING_GAP_TAMPERED".into());
    }
    if int_field(transform, "horizon_sessions", -1) != i64::from(policy::FORECAST_HORIZON_SESSIONS)
    {
        reasons.push("TRANSFORM_HORIZON_TAMPERED".into());
    }
    if int_field(transform, "minimum_matches", -1) != i64::from(policy::SHRINKAGE_MIN_MATCHES) {
        reasons.push("MINIMUM_MATCHES_TAMPERED".into());
    }
    let Some(decision) = string(row.get("decision_session")) else {
        reasons.push("DECISION_SESSION_NOT_IN_PRICE_SERIES".into());
        return audit_shrinkage_result(None, reasons);
    };
    let Some(index) = dates
        .iter()
        .position(|date| date == &decision)
        .map(|position| position as i64)
    else {
        reasons.push("DECISION_SESSION_NOT_IN_PRICE_SERIES".into());
        return audit_shrinkage_result(None, reasons);
    };
    if require_full_52w && row.get("full_52w_input") != Some(&Value::Bool(true)) {
        reasons.push("NOT_FULL_52W_INPUT".into());
    }
    let expected = recompute_legacy_v1(
        dates,
        closes,
        index,
        i64::from(policy::FORECAST_HORIZON_SESSIONS),
        i64::from(policy::NONOVERLAP_GAP_SESSIONS),
    );
    let matching = object(expected.get("matching"));
    if transform.get("matching_origins") != matching.get("origins") {
        reasons.push("MATCHING_SAMPLE_REWRITTEN".into());
    }
    if !close_diff(
        transform.get("base_rate_at_origin"),
        expected.get("base_rate"),
    ) {
        reasons.push("BASE_RATE_AT_ORIGIN_TAMPERED".into());
    }
    if transform.get("base_rate_label_count") != expected.get("base_rate_label_count") {
        reasons.push("BASE_RATE_SAMPLE_REWRITTEN".into());
    }
    let expected_probability = expected.get("probability");
    let probability_matches = close_diff(row.get("probability"), expected_probability);
    let both_none = expected_probability.map(Value::is_null).unwrap_or(true)
        && row.get("probability").map(Value::is_null).unwrap_or(true);
    if !probability_matches && !both_none {
        reasons.push("PROBABILITY_TAMPERED".into());
    }
    let mut recomputed = Map::new();
    recomputed.insert(
        "probability".into(),
        expected_probability.cloned().unwrap_or(Value::Null),
    );
    recomputed.insert(
        "raw_probability".into(),
        expected
            .get("raw_probability")
            .cloned()
            .unwrap_or(Value::Null),
    );
    recomputed.insert(
        "wins".into(),
        matching.get("wins").cloned().unwrap_or(Value::Null),
    );
    recomputed.insert(
        "observations".into(),
        matching.get("observations").cloned().unwrap_or(Value::Null),
    );
    recomputed.insert(
        "base_rate_at_origin".into(),
        expected.get("base_rate").cloned().unwrap_or(Value::Null),
    );
    recomputed.insert(
        "matching_origins".into(),
        matching.get("origins").cloned().unwrap_or(Value::Null),
    );
    recomputed.insert(
        "transform_version".into(),
        Value::String(policy::SHRINKAGE_VERSION.into()),
    );
    recomputed.insert("legacy".into(), Value::Bool(true));
    audit_shrinkage_result(Some(Value::Object(recomputed)), reasons)
}

/// Identity transform: probability must equal the recorded raw frequency.
pub fn audit_identity_record(row: &Value) -> Value {
    let map = object(Some(row));
    let mut reasons = record_reasons(&map, &["probability", "probability_transform"]);
    let transform = object(map.get("probability_transform"));
    if transform.get("type").and_then(Value::as_str) != Some("identity") {
        reasons.push("TRANSFORM_TYPE_MISMATCH".into());
    }
    if transform.contains_key("fitted_calibrator") {
        reasons.push("IDENTITY_TRANSFORM_CONTAINS_CALIBRATOR".into());
    }
    let mut result = Map::new();
    result.insert("ok".into(), Value::Bool(reasons.is_empty()));
    result.insert(
        "reasons".into(),
        Value::Array(reasons.into_iter().map(Value::String).collect()),
    );
    result.insert("recomputed".into(), Value::Null);
    Value::Object(result)
}

/// Audit one record according to the declared transform type.
pub fn audit_record(row: &Value, bars: &[Value], require_full_52w: bool) -> Value {
    let map = object(Some(row));
    let kind = map
        .get("probability_transform")
        .and_then(Value::as_object)
        .and_then(|transform| transform.get("type"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if kind == "identity" {
        return audit_identity_record(row);
    }
    if kind == "causal_empirical_shrinkage" || kind == "causal_drift_shrinkage" {
        return match series(bars) {
            Ok((dates, closes)) => audit_shrinkage_record(&map, &dates, &closes, require_full_52w),
            Err(reasons) => audit_shrinkage_result(None, reasons),
        };
    }
    if map.get("probability").map(Value::is_null).unwrap_or(true) {
        let mut result = Map::new();
        result.insert("ok".into(), Value::Bool(true));
        result.insert("reasons".into(), Value::Array(Vec::new()));
        result.insert("recomputed".into(), Value::Null);
        result.insert("abstained".into(), Value::Bool(true));
        return Value::Object(result);
    }
    audit_shrinkage_result(
        None,
        vec![format!(
            "UNKNOWN_TRANSFORM_TYPE:{}",
            sd_core::contracts::py_repr(
                map.get("probability_transform")
                    .and_then(Value::as_object)
                    .and_then(|transform| transform.get("type"))
                    .unwrap_or(&Value::Null)
            )
        )],
    )
}

fn valid_rows(result: &Value) -> Vec<Value> {
    result
        .get("validation")
        .and_then(|validation| validation.get("oos_forecasts"))
        .and_then(Value::as_array)
        .map(|rows| rows.iter().filter(|row| row.is_object()).cloned().collect())
        .unwrap_or_default()
}

/// Audit every walk-forward row of one researcher result.
pub fn audit_oos_result(
    result: &Value,
    bars: Option<&[Value]>,
    sessions: Option<&[NaiveDate]>,
) -> Value {
    let rows = valid_rows(result);
    let Some(bars) = bars else {
        let mut map = Map::new();
        map.insert("status".into(), Value::String("not_assessed".into()));
        map.insert("integrity_pass".into(), Value::Null);
        map.insert(
            "reasons".into(),
            Value::Array(vec![Value::String("PRICE_SERIES_UNAVAILABLE".into())]),
        );
        map.insert("record_count".into(), Value::from(rows.len()));
        map.insert("valid_count".into(), Value::from(0));
        map.insert("invalid_count".into(), Value::from(0));
        map.insert("abstained_count".into(), Value::from(0));
        map.insert("nonoverlap_valid_count".into(), Value::from(0));
        map.insert("effective_valid_samples".into(), Value::from(0));
        return Value::Object(map);
    };
    let mut audited: Vec<Value> = Vec::new();
    let mut invalid = 0i64;
    let mut abstained = 0i64;
    let mut valid_rows_found: Vec<Value> = Vec::new();
    for row in &rows {
        if row.get("probability").map(Value::is_null).unwrap_or(true) {
            abstained += 1;
            let mut entry = Map::new();
            entry.insert(
                "decision_session".into(),
                row.get("decision_session").cloned().unwrap_or(Value::Null),
            );
            entry.insert("status".into(), Value::String("abstained".into()));
            entry.insert(
                "reason_code".into(),
                row.get("reason_code").cloned().unwrap_or(Value::Null),
            );
            audited.push(Value::Object(entry));
            continue;
        }
        let outcome = audit_record(row, bars, false);
        let mut entry = Map::new();
        entry.insert(
            "decision_session".into(),
            row.get("decision_session").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "status".into(),
            Value::String(
                if outcome.get("ok") == Some(&Value::Bool(true)) {
                    "valid"
                } else {
                    "invalid"
                }
                .into(),
            ),
        );
        entry.insert(
            "reasons".into(),
            outcome
                .get("reasons")
                .cloned()
                .unwrap_or(Value::Array(Vec::new())),
        );
        entry.insert(
            "probability".into(),
            row.get("probability").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "target".into(),
            row.get("target").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "transform_version".into(),
            row.get("probability_transform")
                .and_then(|transform| transform.get("version"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        audited.push(Value::Object(entry));
        if outcome.get("ok") == Some(&Value::Bool(true)) {
            valid_rows_found.push(row.clone());
        } else {
            invalid += 1;
        }
    }
    let mut valid_dates: Vec<String> = valid_rows_found
        .iter()
        .map(|row| sd_core::contracts::py_str(row.get("decision_session").unwrap_or(&Value::Null)))
        .collect();
    valid_dates.sort();
    valid_dates.dedup();
    let mut ordered: Vec<(i64, &Value)> = valid_rows_found
        .iter()
        .filter_map(|row| {
            row.get("decision_index")
                .and_then(Value::as_i64)
                .map(|index| (index, row))
        })
        .collect();
    ordered.sort_by_key(|(index, _)| *index);
    let mut kept = 0i64;
    let mut last_end = -1i64;
    for (index, _) in &ordered {
        if *index > last_end {
            kept += 1;
            last_end = index + i64::from(policy::FORECAST_HORIZON_SESSIONS);
        }
    }
    let sampling = match sessions {
        Some(sessions) => fixed_phase_sample(&valid_dates, sessions, policy::SAMPLING_MODULUS).1,
        None => {
            let mut map = Map::new();
            map.insert(
                "method".into(),
                Value::String("fixed_exchange_session_phase".into()),
            );
            map.insert(
                "status".into(),
                Value::String("calendar_unavailable".into()),
            );
            map.insert("phase_window_independent".into(), Value::Null);
            Value::Object(map)
        }
    };
    let path_sampling = object(
        result
            .get("validation")
            .and_then(|validation| validation.get("sampling")),
    );
    let effective = valid_rows_found.len() as i64 / i64::from(policy::FORECAST_HORIZON_SESSIONS);
    let mut reasons: Vec<String> = Vec::new();
    if invalid != 0 {
        reasons.push("OOS_INTEGRITY_FAILED".into());
    }
    if valid_rows_found.is_empty() {
        reasons.push("NO_VALID_OOS_PROBABILITY".into());
    } else if effective < i64::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES) {
        reasons.push("OOS_SUPPORT_INSUFFICIENT".into());
    }
    let effectiveness = baseline_comparison(
        &valid_rows_found,
        i64::from(policy::FORECAST_HORIZON_SESSIONS),
        None,
    );
    let mut map = Map::new();
    map.insert(
        "status".into(),
        Value::String(
            if reasons.is_empty() {
                "valid"
            } else if invalid == 0 {
                "insufficient"
            } else {
                "invalid"
            }
            .into(),
        ),
    );
    map.insert("integrity_pass".into(), Value::Bool(invalid == 0));
    map.insert(
        "reasons".into(),
        Value::Array(reasons.into_iter().map(Value::String).collect()),
    );
    map.insert("record_count".into(), Value::from(rows.len()));
    map.insert("valid_count".into(), Value::from(valid_rows_found.len()));
    map.insert("invalid_count".into(), Value::from(invalid));
    map.insert("abstained_count".into(), Value::from(abstained));
    map.insert("nonoverlap_valid_count".into(), Value::from(kept));
    map.insert(
        "nonoverlap_method".into(),
        Value::String("greedy_5_session_gap_over_valid_rows".into()),
    );
    map.insert("effective_valid_samples".into(), Value::from(effective));
    map.insert(
        "effective_sample_definition".into(),
        Value::String(format!(
            "valid rows / {}",
            policy::FORECAST_HORIZON_SESSIONS
        )),
    );
    map.insert(
        "minimum_effective_samples".into(),
        Value::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES),
    );
    map.insert(
        "coverage_ratio".into(),
        path_sampling
            .get("coverage_ratio")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert("sampling".into(), sampling);
    map.insert(
        "sampling_role".into(),
        Value::String("throttle_only_not_qualification".into()),
    );
    map.insert("effectiveness".into(), effectiveness);
    map.insert("rows".into(), Value::Array(audited));
    map.insert(
        "minimum_nonoverlap".into(),
        Value::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES),
    );
    Value::Object(map)
}

/// Verify that the reported current probability is reproducible from bars.
pub fn audit_current_prediction(result: &Value, bars: Option<&[Value]>) -> Value {
    let Some(bars) = bars else {
        let mut map = Map::new();
        map.insert("pass".into(), Value::Bool(false));
        map.insert(
            "reasons".into(),
            Value::Array(vec![Value::String("PRICE_SERIES_UNAVAILABLE".into())]),
        );
        return Value::Object(map);
    };
    let probability = result.get("prob_up").cloned().unwrap_or(Value::Null);
    if probability.is_null() {
        let mut map = Map::new();
        map.insert("pass".into(), Value::Bool(true));
        map.insert("reasons".into(), Value::Array(Vec::new()));
        map.insert("abstained".into(), Value::Bool(true));
        return Value::Object(map);
    }
    let validation = object(result.get("validation"));
    let transform = validation
        .get("probability_transform")
        .cloned()
        .unwrap_or(Value::Null);
    if !transform.is_object() {
        let mut map = Map::new();
        map.insert("pass".into(), Value::Bool(false));
        map.insert(
            "reasons".into(),
            Value::Array(vec![Value::String("MISSING_PROBABILITY_TRANSFORM".into())]),
        );
        return Value::Object(map);
    }
    let (Ok((dates, closes)), _) = (series(bars), ()) else {
        let mut map = Map::new();
        map.insert("pass".into(), Value::Bool(false));
        map.insert(
            "reasons".into(),
            Value::Array(vec![Value::String("PRICE_SERIES_INVALID".into())]),
        );
        return Value::Object(map);
    };
    let mut row = Map::new();
    row.insert(
        "decision_session".into(),
        dates
            .last()
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    row.insert("decision_index".into(), Value::from(dates.len() as i64 - 1));
    row.insert("probability".into(), probability);
    row.insert(
        "raw_probability".into(),
        result
            .get("raw_probability")
            .cloned()
            .unwrap_or(Value::Null),
    );
    row.insert("probability_transform".into(), transform);
    row.insert("full_52w_input".into(), Value::Bool(true));
    let outcome = audit_shrinkage_record(&row, &dates, &closes, false);
    let mut map = Map::new();
    map.insert(
        "pass".into(),
        outcome.get("ok").cloned().unwrap_or(Value::Bool(false)),
    );
    map.insert(
        "reasons".into(),
        outcome
            .get("reasons")
            .cloned()
            .unwrap_or(Value::Array(Vec::new())),
    );
    map.insert(
        "recomputed".into(),
        outcome.get("recomputed").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "transform_independent_reimplementation".into(),
        Value::Bool(true),
    );
    Value::Object(map)
}

fn evidence_interval(paired: &[Value], length: i64) -> Value {
    let improvements: Vec<f64> = paired
        .iter()
        .filter_map(|row| row.get("improvement").and_then(Value::as_f64))
        .collect();
    bootstrap_mean_interval(
        &improvements,
        length,
        sd_core::statistics::BOOTSTRAP_REPLICATES,
        sd_core::statistics::BOOTSTRAP_SEED,
    )
}

/// Describe the in-window replay and keep `supported` for independent data only.
pub fn directional_edge_assessment(
    replay_records: &[Value],
    prospective_records: &[Value],
) -> Value {
    let matured: Vec<Value> = replay_records
        .iter()
        .filter(|row| {
            !row.get("probability").map(Value::is_null).unwrap_or(true)
                && !row.get("target").map(Value::is_null).unwrap_or(true)
                && row
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("matured")
                    == "matured"
        })
        .cloned()
        .collect();
    let paired = paired_rows(&matured);
    let lengths = block_lengths_for(
        paired.len() as i64,
        i64::from(policy::FORECAST_HORIZON_SESSIONS),
    );
    let mut intervals: Vec<Value> = lengths
        .iter()
        .map(|length| evidence_interval(&paired, *length))
        .collect();
    for (position, interval) in intervals.iter_mut().enumerate() {
        if let Value::Object(map) = interval {
            map.insert("primary".into(), Value::Bool(position == 0));
        }
    }
    let primary_interval = intervals.first().cloned().unwrap_or(Value::Null);
    let mut state = EFFECTIVENESS_VERDICT_INCONCLUSIVE;
    let mut reasons: Vec<String> = Vec::new();
    if paired.len() < policy::MIN_REPLAY_OBSERVATIONS as usize {
        reasons.push("REPLAY_SUPPORT_INSUFFICIENT".into());
    } else if primary_interval.get("status").and_then(Value::as_str) != Some("estimated") {
        reasons.push("INTERVAL_UNESTIMABLE".into());
    } else {
        let lower = primary_interval
            .get("lower")
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        let upper = primary_interval
            .get("upper")
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        if lower > 0.0 {
            state = EFFECTIVENESS_VERDICT_POSITIVE;
        } else if upper < 0.0 {
            state = EFFECTIVENESS_VERDICT_NEGATIVE;
            reasons.push("INTERVAL_BELOW_ZERO".into());
        } else {
            reasons.push("INTERVAL_CROSSES_ZERO".into());
        }
    }
    let prospective: Vec<Value> = prospective_records
        .iter()
        .filter(|row| {
            row.get("status").and_then(Value::as_str) == Some("matured")
                && !row.get("probability").map(Value::is_null).unwrap_or(true)
                && row.get("sampled") == Some(&Value::Bool(true))
                && row.get("sampling_status").and_then(Value::as_str) == Some("anchored")
        })
        .cloned()
        .collect();
    let prospective_paired = paired_rows(&prospective);
    let prospective_intervals: Vec<Value> =
        if prospective_paired.len() >= policy::MIN_PROSPECTIVE_NONOVERLAP as usize {
            block_lengths_for(
                prospective_paired.len() as i64,
                i64::from(policy::FORECAST_HORIZON_SESSIONS),
            )
            .iter()
            .enumerate()
            .map(|(position, length)| {
                let mut interval = evidence_interval(&prospective_paired, *length);
                if let Value::Object(map) = &mut interval {
                    map.insert("primary".into(), Value::Bool(position == 0));
                }
                interval
            })
            .collect()
        } else {
            Vec::new()
        };
    if prospective_paired.len() < policy::MIN_PROSPECTIVE_NONOVERLAP as usize {
        reasons.push("PROSPECTIVE_SUPPORT_INSUFFICIENT".into());
    }
    let independent_supported = prospective_intervals
        .first()
        .map(|interval| {
            interval.get("status").and_then(Value::as_str) == Some("estimated")
                && interval.get("lower").and_then(Value::as_f64).unwrap_or(0.0) > 0.0
        })
        .unwrap_or(false);
    let replay_claim = match state {
        EFFECTIVENESS_VERDICT_POSITIVE => "replay_interval_excludes_zero",
        EFFECTIVENESS_VERDICT_NEGATIVE => "replay_interval_below_zero",
        _ => "replay_interval_inconclusive",
    };
    let mut map = Map::new();
    map.insert("state".into(), Value::String(state.into()));
    map.insert("replay_state".into(), Value::String(state.into()));
    map.insert("supported".into(), Value::Bool(independent_supported));
    map.insert(
        "independent_supported".into(),
        Value::Bool(independent_supported),
    );
    map.insert(
        "evidence_scope".into(),
        Value::String("in_window_causal_prefix_replay_not_independent_holdout".into()),
    );
    map.insert(
        "replay_interval_excludes_zero".into(),
        Value::Bool(state == EFFECTIVENESS_VERDICT_POSITIVE),
    );
    map.insert("replay_claim".into(), Value::String(replay_claim.into()));
    map.insert(
        "selection_risk".into(),
        Value::String(
            "base rate, volatility and the estimator itself are estimated on the same 52-week window that the replay scores; parameters were frozen in advance, but the window is not an independent holdout"
                .into(),
        ),
    );
    map.insert(
        "reasons".into(),
        Value::Array(if reasons.is_empty() {
            vec![Value::String("DIRECTIONAL_EDGE_UNPROVEN".into())]
        } else {
            reasons.into_iter().map(Value::String).collect()
        }),
    );
    map.insert(
        "primary_evidence".into(),
        Value::String("in_window_causal_prefix_replay".into()),
    );
    map.insert("replay_observations".into(), Value::from(paired.len()));
    map.insert(
        "replay_effective_samples".into(),
        Value::from(paired.len() as i64 / i64::from(policy::FORECAST_HORIZON_SESSIONS)),
    );
    map.insert(
        "prospective_nonoverlap_count".into(),
        Value::from(prospective_paired.len()),
    );
    map.insert(
        "minimum_required".into(),
        Value::from(policy::MIN_PROSPECTIVE_NONOVERLAP),
    );
    map.insert(
        "minimum_effective_samples".into(),
        Value::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES),
    );
    map.insert(
        "minimum_replay_observations".into(),
        Value::from(policy::MIN_REPLAY_OBSERVATIONS),
    );
    map.insert("paired_records".into(), Value::from(paired.len()));
    map.insert("intervals".into(), Value::Array(intervals));
    map.insert(
        "prospective_intervals".into(),
        Value::Array(prospective_intervals),
    );
    map.insert(
        "definition".into(),
        Value::String(
            "baseline_squared_error - model_squared_error vs the causal base rate at each origin"
                .into(),
        ),
    );
    map.insert(
        "prospective_role".into(),
        Value::String(
            "the only admissible independent evidence; the replay is an in-window diagnostic"
                .into(),
        ),
    );
    map.insert("trading_signal_eligible".into(), Value::Bool(false));
    Value::Object(map)
}

/// Sorted unique researcher ids (compatibility helper).
pub fn unique_research_paths(researchers: &[Value]) -> Vec<String> {
    let mut paths: Vec<String> = researchers
        .iter()
        .filter_map(|item| {
            item.get("researcher_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    paths.sort();
    paths.dedup();
    paths
}

/// True when any declared leakage flag is explicitly false.
pub fn path_has_detected_leakage(result: &Value) -> bool {
    object(result.get("validation"))
        .get("leakage_audit")
        .and_then(Value::as_object)
        .map(|audit| audit.iter().any(|(_, value)| value == &Value::Bool(false)))
        .unwrap_or(false)
}

/// Legacy compatibility entry point returning `(quality, skill)` blocks.
pub fn build_probability_validation(
    researchers: &[Value],
    bars: Option<&[Value]>,
    skill_epsilon: f64,
) -> (Value, Value) {
    let mut audits = Map::new();
    for result in researchers {
        let path = sd_core::contracts::py_str(result.get("researcher_id").unwrap_or(&Value::Null));
        audits.insert(path, audit_oos_result(result, bars, None));
    }
    let status = if audits.is_empty() {
        "not_assessed"
    } else if audits
        .values()
        .all(|audit| audit.get("integrity_pass") == Some(&Value::Bool(true)))
    {
        "valid"
    } else {
        "invalid"
    };
    let mut quality = Map::new();
    quality.insert("status".into(), Value::String(status.into()));
    quality.insert("paths".into(), Value::Object(audits));
    quality.insert(
        "calibration_status".into(),
        Value::String("disabled_by_policy".into()),
    );
    let mut ensemble = Map::new();
    ensemble.insert("status".into(), Value::String("disabled_by_policy".into()));
    quality.insert("ensemble_calibration".into(), Value::Object(ensemble));
    quality.insert(
        "skill_metric".into(),
        Value::String("brier_skill_vs_causal_base_rate_and_constant_half".into()),
    );
    quality.insert("skill_epsilon".into(), Value::from(skill_epsilon));
    let mut skill = Map::new();
    skill.insert("status".into(), Value::String("not_applicable".into()));
    skill.insert("weighting".into(), Value::String("none".into()));
    skill.insert(
        "reason".into(),
        Value::String("single production path; shadow candidates are not weighted".into()),
    );
    (Value::Object(quality), Value::Object(skill))
}
