//! Configurable event recency decay in observed exchange sessions (`news/decay.py`).

use chrono_tz::Tz;
use serde_json::{Map, Value};

use sd_core::contracts::parse_timestamp;
use sd_core::policy::EXCHANGE_TIMEZONES;
use sd_core::policy::HORIZONS;

/// A News item older than this is outside the supported recency window.
pub const NEWS_MAX_AGE_CALENDAR_DAYS: i64 = 90;
/// Default half-life when the policy gives no override.
pub const NEWS_DEFAULT_HALF_LIFE_SESSIONS: f64 = 30.0;

fn exchange_timezone(market: &str) -> Result<Tz, String> {
    let timezone = EXCHANGE_TIMEZONES
        .iter()
        .find(|(code, _)| *code == market)
        .map(|(_, timezone)| *timezone)
        .ok_or_else(|| format!("unknown market for exchange timezone: {market}"))?;
    timezone.parse::<Tz>().map_err(|error| error.to_string())
}

fn horizon_sessions(horizon: &str) -> f64 {
    HORIZONS
        .iter()
        .find(|(name, _)| *name == horizon)
        .map(|(_, sessions)| f64::from(*sessions))
        .unwrap_or(5.0)
}

/// Half-life in sessions for one event type, honouring per-type overrides.
pub fn half_life_for(event_type: &str, horizon: &str, policy: &Value) -> Result<f64, String> {
    let overrides = policy.get("half_life_sessions_by_event_type");
    let fallback = horizon_sessions(horizon).max(NEWS_DEFAULT_HALF_LIFE_SESSIONS);
    let value = match overrides.and_then(Value::as_object) {
        Some(overrides) => overrides
            .get(event_type)
            .or_else(|| overrides.get("default"))
            .cloned()
            .unwrap_or(Value::from(fallback)),
        None => Value::from(fallback),
    };
    let number = match &value {
        Value::Number(number) => number.as_f64().unwrap_or(fallback),
        Value::Bool(_) => fallback,
        Value::String(text) => text.parse::<f64>().unwrap_or(fallback),
        _ => fallback,
    };
    if !number.is_finite() || number <= 0.0 {
        return Err("news half-life values must be finite positive session counts".into());
    }
    Ok(number)
}

/// Recency decay of one event, measured in observed sessions before the decision.
pub fn time_decay(
    event_published_at: &str,
    bars: &[Value],
    event_type: &str,
    horizon: &str,
    policy: &Value,
    market: &str,
) -> Result<Map<String, Value>, String> {
    let half_life = half_life_for(event_type, horizon, policy)?;
    let timezone = exchange_timezone(market)?;
    let event_day = parse_timestamp(&Value::String(event_published_at.to_string()))
        .map_err(|error| error.message().to_string())?
        .with_timezone(&timezone)
        .date_naive();
    let mut age_sessions = 0i64;
    for row in bars {
        let session = row
            .get("date")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(10)
            .collect::<String>();
        let Ok(session_day) = chrono::NaiveDate::parse_from_str(&session, "%Y-%m-%d") else {
            continue;
        };
        if session_day > event_day {
            age_sessions += 1;
        }
    }
    let decay = 0.5f64.powf(age_sessions as f64 / half_life);
    let mut result = Map::new();
    result.insert("age_sessions".into(), Value::from(age_sessions));
    result.insert("half_life_sessions".into(), Value::from(half_life));
    result.insert("time_decay".into(), Value::from(decay));
    Ok(result)
}
