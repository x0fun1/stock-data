//! Validate news-time market reactions using only frozen OHLCV bars (`news/reaction.py`).

use chrono::NaiveTime;
use chrono_tz::Tz;
use serde_json::{Map, Value};

use sd_core::contracts::parse_timestamp;
use sd_core::float_sum::python_sum;

fn exchange(market: &str) -> Option<(&'static str, NaiveTime)> {
    match market {
        "US" => Some((
            "America/New_York",
            NaiveTime::from_hms_opt(16, 0, 0).expect("close"),
        )),
        "HK" => Some((
            "Asia/Hong_Kong",
            NaiveTime::from_hms_opt(16, 0, 0).expect("close"),
        )),
        _ => None,
    }
}

fn returns(closes: &[f64]) -> Vec<f64> {
    let mut out = Vec::new();
    for index in 1..closes.len() {
        if closes[index - 1] > 0.0 {
            out.push(closes[index] / closes[index - 1] - 1.0);
        }
    }
    out
}

fn mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    Some(python_sum(values.iter().copied()) / values.len() as f64)
}

fn sample_std(values: &[f64]) -> Option<f64> {
    if values.len() < 2 {
        return None;
    }
    let center = mean(values)?;
    let variance = python_sum(
        values
            .iter()
            .map(|value| (value - center) * (value - center)),
    ) / (values.len() - 1) as f64;
    Some(variance.sqrt())
}

fn price_direction(value: Option<f64>) -> &'static str {
    match value {
        None => "unavailable",
        Some(value) if value > 0.0 => "positive",
        Some(value) if value < 0.0 => "negative",
        Some(_) => "flat",
    }
}

fn reaction_status(event_direction: &str, price: &str) -> &'static str {
    match (event_direction, price) {
        ("positive", "positive") => "confirmed_positive",
        ("positive", "negative") => "event_price_divergence_positive",
        ("negative", "positive") => "event_price_divergence_negative",
        ("negative", "negative") => "confirmed_negative",
        _ => "not_classifiable",
    }
}

fn unavailable_order(warning: &str, session: Value) -> Value {
    let mut map = Map::new();
    map.insert("status".into(), Value::String("unavailable".into()));
    map.insert("market_session_date".into(), session);
    map.insert(
        "session_mapping_rule".into(),
        Value::String("first frozen exchange session ending after publication".into()),
    );
    for key in [
        "daily_close_return",
        "opening_gap_return",
        "abnormal_volume_z",
        "pre_event_volatility_5d",
        "post_event_volatility_5d",
        "volatility_change",
        "sector_relative_return",
    ] {
        map.insert(key.into(), Value::Null);
    }
    map.insert("warning".into(), Value::String(warning.into()));
    Value::Object(map)
}

/// Validate the event-session reaction against the frozen bars.
pub fn validate_reaction(
    published_at: &str,
    event_direction: &str,
    market: &str,
    bars: &[Value],
    session_closes: Option<&Map<String, Value>>,
) -> Value {
    let Some((timezone, nominal_close)) = exchange(market) else {
        let mut map = Map::new();
        map.insert("status".into(), Value::String("unavailable".into()));
        map.insert(
            "warning".into(),
            Value::String(format!("no session-time mapping for market '{market}'")),
        );
        return Value::Object(map);
    };
    if bars.is_empty() {
        let mut map = Map::new();
        map.insert("status".into(), Value::String("unavailable".into()));
        map.insert(
            "warning".into(),
            Value::String("frozen market history contains no bars".into()),
        );
        return Value::Object(map);
    }
    let Ok(published) = parse_timestamp(&Value::String(published_at.to_string())) else {
        return unavailable_order(
            "news publication timestamp could not be parsed; no market reaction is assessed",
            Value::Null,
        );
    };
    let timezone = timezone.parse::<Tz>().expect("compiled-in timezone");
    let local = published.with_timezone(&timezone);
    let local_day = local.date_naive();
    let close_at = session_closes
        .and_then(|closes| closes.get(&local_day.to_string()))
        .and_then(Value::as_str)
        .and_then(|text| parse_timestamp(&Value::String(text.to_string())).ok());
    let same_session_eligible = match close_at {
        Some(close_at) => published <= close_at,
        None => local.time() <= nominal_close,
    };
    let dates: Vec<String> = bars
        .iter()
        .map(|row| {
            row.get("date")
                .map(sd_core::contracts::py_str)
                .unwrap_or_default()
                .chars()
                .take(10)
                .collect()
        })
        .collect();
    let first = dates.first().cloned().unwrap_or_default();
    if !first.is_empty() && local_day.to_string() < first {
        return unavailable_order(
            "news predates the available frozen OHLCV window; no market reaction is assessed",
            Value::Null,
        );
    }
    let local_text = local_day.to_string();
    let candidate = dates
        .iter()
        .position(|day| *day >= local_text && (*day > local_text || same_session_eligible));
    let Some(index) = candidate else {
        return unavailable_order(
            "no frozen market session follows this news item within the snapshot",
            Value::Null,
        );
    };
    let intraday = dates[index] == local_text
        && local.time() > NaiveTime::from_hms_opt(9, 30, 0).expect("open");
    let warning = "daily OHLCV cannot isolate intraday reaction or establish causality";
    let mut daily_return: Option<f64> = None;
    let mut opening_gap: Option<f64> = None;
    if index > 0 {
        let previous_close = bars[index - 1].get("close").and_then(Value::as_f64);
        let current_close = bars[index].get("close").and_then(Value::as_f64);
        let current_open = bars[index].get("open").and_then(Value::as_f64);
        if let (Some(previous_close), Some(current_close), Some(current_open)) =
            (previous_close, current_close, current_open)
        {
            if previous_close > 0.0 {
                daily_return = Some(current_close / previous_close - 1.0);
                opening_gap = if intraday {
                    None
                } else {
                    Some(current_open / previous_close - 1.0)
                };
            }
        }
    }
    let start = index.saturating_sub(20);
    let past_volumes: Vec<f64> = bars[start..index]
        .iter()
        .filter_map(|row| row.get("volume").and_then(Value::as_f64))
        .collect();
    let mut abnormal_volume_z: Option<f64> = None;
    if past_volumes.len() >= 5 {
        if let (Some(mean_volume), Some(std_volume)) =
            (mean(&past_volumes), sample_std(&past_volumes))
        {
            if std_volume > 0.0 {
                let current = bars[index]
                    .get("volume")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0);
                abnormal_volume_z = Some((current - mean_volume) / std_volume);
            }
        }
    }
    let closes: Vec<f64> = bars
        .iter()
        .filter_map(|row| row.get("close").and_then(Value::as_f64))
        .collect();
    let all_returns = returns(&closes);
    let pre_start = index.saturating_sub(6);
    let pre_end = index.saturating_sub(1);
    let pre_returns: Vec<f64> = all_returns
        .get(pre_start..pre_end.min(all_returns.len()))
        .map(|slice| slice.to_vec())
        .unwrap_or_default();
    let post_start = index.saturating_sub(1);
    let post_end = (index + 4).min(all_returns.len());
    let post_returns: Vec<f64> = all_returns
        .get(post_start..post_end)
        .map(|slice| slice.to_vec())
        .unwrap_or_default();
    let pre_vol = if pre_returns.len() >= 3 {
        sample_std(&pre_returns)
    } else {
        None
    };
    let post_vol = if post_returns.len() >= 3 {
        sample_std(&post_returns)
    } else {
        None
    };
    let vol_change = match (post_vol, pre_vol) {
        (Some(post), Some(pre)) => Some(post - pre),
        _ => None,
    };
    let direction = price_direction(daily_return);
    let (status, reaction_window) = if intraday {
        (
            "intraday_daily_association",
            "whole_session_includes_pre_publication",
        )
    } else {
        (
            reaction_status(event_direction, direction),
            "first_session_after_publication",
        )
    };
    let mut map = Map::new();
    map.insert("status".into(), Value::String(status.into()));
    map.insert(
        "reaction_window".into(),
        Value::String(reaction_window.into()),
    );
    map.insert(
        "calendar_mapping".into(),
        Value::String(
            if close_at.is_some() {
                "source_close_timestamp"
            } else {
                "nominal_close_estimate"
            }
            .into(),
        ),
    );
    map.insert(
        "market_session_date".into(),
        Value::String(dates[index].clone()),
    );
    map.insert(
        "session_mapping_rule".into(),
        Value::String(
            "source close timestamp when supplied, otherwise nominal local 16:00; first eligible frozen session"
                .into(),
        ),
    );
    map.insert(
        "daily_close_return".into(),
        daily_return.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "opening_gap_return".into(),
        opening_gap.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert("price_direction".into(), Value::String(direction.into()));
    map.insert(
        "abnormal_volume_z".into(),
        abnormal_volume_z.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "pre_event_volatility_5d".into(),
        pre_vol.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "post_event_volatility_5d".into(),
        post_vol.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "volatility_change".into(),
        vol_change.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert("sector_relative_return".into(), Value::Null);
    map.insert("warning".into(), Value::String(warning.into()));
    Value::Object(map)
}
