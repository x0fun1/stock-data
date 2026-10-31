//! Research D: 5-session descriptive factor diagnostics (`factor_backtest.py`).
//!
//! Never a probability. Fixed factor definitions, a 70/30 chronological split, training
//! quantile cutoffs fixed before the holdout is scored, and non-overlapping labels on both
//! sides. The single-series design means this is a time-series IC diagnostic, not a
//! cross-sectional Rank IC or a portfolio backtest.

use serde_json::{Map, Value};

use crate::math_utils::{mean, pearson, quantile, sample_std, spearman};
use crate::result::researcher_result;
use sd_core::float_sum::python_sum;
use sd_core::market_data::{forward_returns, market_bars, non_overlapping};
use sd_core::policy;

/// Minimum holdout observations before an IC counts as `available`.
pub const MIN_HOLDOUT_OBSERVATIONS: usize = 20;
/// Frozen model version.
pub const MODEL_VERSION: &str = "factor_backtest_5d_descriptive_v1";

fn float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

/// Factor name -> (family, per-session values with `None` before warm-up).
fn factor_values(bars: &[Value]) -> Vec<(&'static str, &'static str, Vec<Option<f64>>)> {
    let count = bars.len();
    let column = |key: &str| -> Vec<f64> {
        bars.iter()
            .map(|bar| float(bar.get(key)).unwrap_or(0.0))
            .collect()
    };
    let close = column("close");
    let open = column("open");
    let high = column("high");
    let low = column("low");
    let volume = column("volume");
    let mut result: Vec<(&'static str, &'static str, Vec<Option<f64>>)> = Vec::new();
    for window in [5usize, 20, 60] {
        let mut values: Vec<Option<f64>> = vec![None; count];
        for index in window..count {
            if close[index - window] > 0.0 {
                values[index] = Some(close[index] / close[index - window] - 1.0);
            }
        }
        let name: &'static str = match window {
            5 => "momentum_5",
            20 => "momentum_20",
            _ => "momentum_60",
        };
        result.push((name, "momentum", values));
    }
    let mut one_day: Vec<Option<f64>> = vec![None; count];
    let mut gap: Vec<Option<f64>> = vec![None; count];
    let mut close_location: Vec<Option<f64>> = vec![None; count];
    let mut trend: Vec<Option<f64>> = vec![None; count];
    let mut range_position: Vec<Option<f64>> = vec![None; count];
    let mut volume_z: Vec<Option<f64>> = vec![None; count];
    for index in 0..count {
        if index >= 1 && close[index - 1] > 0.0 {
            one_day[index] = Some(close[index] / close[index - 1] - 1.0);
            gap[index] = Some(open[index] / close[index - 1] - 1.0);
        }
        let day_range = high[index] - low[index];
        if day_range > 0.0 {
            close_location[index] =
                Some((2.0 * close[index] - high[index] - low[index]) / day_range);
        }
        if index >= 19 {
            let window = 20;
            let close_window = &close[index + 1 - window..=index];
            let center = python_sum(close_window.iter().copied()) / close_window.len() as f64;
            trend[index] = if center > 0.0 {
                Some(close[index] / center - 1.0)
            } else {
                None
            };
            let high20 = high[index + 1 - window..=index]
                .iter()
                .copied()
                .fold(f64::NEG_INFINITY, f64::max);
            let low20 = low[index + 1 - window..=index]
                .iter()
                .copied()
                .fold(f64::INFINITY, f64::min);
            range_position[index] = if high20 > low20 {
                Some((close[index] - low20) / (high20 - low20))
            } else {
                Some(0.5)
            };
            let volume_window = &volume[index + 1 - window..=index];
            let window_mean =
                python_sum(volume_window.iter().copied()) / volume_window.len() as f64;
            let scale = sample_std(volume_window);
            volume_z[index] = Some(match scale {
                Some(scale) if scale != 0.0 => (volume[index] - window_mean) / scale,
                _ => 0.0,
            });
        }
    }
    result.push(("momentum_1", "momentum", one_day));
    result.push(("trend_sma20", "trend", trend));
    result.push(("range_position_20", "trend", range_position));
    result.push(("volume_z20", "volume", volume_z));
    result.push(("overnight_gap", "volatility", gap));
    result.push(("close_location", "volume", close_location));
    result
}

fn aligned(
    values: &[Option<f64>],
    labels: &[Option<f64>],
    indices: &[i64],
) -> (Vec<f64>, Vec<f64>) {
    let mut x = Vec::new();
    let mut y = Vec::new();
    for index in indices {
        if *index < 0 {
            continue;
        }
        let position = *index as usize;
        if position >= values.len() || position >= labels.len() {
            continue;
        }
        let (Some(value), Some(label)) = (values[position], labels[position]) else {
            continue;
        };
        if !value.is_finite() || !label.is_finite() {
            continue;
        }
        x.push(value);
        y.push(label);
    }
    (x, y)
}

fn mean_of(values: &[f64]) -> Option<f64> {
    mean(values)
}

fn metrics(
    values: &[Option<f64>],
    labels: &[Option<f64>],
    indices: &[i64],
    low_cut: Option<f64>,
    high_cut: Option<f64>,
) -> Value {
    let (x, y) = aligned(values, labels, indices);
    let mut low_returns: Vec<f64> = Vec::new();
    let mut high_returns: Vec<f64> = Vec::new();
    for (value, outcome) in x.iter().zip(y.iter()) {
        if low_cut.map(|cut| *value <= cut).unwrap_or(false) {
            low_returns.push(*outcome);
        }
        if high_cut.map(|cut| *value >= cut).unwrap_or(false) {
            high_returns.push(*outcome);
        }
    }
    let spread = match (mean_of(&high_returns), mean_of(&low_returns)) {
        (Some(high), Some(low)) if !high_returns.is_empty() && !low_returns.is_empty() => {
            Some(high - low)
        }
        _ => None,
    };
    let enough = x.len() >= MIN_HOLDOUT_OBSERVATIONS;
    let status = if enough {
        "available"
    } else if x.len() >= 3 {
        "descriptive_only"
    } else {
        "insufficient_data"
    };
    let mut result = Map::new();
    result.insert("status".into(), Value::String(status.into()));
    result.insert("descriptive_only".into(), Value::Bool(!enough));
    result.insert("observations".into(), Value::from(x.len()));
    result.insert(
        "minimum_observations".into(),
        Value::from(MIN_HOLDOUT_OBSERVATIONS),
    );
    result.insert(
        "spearman_time_series_ic".into(),
        if x.len() >= 3 {
            spearman(&x, &y).map(Value::from).unwrap_or(Value::Null)
        } else {
            Value::Null
        },
    );
    result.insert(
        "pearson_correlation".into(),
        if x.len() >= 3 {
            pearson(&x, &y).map(Value::from).unwrap_or(Value::Null)
        } else {
            Value::Null
        },
    );
    result.insert(
        "mean_forward_return".into(),
        mean_of(&y).map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "top_minus_bottom_training_quantile_return".into(),
        spread.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "top_quantile_observations".into(),
        Value::from(high_returns.len()),
    );
    result.insert(
        "bottom_quantile_observations".into(),
        Value::from(low_returns.len()),
    );
    result.insert(
        "ic_interpretation".into(),
        Value::String(
            if enough {
                "meets the predeclared observation floor"
            } else {
                "exploratory point estimate; no confirmatory conclusion below the 20-observation floor"
            }
            .into(),
        ),
    );
    Value::Object(result)
}

/// Descriptive 5-session diagnostics for the frozen snapshot.
pub fn run(manifest: &Value, market: &Value) -> Result<Value, String> {
    let ticker = sd_core::contracts::py_str(manifest.get("ticker").unwrap_or(&Value::Null));
    let horizon_name = sd_core::contracts::py_str(manifest.get("horizon").unwrap_or(&Value::Null));
    let snapshot_id =
        sd_core::contracts::py_str(manifest.get("snapshot_id").unwrap_or(&Value::Null));
    let bars = market_bars(market, false).map_err(|error| error.message().to_string())?;
    let closes: Vec<f64> = bars.iter().map(|bar| bar.close).collect();
    let factors = factor_values(
        &bars
            .iter()
            .map(|bar| {
                serde_json::json!({
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                })
            })
            .collect::<Vec<_>>(),
    );
    let count = bars.len();
    let split = (count as f64 * 0.7) as usize;
    let horizon = policy::FORECAST_HORIZON_SESSIONS as usize;
    let forward = forward_returns(&closes, horizon);
    let mut metrics_map = Map::new();
    let mut available = 0i64;
    let mut observations = 0i64;
    let train_indices = non_overlapping(
        &(60..split)
            .filter(|index| index + horizon < split)
            .map(|index| index as i64)
            .collect::<Vec<_>>(),
        horizon as i64,
    );
    let holdout_indices = non_overlapping(
        &(split..count)
            .filter(|index| index + horizon < count)
            .map(|index| index as i64)
            .collect::<Vec<_>>(),
        horizon as i64,
    );
    for (name, family, values) in &factors {
        let (train_x, _) = aligned(values, &forward, &train_indices);
        let low_cut = quantile(&train_x, 0.2);
        let high_cut = quantile(&train_x, 0.8);
        let holdout = metrics(values, &forward, &holdout_indices, low_cut, high_cut);
        if holdout["status"] == "available" {
            available += 1;
        }
        observations += holdout["observations"].as_i64().unwrap_or(0);
        let mut entry = Map::new();
        entry.insert("family".into(), Value::String((*family).into()));
        entry.insert("horizon".into(), Value::String("5D".into()));
        entry.insert(
            "training".into(),
            metrics(values, &forward, &train_indices, low_cut, high_cut),
        );
        entry.insert("holdout".into(), holdout);
        metrics_map.insert((*name).to_string(), Value::Object(entry));
    }
    let status = if available != 0 {
        "success"
    } else if observations != 0 {
        "partial"
    } else {
        "insufficient_data"
    };
    let mut warnings: Vec<Value> = vec![
        Value::String(
            "Single-security 5-session IC/forward-return diagnostics only; not a cross-sectional Rank IC or portfolio backtest."
                .into(),
        ),
        Value::String(
            "Descriptive evidence: this path never feeds the production probability, eligibility or model promotion."
                .into(),
        ),
        Value::String(
            "Gross close-to-close diagnostics; no trades, funding, turnover costs or market impact are simulated."
                .into(),
        ),
    ];
    if available == 0 {
        warnings.push(Value::String(format!(
            "No 5D holdout factor reached {MIN_HOLDOUT_OBSERVATIONS} observations; ICs remain exploratory."
        )));
    }
    let primary = metrics_map
        .get("momentum_20")
        .and_then(|entry| entry.get("holdout"))
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let split_date = bars
        .get(split)
        .map(|bar| Value::String(bar.date.clone()))
        .unwrap_or(Value::Null);
    let split_block = {
        let mut map = Map::new();
        map.insert("training_fraction".into(), Value::from(0.7));
        map.insert("holdout_fraction".into(), Value::from(0.3));
        map.insert("holdout_start_session".into(), split_date);
        Value::Object(map)
    };
    let mut validation = Map::new();
    validation.insert("model_version".into(), Value::String(MODEL_VERSION.into()));
    validation.insert(
        "method".into(),
        Value::String("fixed-factor 5D time-series IC and forward-return diagnostics".into()),
    );
    validation.insert(
        "universe_type".into(),
        Value::String("single_security".into()),
    );
    validation.insert(
        "signal_timing".into(),
        Value::String(
            "factor uses OHLCV through close[t]; label is close[t+5] / close[t] - 1".into(),
        ),
    );
    validation.insert("split".into(), split_block);
    validation.insert("non_overlapping_labels".into(), Value::Bool(true));
    validation.insert(
        "minimum_holdout_observations".into(),
        Value::from(MIN_HOLDOUT_OBSERVATIONS),
    );
    validation.insert(
        "horizons".into(),
        Value::Array(vec![Value::String("5D".into())]),
    );
    validation.insert("descriptive_only".into(), Value::Bool(true));
    validation.insert(
        "information_coefficient".into(),
        primary
            .get("spearman_time_series_ic")
            .cloned()
            .unwrap_or(Value::Null),
    );
    validation.insert(
        "holdout_sample_count".into(),
        primary.get("observations").cloned().unwrap_or(Value::Null),
    );
    validation.insert("factor_metrics".into(), Value::Object(metrics_map));
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
        "training_quantile_cutoffs_fixed_before_holdout".into(),
        Value::Bool(true),
    );
    leakage.insert("holdout_labels_non_overlapping".into(), Value::Bool(true));
    validation.insert("leakage_audit".into(), Value::Object(leakage));
    let mut cross_sectional = Map::new();
    cross_sectional.insert("status".into(), Value::String("not_assessed".into()));
    cross_sectional.insert(
        "reason".into(),
        Value::String(
            "requires a multi-ticker factor panel, point-in-time universe and security masks"
                .into(),
        ),
    );
    validation.insert(
        "full_cross_sectional_backtest".into(),
        Value::Object(cross_sectional),
    );
    researcher_result(
        "factor_backtest",
        &ticker,
        &horizon_name,
        &snapshot_id,
        status,
        "diagnostic",
        None,
        None,
        serde_json::json!({"positive": [], "negative": []}),
        Value::Object(validation),
        vec![Value::String("daily split-adjusted OHLCV".into())],
        warnings,
        None,
        None,
    )
}
