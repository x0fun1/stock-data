//! Run-context disclosures: sample capacity and decision timing (`feasibility.py`).
//!
//! Metadata only; these blocks never move a probability. Capacity is an upper bound on
//! the labels a path can use, so a path marked `feasible` can still abstain after its own
//! filters. Reporting the ceiling up front stops a run from being read as "no signal"
//! when the window simply cannot host the test.

use serde_json::{Map, Value};

/// Minimum matured matches for the quant path.
pub const QUANT_MIN_MATCHES: i64 = 12;
/// Minimum matured non-overlapping labels for the factor path.
pub const FACTOR_MIN_MATURED: i64 = 18;
/// Minimum selection-segment labels for the factor path.
pub const FACTOR_MIN_SELECTION_LABELS: i64 = 10;
/// Minimum training rows for the ML path.
pub const ML_MIN_TRAIN: i64 = 60;
/// Minimum calibration observations for the ML path.
pub const ML_CALIBRATION_MIN: i64 = 40;
/// Warm-up sessions before the quant feature is available.
pub const QUANT_WARMUP_SESSIONS: i64 = 20;
/// Warm-up sessions before the factor feature is available.
pub const FACTOR_WARMUP_SESSIONS: i64 = 60;
/// Chronological fraction reserved for selection.
pub const FACTOR_SELECTION_FRACTION: f64 = 0.5;

fn horizon_sessions(manifest: &Value) -> i64 {
    let text = manifest
        .get("horizon")
        .map(sd_core::contracts::py_str)
        .unwrap_or_else(|| "5D".to_string());
    let digits: String = text
        .chars()
        .filter(|character| character.is_ascii_digit())
        .collect();
    let horizon = digits.parse::<i64>().unwrap_or(5);
    horizon.max(1)
}

fn spaced_capacity(bar_count: i64, horizon: i64, warmup: i64) -> i64 {
    let last = bar_count - 1 - horizon;
    if last < warmup {
        return 0;
    }
    (last - warmup) / (horizon + 1) + 1
}

fn selection_capacity(bar_count: i64, horizon: i64) -> i64 {
    let last = (bar_count as f64 * FACTOR_SELECTION_FRACTION) as i64 - horizon - 1;
    if last < FACTOR_WARMUP_SESSIONS {
        return 0;
    }
    (last - FACTOR_WARMUP_SESSIONS) / (horizon + 1) + 1
}

fn daily_matured(bar_count: i64, horizon: i64, warmup: i64) -> i64 {
    let last = bar_count - 1 - horizon;
    0.max(last - warmup + 1)
}

fn status(capacity: i64, required: i64) -> &'static str {
    if capacity >= required {
        "feasible"
    } else {
        "capacity_insufficient"
    }
}

fn observed(results: Option<&Map<String, Value>>, researcher_id: &str) -> Option<i64> {
    let results = results?;
    let validation = results
        .get(researcher_id)
        .and_then(|result| result.get("validation"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let value = match researcher_id {
        "quant" => validation
            .get("non_overlapping_training_labels")
            .and_then(Value::as_i64),
        "factor" => {
            let counts = validation
                .get("calibration_bucket_counts")
                .and_then(Value::as_object);
            counts.map(|counts| counts.values().filter_map(Value::as_i64).sum::<i64>())
        }
        "ml" => validation
            .get("production_replay")
            .and_then(|replay| replay.get("observations"))
            .and_then(Value::as_i64)
            .or_else(|| {
                validation
                    .get("non_overlapping_prediction_count")
                    .and_then(Value::as_i64)
            }),
        _ => None,
    };
    value
}

/// Per-path label ceiling versus requirement for the frozen window.
pub fn build_forecast_feasibility(
    manifest: &Value,
    bars: &[Value],
    results: Option<&Map<String, Value>>,
) -> Value {
    let horizon = horizon_sessions(manifest);
    let sessions = bars.len() as i64;
    let quant_capacity = spaced_capacity(sessions, horizon, QUANT_WARMUP_SESSIONS);
    let factor_capacity = spaced_capacity(sessions, horizon, FACTOR_WARMUP_SESSIONS);
    let selection = selection_capacity(sessions, horizon);
    let ml_training_capacity = daily_matured(sessions, horizon, FACTOR_WARMUP_SESSIONS);
    let ml_replay_capacity =
        0.max((sessions - FACTOR_WARMUP_SESSIONS - horizon - ML_MIN_TRAIN) / (horizon + 1));

    let mut quant = Map::new();
    quant.insert(
        "required_matured_non_overlapping".into(),
        Value::from(QUANT_MIN_MATCHES),
    );
    quant.insert(
        "capacity_matured_non_overlapping".into(),
        Value::from(quant_capacity),
    );
    quant.insert(
        "status".into(),
        Value::String(status(quant_capacity, QUANT_MIN_MATCHES).into()),
    );
    quant.insert(
        "basis".into(),
        Value::String(
            "upper bound: the same-sign filter and matured-label ordering can only reduce it"
                .into(),
        ),
    );

    let mut factor = Map::new();
    factor.insert(
        "required_matured_non_overlapping".into(),
        Value::from(FACTOR_MIN_MATURED),
    );
    factor.insert(
        "required_selection_non_overlapping".into(),
        Value::from(FACTOR_MIN_SELECTION_LABELS),
    );
    factor.insert(
        "capacity_matured_non_overlapping".into(),
        Value::from(factor_capacity),
    );
    factor.insert(
        "capacity_selection_non_overlapping".into(),
        Value::from(selection),
    );
    factor.insert(
        "status".into(),
        Value::String(
            if factor_capacity >= FACTOR_MIN_MATURED && selection >= FACTOR_MIN_SELECTION_LABELS {
                "feasible"
            } else {
                "capacity_insufficient"
            }
            .into(),
        ),
    );
    factor.insert(
        "basis".into(),
        Value::String("three buckets of six matured labels plus ten selection labels".into()),
    );

    let mut ml = Map::new();
    ml.insert("required_training_rows".into(), Value::from(ML_MIN_TRAIN));
    ml.insert(
        "capacity_training_rows".into(),
        Value::from(ml_training_capacity),
    );
    ml.insert("required_non_overlapping_replay".into(), Value::from(1));
    ml.insert(
        "capacity_non_overlapping_replay".into(),
        Value::from(ml_replay_capacity),
    );
    ml.insert(
        "required_calibration_observations".into(),
        Value::from(ML_CALIBRATION_MIN),
    );
    ml.insert(
        "capacity_calibration_observations".into(),
        Value::from(ml_replay_capacity),
    );
    ml.insert(
        "calibration_status".into(),
        Value::String(status(ml_replay_capacity, ML_CALIBRATION_MIN).into()),
    );
    ml.insert(
        "status".into(),
        Value::String(status(ml_training_capacity, ML_MIN_TRAIN).into()),
    );
    ml.insert(
        "basis".into(),
        Value::String(
            "60 matured training rows are required; the replay scores non-overlapping H+1 decisions. The 52-week window usually supplies fewer than the 40 matured replay rows Platt needs, so the published probability is raw and calibration is disclosed as capacity-limited."
                .into(),
        ),
    );

    let mut paths = Map::new();
    paths.insert("quant".into(), Value::Object(quant));
    paths.insert("factor".into(), Value::Object(factor));
    paths.insert("ml".into(), Value::Object(ml));
    for name in ["quant", "factor", "ml"] {
        if let Some(value) = observed(results, name) {
            if let Some(Value::Object(path)) = paths.get_mut(name) {
                path.insert("observed".into(), Value::from(value));
            }
        }
    }
    let mut result = Map::new();
    result.insert("sessions".into(), Value::from(sessions));
    result.insert("horizon_sessions".into(), Value::from(horizon));
    result.insert("paths".into(), Value::Object(paths));
    result.insert(
        "note".into(),
        Value::String(
            "capacity_insufficient means the 52-week window cannot host the path's declared minimum; the path abstains instead of lowering its threshold."
                .into(),
        ),
    );
    Value::Object(result)
}

/// Decision timestamp and label basis for the reported probability.
pub fn build_timing(manifest: &Value, bars: &[Value]) -> Value {
    let validation = manifest
        .get("data_validation")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let latest = validation
        .get("latest_confirmed_close")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let last_session = bars
        .last()
        .and_then(|bar| bar.get("date"))
        .cloned()
        .unwrap_or(Value::Null);
    let decision_session = latest
        .get("session_date")
        .cloned()
        .filter(|value| !value.is_null())
        .unwrap_or_else(|| last_session.clone());
    let mut result = Map::new();
    result.insert(
        "asof_timestamp".into(),
        manifest
            .get("asof_timestamp")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert("last_bar_session".into(), last_session);
    result.insert("decision_session".into(), decision_session);
    result.insert(
        "decision_at".into(),
        latest.get("close_at").cloned().unwrap_or(Value::Null),
    );
    result.insert("label_basis".into(), Value::String("close_to_close".into()));
    result.insert("entry_basis".into(), Value::String("not_modelled".into()));
    result.insert("first_executable_at".into(), Value::Null);
    result.insert(
        "first_executable_rule".into(),
        Value::String(
            "next session open after the confirmed close; not resolved in this run".into(),
        ),
    );
    result.insert(
        "note".into(),
        Value::String(
            "The probability is a close-to-close research estimate, not an executable trade signal.".into(),
        ),
    );
    Value::Object(result)
}
