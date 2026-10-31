//! Research A: drift-shrunk causal base rate on the fixed 52-week / 5-session window.
//!
//! Port of `quant_research/researchers/quant.py`. Version 2 replaces the 20-session
//! momentum hit rate with a volatility-calibrated drift prior: the causal base rate is
//! shrunk toward `p_vol` with fixed `k`, which is pure variance reduction and carries no
//! predictive claim. Everything remains re-derivable from the frozen bars: the transform
//! record carries the label boundary, volatility window, `n_eff`, `k`, `mu`, `p_vol` and
//! the matching origins.
//!
//! `run()` (the snapshot-level researcher result) arrives with R06, because it embeds
//! the composite momentum block and the rank-IC diagnostic from `momentum.py`.

use chrono::NaiveDate;
use serde_json::{Map, Value};

use crate::result::researcher_result;
use sd_core::float_sum::python_sum;
use sd_core::policy;

use sd_core::market_data::forward_returns;

use crate::math_utils::{mean, wilson_interval};
use crate::momentum::roc;

/// Number of matching samples the legacy v1 estimator used (disclosure only in v2).
pub const MINIMUM_MATCHING_SAMPLES: u32 = policy::SHRINKAGE_MIN_MATCHES;
/// Legacy prior strength (disclosed so historic rows stay readable).
pub const SHRINKAGE_PRIOR_OBSERVATIONS: u32 = policy::SHRINKAGE_PRIOR_STRENGTH;
/// Disclosed ROC state window.
pub const ROC_WINDOW: usize = 20;

/// Causal up-rate and the label origins that matured by `decision`.
pub fn causal_up_rate(
    forward: &[Option<f64>],
    decision: usize,
    horizon: usize,
) -> (Option<f64>, Vec<usize>) {
    let end = decision.saturating_sub(horizon.saturating_sub(1));
    let origins: Vec<usize> = (0..end)
        .filter(|index| forward.get(*index).map(Option::is_some).unwrap_or(false))
        .collect();
    if origins.is_empty() {
        return (None, Vec::new());
    }
    let wins = origins
        .iter()
        .filter(|index| forward[**index].map(|value| value > 0.0).unwrap_or(false))
        .count();
    (Some(wins as f64 / origins.len() as f64), origins)
}

/// Volatility-calibrated drift prior using returns up to the decision close.
pub fn drift_prior(closes: &[f64], decision: usize, horizon: usize) -> Option<Value> {
    let window = policy::DRIFT_VOL_WINDOW_SESSIONS as usize;
    let start = 1.max(decision.saturating_sub(window.saturating_sub(1)));
    let mut returns: Vec<f64> = Vec::new();
    if decision >= start {
        for index in start..=decision {
            let previous = closes[index - 1];
            let current = closes[index];
            if previous > 0.0 && current > 0.0 {
                returns.push((current / previous).ln());
            }
        }
    }
    if returns.len() < policy::DRIFT_MIN_VOL_SESSIONS as usize {
        return None;
    }
    let center = python_sum(returns.iter().copied()) / returns.len() as f64;
    let variance = python_sum(
        returns
            .iter()
            .map(|value| (value - center) * (value - center)),
    ) / (returns.len() - 1) as f64;
    if variance <= 0.0 {
        return None;
    }
    let sigma = variance.sqrt();
    if !sigma.is_finite() || sigma <= 0.0 {
        return None;
    }
    let mu_session = policy::DRIFT_MU_ANNUAL / policy::SESSIONS_PER_YEAR;
    let z_score = mu_session * horizon as f64 / (sigma * (horizon as f64).sqrt());
    let probability = 0.5 * (1.0 + erf(z_score / std::f64::consts::SQRT_2));
    let mut map = Map::new();
    map.insert("mu_annual".into(), Value::from(policy::DRIFT_MU_ANNUAL));
    map.insert("mu_per_session".into(), Value::from(mu_session));
    map.insert("sigma_daily".into(), Value::from(sigma));
    map.insert(
        "sigma_horizon".into(),
        Value::from(sigma * (horizon as f64).sqrt()),
    );
    map.insert("z_score".into(), Value::from(z_score));
    map.insert("p_vol".into(), Value::from(probability));
    map.insert(
        "vol_window_sessions".into(),
        Value::from(policy::DRIFT_VOL_WINDOW_SESSIONS),
    );
    map.insert("return_count".into(), Value::from(returns.len()));
    map.insert("return_window_start".into(), Value::from(start));
    map.insert("return_window_end".into(), Value::from(decision));
    Some(Value::Object(map))
}

/// `erf` accurate to double precision; the legacy value comes from the platform libm,
/// so the last ulp is compared as an explicit tolerance in the differential tests.
fn erf(value: f64) -> f64 {
    let x = value.abs();
    if x < 2.5 {
        // Maclaurin series: erf(x) = 2/sqrt(pi) * sum (-1)^n x^(2n+1)/(n!(2n+1))
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
    // Continued fraction (Lentz) for erfc, then 1 - erfc.
    let erfc = erfc_asymptotic(x);
    if value >= 0.0 {
        1.0 - erfc
    } else {
        erfc - 1.0
    }
}

fn erfc_asymptotic(x: f64) -> f64 {
    let mut f = 1.0;
    let mut c: f64 = 1.0;
    let mut d: f64 = 0.0;
    let mut result = 0.0;
    for k in 1..200 {
        let factor = k as f64 * 0.5;
        d = x + d * factor;
        d = 1.0 / d;
        c = x + factor / c.max(f64::MIN_POSITIVE);
        let delta = c * d;
        result = delta;
        f *= delta;
        if (delta - 1.0).abs() < 1e-17 {
            break;
        }
    }
    let _ = f;
    (-x * x).exp() / (std::f64::consts::PI.sqrt() * x) * result
}

/// Shrink the causal base rate toward the volatility-calibrated drift prior.
pub fn drift_shrunk_probability(
    base_rate: Option<f64>,
    n_eff: f64,
    p_vol: Option<f64>,
) -> Option<f64> {
    let base_rate = base_rate?;
    let p_vol = p_vol?;
    if n_eff < 0.0 {
        return None;
    }
    let k = f64::from(policy::DRIFT_SHRINKAGE_K);
    Some((n_eff * base_rate + k * p_vol) / (n_eff + k))
}

/// Describe the actual abstention reason (each reason code names its own gap).
pub fn no_probability_warning(row: &Value) -> String {
    let code = row
        .get("reason_code")
        .and_then(Value::as_str)
        .unwrap_or("PROBABILITY_UNAVAILABLE");
    let matches = row
        .get("matching_observations")
        .and_then(Value::as_u64)
        .or_else(|| {
            row.get("probability_transform")
                .and_then(|transform| transform.get("observations"))
                .and_then(Value::as_u64)
        })
        .unwrap_or(0);
    match code {
        "BASE_RATE_UNAVAILABLE" => format!(
            "No reportable probability: BASE_RATE_UNAVAILABLE (no matured 5-session label exists before the decision close; {matches} same-state matching origins are disclosure only)."
        ),
        "VOLATILITY_UNAVAILABLE" => format!(
            "No reportable probability: VOLATILITY_UNAVAILABLE (fewer than {} positive finite daily returns are available for the volatility prior).",
            policy::DRIFT_MIN_VOL_SESSIONS
        ),
        other => format!("No reportable probability: {other}."),
    }
}

/// Same-sign, matured origins accepted in chronological order at least `gap` apart.
pub fn matching_indices(
    closes: &[f64],
    forward: &[Option<f64>],
    decision: usize,
    horizon: usize,
    gap: usize,
) -> Vec<usize> {
    let Some(current_signal) = roc(closes, decision, ROC_WINDOW) else {
        return Vec::new();
    };
    let current_positive = current_signal >= 0.0;
    let mut selected: Vec<usize> = Vec::new();
    let mut last: Option<usize> = None;
    let limit = decision.min(forward.len());
    for prior in ROC_WINDOW..limit {
        if prior + horizon > decision || forward.get(prior).map(Option::is_none).unwrap_or(true) {
            continue;
        }
        let Some(prior_signal) = roc(closes, prior, ROC_WINDOW) else {
            continue;
        };
        if (prior_signal >= 0.0) != current_positive {
            continue;
        }
        if last.map(|last| prior - last < gap).unwrap_or(false) {
            continue;
        }
        selected.push(prior);
        last = Some(prior);
    }
    selected
}

/// Produce the frozen-policy prediction for one decision session.
#[allow(clippy::too_many_arguments)] // mirrors the legacy keyword arguments positionally
pub fn predict_at_origin(
    dates: &[String],
    closes: &[f64],
    decision: usize,
    horizon: usize,
    window_start_exclusive: Option<&str>,
    expected_window_sessions: Option<i64>,
    coverage_verified: bool,
    gap: usize,
) -> Result<Value, String> {
    if decision >= closes.len() {
        return Err("decision index outside the price series".into());
    }
    let forward = forward_returns(closes, horizon);
    let current_signal = roc(closes, decision, ROC_WINDOW);
    let (base_rate, base_origins) = causal_up_rate(&forward, decision, horizon);
    let n_eff = if base_origins.is_empty() {
        0.0
    } else {
        base_origins.len() as f64 / horizon as f64
    };
    let drift = drift_prior(closes, decision, horizon);
    let identity = policy::model_identity("quant").map_err(|error| error.message().to_string())?;
    let identity_string = |key: &str| identity.get(key).cloned().unwrap_or(Value::Null);

    let mut feature = Map::new();
    feature.insert("name".into(), Value::String("roc20".into()));
    feature.insert("window_sessions".into(), Value::from(ROC_WINDOW));
    feature.insert("zero_state".into(), Value::String("non_negative".into()));
    feature.insert("used_for_probability".into(), Value::Bool(false));
    feature.insert(
        "role".into(),
        Value::String(policy::FEATURE_ROLE_IN_PROBABILITY.into()),
    );

    let mut transform = Map::new();
    transform.insert(
        "type".into(),
        Value::String("causal_drift_shrinkage".into()),
    );
    transform.insert(
        "version".into(),
        Value::String(policy::DRIFT_SHRINKAGE_VERSION.into()),
    );
    transform.insert("feature".into(), Value::Object(feature));
    transform.insert("horizon_sessions".into(), Value::from(horizon));
    transform.insert("minimum_gap_sessions".into(), Value::from(gap));
    transform.insert(
        "minimum_matches".into(),
        Value::from(MINIMUM_MATCHING_SAMPLES),
    );
    transform.insert(
        "matching_floor_role".into(),
        Value::String("disclosure_only_not_an_eligibility_gate".into()),
    );
    transform.insert(
        "legacy_prior_strength".into(),
        Value::from(SHRINKAGE_PRIOR_OBSERVATIONS),
    );
    transform.insert(
        "legacy_transform_version".into(),
        Value::String(policy::SHRINKAGE_VERSION.into()),
    );
    transform.insert("k".into(), Value::from(policy::DRIFT_SHRINKAGE_K));
    transform.insert(
        "probability_formula".into(),
        Value::String("p = (n_eff * base_rate + k * p_vol) / (n_eff + k)".into()),
    );
    transform.insert("drift_prior".into(), drift.clone().unwrap_or(Value::Null));
    transform.insert("n_eff".into(), Value::from(n_eff));
    transform.insert(
        "base_rate_source".into(),
        Value::String("all_matured_daily_5d_labels".into()),
    );
    transform.insert(
        "base_rate_label_count".into(),
        Value::from(base_origins.len()),
    );
    let label_end_max = base_origins
        .last()
        .and_then(|index| dates.get(index + horizon))
        .cloned()
        .map(Value::String)
        .unwrap_or(Value::Null);
    transform.insert("base_rate_label_end_max".into(), label_end_max);
    transform.insert("matching_origins".into(), Value::Array(Vec::new()));
    transform.insert("matching_label_ends".into(), Value::Array(Vec::new()));
    transform.insert("policy_version".into(), identity_string("policy_version"));
    transform.insert("model_version".into(), identity_string("model_version"));
    transform.insert("feature_version".into(), identity_string("feature_version"));
    transform.insert("target_version".into(), identity_string("target_version"));

    let mut row = Map::new();
    row.insert(
        "decision_session".into(),
        Value::String(dates[decision].clone()),
    );
    row.insert("decision_index".into(), Value::from(decision));
    row.insert("probability".into(), Value::Null);
    row.insert("raw_probability".into(), Value::Null);
    row.insert(
        "probability_transform".into(),
        Value::Object(transform.clone()),
    );
    row.insert(
        "causal_base_rate".into(),
        base_rate.map(Value::from).unwrap_or(Value::Null),
    );
    row.insert("n_eff".into(), Value::from(n_eff));
    row.insert(
        "target".into(),
        forward
            .get(decision)
            .and_then(|value| *value)
            .map(|value| Value::from(i64::from(value > 0.0)))
            .unwrap_or(Value::Null),
    );
    row.insert(
        "label_end_session".into(),
        dates
            .get(decision + horizon)
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    row.insert("status".into(), Value::String("abstained".into()));
    row.insert("reason_code".into(), Value::Null);
    row.insert("model_identity".into(), identity);

    let selected = matching_indices(closes, &forward, decision, horizon, gap);
    let matching_origins: Vec<Value> = selected
        .iter()
        .map(|index| Value::String(dates[*index].clone()))
        .collect();
    let matching_label_ends: Vec<Value> = selected
        .iter()
        .filter_map(|index| dates.get(index + horizon).cloned().map(Value::String))
        .collect();
    transform.insert(
        "matching_origins".into(),
        Value::Array(matching_origins.clone()),
    );
    transform.insert(
        "matching_label_ends".into(),
        Value::Array(matching_label_ends.clone()),
    );
    transform.insert(
        "matching_label_end_max".into(),
        matching_label_ends.last().cloned().unwrap_or(Value::Null),
    );
    transform.insert(
        "base_rate_at_origin".into(),
        base_rate.map(Value::from).unwrap_or(Value::Null),
    );
    let wins = selected
        .iter()
        .filter(|index| forward[**index].map(|value| value > 0.0).unwrap_or(false))
        .count();
    transform.insert("wins".into(), Value::from(wins));
    transform.insert("observations".into(), Value::from(selected.len()));
    row.insert("matching_observations".into(), Value::from(selected.len()));
    row.insert("matching_wins".into(), Value::from(wins));
    let p_vol = drift
        .as_ref()
        .and_then(|drift| drift.get("p_vol"))
        .cloned()
        .unwrap_or(Value::Null);
    transform.insert("p_vol".into(), p_vol.clone());

    if base_rate.is_none() {
        row.insert(
            "reason_code".into(),
            Value::String("BASE_RATE_UNAVAILABLE".into()),
        );
        row.insert("probability_transform".into(), Value::Object(transform));
        return Ok(Value::Object(row));
    }
    let Some(drift) = drift.as_ref() else {
        row.insert(
            "reason_code".into(),
            Value::String("VOLATILITY_UNAVAILABLE".into()),
        );
        row.insert(
            "volatility_returns_required".into(),
            Value::from(policy::DRIFT_MIN_VOL_SESSIONS),
        );
        row.insert("probability_transform".into(), Value::Object(transform));
        return Ok(Value::Object(row));
    };
    if current_signal.is_none() {
        row.insert("feature_state".into(), Value::String("unavailable".into()));
    }
    let p_vol_value = drift.get("p_vol").and_then(Value::as_f64);
    let Some(probability) = drift_shrunk_probability(base_rate, n_eff, p_vol_value) else {
        row.insert(
            "reason_code".into(),
            Value::String("PROBABILITY_UNAVAILABLE".into()),
        );
        row.insert("probability_transform".into(), Value::Object(transform));
        return Ok(Value::Object(row));
    };
    if selected.len() < MINIMUM_MATCHING_SAMPLES as usize {
        row.insert(
            "matching_disclosure".into(),
            Value::String(format!(
                "{} same-state matching origins; the {MINIMUM_MATCHING_SAMPLES}-match floor is disclosed context, not an eligibility gate under v2",
                selected.len()
            )),
        );
    } else {
        row.insert("matching_disclosure".into(), Value::Null);
    }
    if selected.is_empty() {
        row.insert(
            "matching_disclosure".into(),
            Value::String("no same-state matching origin inside this window".into()),
        );
    }
    let raw = if selected.is_empty() {
        None
    } else {
        Some(wins as f64 / selected.len() as f64)
    };
    let selected_returns: Vec<f64> = selected
        .iter()
        .filter_map(|index| forward[*index])
        .collect();
    row.insert("probability".into(), Value::from(probability));
    row.insert(
        "raw_probability".into(),
        raw.map(Value::from).unwrap_or(Value::Null),
    );
    row.insert("status".into(), Value::String("matured".into()));
    row.insert(
        "mean_forward_return".into(),
        if selected.is_empty() {
            Value::Null
        } else {
            mean(&selected_returns)
                .map(Value::from)
                .unwrap_or(Value::Null)
        },
    );
    let interval = if selected.is_empty() {
        Value::Null
    } else {
        wilson_interval(wins as u64, selected.len() as u64, 1.96)
            .map(|(low, high)| Value::Array(vec![Value::from(low), Value::from(high)]))
            .unwrap_or(Value::Null)
    };
    row.insert("wilson_95_interval".into(), interval);
    row.insert(
        "wilson_interval_scope".into(),
        Value::String(
            "raw_matching_hit_frequency_only; overlapping labels, not a confidence interval for the shrunk probability"
                .into(),
        ),
    );
    let available_days = match (
        window_start_exclusive,
        NaiveDate::parse_from_str(&dates[decision], "%Y-%m-%d"),
    ) {
        (Some(start), Ok(decision_date)) => NaiveDate::parse_from_str(start, "%Y-%m-%d")
            .ok()
            .map(|start_date| (decision_date - start_date).num_days()),
        _ => None,
    };
    row.insert(
        "available_history_days".into(),
        available_days.map(Value::from).unwrap_or(Value::Null),
    );
    row.insert(
        "available_history_sessions".into(),
        Value::from(decision + 1),
    );
    let full_window = coverage_verified
        && expected_window_sessions
            .map(|expected| expected > 0)
            .unwrap_or(false)
        && expected_window_sessions
            .map(|expected| (decision + 1) as i64 >= expected)
            .unwrap_or(false)
        && window_start_exclusive.is_some()
        && available_days.map(|days| days >= 364).unwrap_or(false);
    row.insert("full_52w_input".into(), Value::Bool(full_window));
    row.insert(
        "evidence_scope".into(),
        Value::String("prefix_walk_forward".into()),
    );
    row.insert(
        "target_definition".into(),
        Value::String(format!("close[t+{horizon}] / close[t] - 1 > 0")),
    );
    row.insert("probability_transform".into(), Value::Object(transform));
    Ok(Value::Object(row))
}

fn researcher_identity() -> Value {
    policy::model_identity("quant").unwrap_or(Value::Null)
}

fn benchmark_series(market: &Value) -> (Option<Map<String, Value>>, Map<String, Value>) {
    let block = market
        .get("data")
        .and_then(|data| data.get("benchmark"))
        .and_then(Value::as_object);
    let Some(block) = block else {
        let mut meta = Map::new();
        meta.insert("symbol".into(), Value::Null);
        meta.insert("selection".into(), Value::String("absent".into()));
        meta.insert("status".into(), Value::String("absent".into()));
        return (None, meta);
    };
    let mut meta = Map::new();
    meta.insert(
        "symbol".into(),
        block.get("symbol").cloned().unwrap_or(Value::Null),
    );
    meta.insert(
        "selection".into(),
        block
            .get("selection")
            .cloned()
            .unwrap_or(Value::String("declared".into())),
    );
    let Some(bars) = block.get("bars").and_then(Value::as_array) else {
        meta.insert("status".into(), Value::String("empty".into()));
        return (None, meta);
    };
    if bars.is_empty() {
        meta.insert("status".into(), Value::String("empty".into()));
        return (None, meta);
    }
    let probe = serde_json::json!({
        "data": {"bars": bars},
        "adjustment": block.get("adjustment").cloned().unwrap_or(Value::Null),
    });
    match sd_core::market_data::adjusted_close_series(&probe) {
        Ok((dates, values, provenance)) => {
            let mut series = Map::new();
            for (date, value) in dates.iter().zip(values.iter()) {
                series.insert(date.clone(), Value::from(*value));
            }
            meta.insert("status".into(), Value::String("ok".into()));
            meta.insert(
                "price_basis".into(),
                provenance
                    .get("basis_status")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            (Some(series), meta)
        }
        Err(_) => {
            meta.insert("status".into(), Value::String("invalid".into()));
            (None, meta)
        }
    }
}

/// Snapshot-level production result (`quant.run`).
pub fn run(manifest: &Value, market: &Value) -> Result<Value, String> {
    let ticker = sd_core::contracts::py_str(manifest.get("ticker").unwrap_or(&Value::Null));
    let horizon_name = sd_core::contracts::py_str(manifest.get("horizon").unwrap_or(&Value::Null));
    let snapshot_id =
        sd_core::contracts::py_str(manifest.get("snapshot_id").unwrap_or(&Value::Null));
    let horizon = policy::FORECAST_HORIZON_SESSIONS as usize;
    let bars = sd_core::market_data::market_bars(market, false)
        .map_err(|error| error.message().to_string())?;
    let (dates, closes, price_provenance) = sd_core::market_data::prediction_price_series(market)
        .map_err(|error| error.message().to_string())?;
    let (momentum_dates, momentum_closes, momentum_provenance) =
        sd_core::market_data::adjusted_close_series(market)
            .map_err(|error| error.message().to_string())?;
    let (benchmark_closes, benchmark_meta) = benchmark_series(market);
    let benchmark_value = benchmark_closes
        .as_ref()
        .map(|series| Value::Object(series.clone()))
        .unwrap_or(Value::Null);
    let policy_window = market
        .get("data")
        .and_then(|data| data.get("policy_window"))
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let window_start = policy_window
        .get("window_start_exclusive")
        .cloned()
        .unwrap_or(Value::Null);
    let expected_window_sessions = policy_window
        .get("expected_sessions")
        .and_then(Value::as_i64);
    let coverage_verified = policy_window.get("coverage_status").and_then(Value::as_str)
        == Some("complete")
        && truthy(policy_window.get("coverage_recomputed"))
        && truthy(policy_window.get("calendar_verified"));
    let sessions = sd_core::market_data::calendar_session_list(market);
    let current = bars.len().saturating_sub(1);
    let current_signal = roc(&closes, current, ROC_WINDOW);
    if current_signal.is_none() {
        return researcher_result(
            "quant",
            &ticker,
            &horizon_name,
            &snapshot_id,
            "insufficient_data",
            "forecast",
            None,
            None,
            serde_json::json!({"positive": [], "negative": []}),
            Value::Object(Map::new()),
            Vec::new(),
            vec![Value::String(
                "At least 21 daily closes are required for the predeclared 20-session momentum hypothesis.".into(),
            )],
            None,
            None,
        );
    }
    let current_row = predict_at_origin(
        &dates,
        &closes,
        current,
        horizon,
        window_start.as_str(),
        expected_window_sessions,
        coverage_verified,
        6,
    )?;
    let probability = current_row["probability"].as_f64();
    let transform = current_row["probability_transform"].clone();
    let forward = sd_core::market_data::forward_returns(&closes, horizon);

    let mut oos_forecasts: Vec<Value> = Vec::new();
    let mut predicted_decisions: Vec<i64> = Vec::new();
    let ledger_start = ROC_WINDOW + horizon;
    for decision in ledger_start..current {
        if decision + horizon > current {
            break;
        }
        if forward.get(decision).map(Option::is_none).unwrap_or(true) {
            continue;
        }
        let mut row = predict_at_origin(
            &dates,
            &closes,
            decision,
            horizon,
            window_start.as_str(),
            expected_window_sessions,
            coverage_verified,
            6,
        )?;
        if let Value::Object(map) = &mut row {
            map.insert(
                "label_available_at".into(),
                dates
                    .get(decision + horizon)
                    .cloned()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
        }
        if !row["probability"].is_null() {
            predicted_decisions.push(decision as i64);
        }
        oos_forecasts.push(row);
    }
    let matured_oos: Vec<Value> = oos_forecasts
        .iter()
        .filter(|row| !row["probability"].is_null())
        .cloned()
        .collect();
    let nonsampled = match sessions.as_ref() {
        Some(sessions) => sd_core::market_data::evaluation_sample(&oos_forecasts, Some(sessions)),
        None => sd_core::market_data::evaluation_sample(&oos_forecasts, None),
    };
    let sample_rows = nonsampled
        .get("sampled_predictions")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let effectiveness =
        sd_core::statistics::baseline_comparison(&matured_oos, i64::from(horizon as u32), None);

    let mut warnings: Vec<Value> = vec![
        Value::String(
            "Returns are gross close-to-close observations; this baseline does not model execution, spreads, slippage or fees.".into(),
        ),
        Value::String(
            "The replay ledger records daily predictions whose 5-session labels overlap; the effective sample size is the row count divided by 5, and records remain correlated.".into(),
        ),
        Value::String("ROC20 is retained as a disclosed state variable only; it no longer enters the probability.".into()),
        Value::String(
            "The 5D target excludes cash dividends; a window spanning an ex-dividend date can flip a near-flat label downward.".into(),
        ),
    ];
    if probability.is_none() {
        warnings.push(Value::String(no_probability_warning(&current_row)));
    }
    if matured_oos.is_empty() {
        warnings.push(Value::String(
            "No matured walk-forward prediction was available on this prefix.".into(),
        ));
    }
    if current_row.get("full_52w_input") == Some(&Value::Bool(false)) {
        warnings.push(Value::String(
            "Current decision does not yet have a full 52-week input window.".into(),
        ));
    }
    let mut evidence = Map::new();
    evidence.insert("positive".into(), Value::Array(Vec::new()));
    evidence.insert("negative".into(), Value::Array(Vec::new()));
    if let Some(probability) = probability {
        let target = if probability >= 0.5 {
            "positive"
        } else {
            "negative"
        };
        let mut item = Map::new();
        item.insert(
            "family".into(),
            Value::String("base_rate_variance_reduction".into()),
        );
        item.insert(
            "hypothesis".into(),
            Value::String(
                "the causal 5D up-rate is a better 5-session estimator than a momentum-conditioned hit rate; it is shrunk toward a volatility-calibrated drift prior".into(),
            ),
        );
        item.insert(
            "signal_value".into(),
            current_signal.map(Value::from).unwrap_or(Value::Null),
        );
        item.insert(
            "empirical_hit_rate".into(),
            current_row["raw_probability"].clone(),
        );
        item.insert("shrunk_probability".into(), Value::from(probability));
        item.insert(
            "probability_basis".into(),
            Value::String("drift_shrunk_causal_base_rate".into()),
        );
        item.insert(
            "probability_transform_version".into(),
            Value::String(policy::DRIFT_SHRINKAGE_VERSION.into()),
        );
        item.insert(
            "causal_base_rate".into(),
            current_row["causal_base_rate"].clone(),
        );
        item.insert(
            "drift_prior_probability".into(),
            transform.get("p_vol").cloned().unwrap_or(Value::Null),
        );
        item.insert("shrinkage_k".into(), Value::from(policy::DRIFT_SHRINKAGE_K));
        item.insert(
            "matching_observations".into(),
            current_row["matching_observations"].clone(),
        );
        item.insert("matching_wins".into(), current_row["matching_wins"].clone());
        item.insert("n_raw".into(), current_row["matching_observations"].clone());
        item.insert("n_effective".into(), current_row["n_eff"].clone());
        item.insert(
            "wilson_95_interval".into(),
            current_row["wilson_95_interval"].clone(),
        );
        item.insert(
            "wilson_interval_method".into(),
            Value::String(
                "Wilson score on the raw matching hit frequency; nominal IID approximation with overlapping labels; not a confidence interval for the shrunk probability".into(),
            ),
        );
        item.insert("wilson_coverage_validated".into(), Value::Bool(false));
        item.insert(
            "mean_forward_return".into(),
            current_row["mean_forward_return"].clone(),
        );
        let base_rate = current_row["causal_base_rate"].as_f64().unwrap_or(0.0);
        let p_vol = transform
            .get("p_vol")
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        let n_eff = current_row["n_eff"].as_f64().unwrap_or(0.0);
        let base_count = transform
            .get("base_rate_label_count")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        item.insert(
            "rationale".into(),
            Value::String(format!(
                "The causal base rate {base_rate:.4} is measured on {base_count} matured 5D labels and shrunk toward p_vol={p_vol:.4} with k={}; n_eff={n_eff:.1}. The ROC20 state is disclosed for context only.",
                policy::DRIFT_SHRINKAGE_K
            )),
        );
        if let Some(Value::Array(rows)) = evidence.get_mut(target) {
            rows.push(Value::Object(item));
            rows.sort_by(|left, right| {
                left.get("family")
                    .and_then(Value::as_str)
                    .cmp(&right.get("family").and_then(Value::as_str))
            });
        }
    }
    if manifest
        .get("data_quality")
        .and_then(|quality| quality.get("overall"))
        .and_then(Value::as_str)
        == Some("blocked")
    {
        warnings.push(Value::String(
            "Snapshot data validation is blocked; this result is retained only for diagnosis."
                .into(),
        ));
    }
    let momentum_block = crate::momentum::compute(
        &momentum_closes,
        &momentum_dates,
        if benchmark_value.is_null() {
            None
        } else {
            Some(&benchmark_value)
        },
        &ticker,
        benchmark_meta.get("symbol").and_then(Value::as_str),
        benchmark_meta
            .get("selection")
            .and_then(Value::as_str)
            .unwrap_or("absent"),
        momentum_provenance
            .get("price_field")
            .and_then(Value::as_str),
        None,
    )?;
    let mut momentum_block = momentum_block;
    if let Value::Object(map) = &mut momentum_block {
        map.insert("price_basis".into(), momentum_provenance.clone());
        if let Some(Value::Object(benchmark)) = map.get_mut("benchmark") {
            benchmark.insert(
                "status".into(),
                benchmark_meta.get("status").cloned().unwrap_or(Value::Null),
            );
        }
        map.insert(
            "diagnostic_rank_ic".into(),
            crate::momentum::rank_ic_diagnostic(
                &momentum_closes,
                &momentum_dates,
                if benchmark_value.is_null() {
                    None
                } else {
                    Some(&benchmark_value)
                },
                horizon,
                None,
                20,
            ),
        );
    }
    let identity = researcher_identity();
    let eligible_probability = probability.is_some();
    let validation = {
        let mut validation = Map::new();
        validation.insert(
            "method".into(),
            Value::String(
                "drift-shrunk causal base rate on a prefix walk-forward ledger; fixed 52-week window; no fitted calibrator".into(),
            ),
        );
        validation.insert("model_identity".into(), identity.clone());
        validation.insert(
            "ledger_scope".into(),
            Value::String("prefix_walk_forward".into()),
        );
        validation.insert(
            "minimum_matching_samples".into(),
            Value::from(MINIMUM_MATCHING_SAMPLES),
        );
        validation.insert(
            "matching_floor_role".into(),
            Value::String("disclosure_only_not_an_eligibility_gate".into()),
        );
        validation.insert(
            "oos_scope".into(),
            Value::String(
                "prefix walk-forward within the frozen 52-week snapshot; daily overlapping labels; 6-session sampled subset reported separately".into(),
            ),
        );
        validation.insert("oos_predictions".into(), Value::from(matured_oos.len()));
        validation.insert("prediction_days".into(), Value::from(matured_oos.len()));
        validation.insert(
            "oos_evaluable_samples".into(),
            Value::from(oos_forecasts.len()),
        );
        validation.insert(
            "abstained_predictions".into(),
            Value::from(oos_forecasts.len() - matured_oos.len()),
        );
        let nonoverlap =
            sd_core::market_data::non_overlapping(&predicted_decisions, horizon as i64).len();
        validation.insert(
            "non_overlapping_prediction_count".into(),
            Value::from(nonoverlap),
        );
        validation.insert("sampled_predictions".into(), Value::from(sample_rows));
        validation.insert("sampling".into(), nonsampled_sampling(&nonsampled));
        validation.insert(
            "oos_coverage".into(),
            Value::from(if oos_forecasts.is_empty() {
                0.0
            } else {
                matured_oos.len() as f64 / oos_forecasts.len() as f64
            }),
        );
        validation.insert(
            "oos_abstain_rate".into(),
            if oos_forecasts.is_empty() {
                Value::Null
            } else {
                Value::from(1.0 - matured_oos.len() as f64 / oos_forecasts.len() as f64)
            },
        );
        validation.insert("oos_forecasts".into(), Value::Array(oos_forecasts.clone()));
        validation.insert(
            "brier".into(),
            effectiveness
                .get("brier_model")
                .cloned()
                .unwrap_or(Value::Null),
        );
        validation.insert("effectiveness".into(), effectiveness.clone());
        validation.insert(
            "non_overlapping_training_labels".into(),
            current_row["matching_observations"].clone(),
        );
        validation.insert(
            "n_raw".into(),
            if eligible_probability {
                current_row["matching_observations"].clone()
            } else {
                Value::Null
            },
        );
        validation.insert(
            "n_effective".into(),
            if eligible_probability {
                current_row["n_eff"].clone()
            } else {
                Value::Null
            },
        );
        validation.insert(
            "base_rate_effective_samples".into(),
            if eligible_probability {
                current_row["n_eff"].clone()
            } else {
                Value::Null
            },
        );
        validation.insert(
            "prior_weight_share".into(),
            if eligible_probability {
                let n_eff = current_row["n_eff"].as_f64().unwrap_or(0.0);
                Value::from(
                    f64::from(policy::DRIFT_SHRINKAGE_K)
                        / (n_eff + f64::from(policy::DRIFT_SHRINKAGE_K)),
                )
            } else {
                Value::Null
            },
        );
        let mut power = Map::new();
        power.insert(
            "prospective_floor".into(),
            Value::from(policy::MIN_PROSPECTIVE_NONOVERLAP),
        );
        power.insert(
            "curve".into(),
            Value::Array(sd_core::statistics::power_curve(
                &[
                    i64::from(policy::MIN_PROSPECTIVE_NONOVERLAP),
                    60,
                    90,
                    120,
                    250,
                ],
                &[0.55, 0.60, 0.63],
                0.5,
                0.05,
            )),
        );
        power.insert(
            "interpretation".into(),
            Value::String("pass probability of a binomial one-sided 5% test at a 50% null".into()),
        );
        validation.insert("evidence_power".into(), Value::Object(power));
        validation.insert("n_oos".into(), Value::from(matured_oos.len()));
        let history_span = current as i64
            - current_row
                .get("probability_transform")
                .and_then(|transform| transform.get("matching_origins"))
                .and_then(Value::as_array)
                .and_then(|rows| {
                    rows.iter()
                        .filter_map(|row| row.as_str())
                        .filter_map(|date| dates.iter().position(|candidate| candidate == date))
                        .map(|position| position as i64)
                        .min()
                })
                .unwrap_or(current as i64);
        validation.insert("history_span_sessions".into(), Value::from(history_span));
        validation.insert(
            "prediction_decision_session".into(),
            dates
                .get(current)
                .cloned()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        validation.insert("probability_transform".into(), transform.clone());
        let mut prior_policy = Map::new();
        prior_policy.insert(
            "method".into(),
            Value::String(
                "shrinkage of the causal base rate toward a volatility-calibrated drift prior"
                    .into(),
            ),
        );
        prior_policy.insert("k".into(), Value::from(policy::DRIFT_SHRINKAGE_K));
        prior_policy.insert("mu_annual".into(), Value::from(policy::DRIFT_MU_ANNUAL));
        prior_policy.insert(
            "vol_window_sessions".into(),
            Value::from(policy::DRIFT_VOL_WINDOW_SESSIONS),
        );
        prior_policy.insert("time_decay".into(), Value::String("none".into()));
        prior_policy.insert("tuned_on_this_window".into(), Value::Bool(false));
        validation.insert("prior_policy".into(), Value::Object(prior_policy));
        validation.insert(
            "target_definition".into(),
            Value::String(format!(
                "close[t+{horizon}] / close[t] - 1 > 0 on split-adjusted closes"
            )),
        );
        validation.insert(
            "target_version".into(),
            identity
                .get("target_version")
                .cloned()
                .unwrap_or(Value::Null),
        );
        validation.insert("label_basis".into(), policy::dividend_label_disclosure());
        validation.insert("price_basis".into(), price_provenance.clone());
        let mut leakage = Map::new();
        leakage.insert(
            "features_use_data_through_decision_close_only".into(),
            Value::Bool(true),
        );
        leakage.insert(
            "future_returns_used_only_as_labels".into(),
            Value::Bool(true),
        );
        leakage.insert(
            "training_labels_matured_by_decision".into(),
            Value::Bool(true),
        );
        leakage.insert(
            "base_rate_labels_matured_by_decision".into(),
            Value::Bool(true),
        );
        leakage.insert(
            "matching_gap_sessions".into(),
            Value::from(policy::NONOVERLAP_GAP_SESSIONS),
        );
        leakage.insert(
            "oos_labels_overlap_disclosure".into(),
            Value::String(format!(
                "daily prefix ledger; adjacent {horizon}-session labels overlap"
            )),
        );
        validation.insert("leakage_audit".into(), Value::Object(leakage));
        validation.insert("momentum_20d".into(), momentum_block);
        Value::Object(validation)
    };
    researcher_result(
        "quant",
        &ticker,
        &horizon_name,
        &snapshot_id,
        if probability.is_some() {
            "success"
        } else {
            "insufficient_data"
        },
        "forecast",
        probability,
        if probability.is_some() {
            current_row
                .get("mean_forward_return")
                .and_then(Value::as_f64)
        } else {
            None
        },
        Value::Object(evidence),
        validation,
        vec![Value::String(
            "daily split-adjusted close (provider close; dividends not reinvested)".into(),
        )],
        warnings,
        if probability.is_some() {
            Some("drift_shrunk_causal_base_rate")
        } else {
            None
        },
        if probability.is_some() {
            current_row.get("raw_probability").and_then(Value::as_f64)
        } else {
            None
        },
    )
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Null) | None => false,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn nonsampled_sampling(block: &Value) -> Value {
    let mut sampling = block
        .get("sampling")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    for key in [
        "total_predictions",
        "matured_predictions",
        "nonoverlap_predictions",
        "coverage_ratio",
    ] {
        sampling.insert(
            key.to_string(),
            block.get(key).cloned().unwrap_or(Value::Null),
        );
    }
    Value::Object(sampling)
}
