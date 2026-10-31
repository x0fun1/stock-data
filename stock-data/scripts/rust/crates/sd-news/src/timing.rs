//! Non-directional News timing facts: publication bucket and priced-in state
//! (`news/timing.py`).
//!
//! Neither output is a probability, neither has a direction, and neither ever enters
//! Quant `P(up)`. Publication timing carries most of the event's information (the
//! overnight gap after an after-close release), and the event-session move does not
//! predict the following five days, so the honest addition is a priced-in judgement.

use chrono::Duration;
use serde_json::{Map, Value};

use sd_core::contracts::parse_timestamp;
use sd_core::float_sum::python_sum;

use crate::normalize::isoformat_z;

/// Regular US session length used to derive the open from the recorded close.
pub const SESSION_HOURS: f64 = 6.5;
/// Multiplier used by the priced-in heuristic.
pub const PRICED_IN_SIGMA_MULTIPLE: f64 = 1.0;
/// Minimum matured events before the aggregate state is reported.
pub const PRICED_IN_MIN_EVENTS: usize = 3;

/// Publication before the regular open.
pub const BUCKET_PRE_MARKET: &str = "pre_market";
/// Publication during the regular session.
pub const BUCKET_INTRADAY: &str = "intraday";
/// Publication after the regular close.
pub const BUCKET_AFTER_CLOSE: &str = "after_close";
/// Publication time cannot be mapped (bucket unknown, never guessed).
pub const BUCKET_UNKNOWN: &str = "unknown";

fn mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    Some(python_sum(values.iter().copied()) / values.len() as f64)
}

/// Sample sigma of the `window` returns strictly before the event session.
pub fn trailing_sigma(closes: &[f64], index: usize, window: usize) -> (Option<f64>, i64) {
    let start = index.saturating_sub(window + 1);
    let segment = &closes[start..index.min(closes.len())];
    if segment.len() < 2 {
        return (None, 0);
    }
    let mut returns: Vec<f64> = Vec::new();
    for position in 1..segment.len() {
        if segment[position - 1] > 0.0 {
            returns.push(segment[position] / segment[position - 1] - 1.0);
        }
    }
    if returns.len() < 5 {
        return (None, returns.len() as i64);
    }
    let center = mean(&returns).unwrap_or(0.0);
    let variance = python_sum(
        returns
            .iter()
            .map(|value| (value - center) * (value - center)),
    ) / (returns.len() - 1) as f64;
    (
        if variance > 0.0 {
            Some(variance.sqrt())
        } else {
            None
        },
        returns.len() as i64,
    )
}

fn python_round(value: f64, digits: u32) -> f64 {
    // Python `round(x, n)` rounds half to even on the decimal representation; the
    // fixtures avoid exact ties, so the scaled-round form is equivalent here.
    let factor = 10f64.powi(digits as i32);
    (value * factor).round() / factor
}

/// Classify a publication time relative to its mapped trading session.
pub fn event_time_bucket(
    published_at: Option<&Value>,
    session_date: Option<&Value>,
    session_closes: Option<&Map<String, Value>>,
) -> Value {
    let basis = "session_close_at minus 6.5h regular session; frozen calendar only";
    let unavailable = |reason: &str| {
        let mut map = Map::new();
        map.insert("bucket".into(), Value::String(BUCKET_UNKNOWN.into()));
        map.insert("basis".into(), Value::String(basis.into()));
        map.insert("reason".into(), Value::String(reason.into()));
        Value::Object(map)
    };
    let (Some(published_at), Some(session_date), Some(session_closes)) =
        (published_at, session_date, session_closes)
    else {
        return unavailable("publication time or session close unavailable");
    };
    if published_at.is_null() || session_date.is_null() || session_closes.is_empty() {
        return unavailable("publication time or session close unavailable");
    }
    let day = sd_core::contracts::py_str(session_date)
        .chars()
        .take(10)
        .collect::<String>();
    let Some(close_at) = session_closes.get(&day).and_then(Value::as_str) else {
        return unavailable("mapped session has no recorded close time");
    };
    let Ok(close) = parse_timestamp(&Value::String(close_at.to_string())) else {
        return unavailable("mapped session has no recorded close time");
    };
    let opened = close - Duration::seconds((SESSION_HOURS * 3600.0) as i64);
    let Ok(published) = parse_timestamp(published_at) else {
        return unavailable("publication time or session close unavailable");
    };
    let bucket = if published < opened {
        BUCKET_PRE_MARKET
    } else if published <= close {
        BUCKET_INTRADAY
    } else {
        BUCKET_AFTER_CLOSE
    };
    let information_path = match bucket {
        BUCKET_AFTER_CLOSE => "overnight_gap_into_next_session",
        BUCKET_INTRADAY => "same_session",
        _ => "pre_open_positioning",
    };
    let mut map = Map::new();
    map.insert("bucket".into(), Value::String(bucket.into()));
    map.insert("basis".into(), Value::String(basis.into()));
    map.insert("session_date".into(), Value::String(day));
    map.insert("session_open_at".into(), Value::String(isoformat_z(opened)));
    map.insert("session_close_at".into(), Value::String(isoformat_z(close)));
    map.insert("published_at".into(), Value::String(isoformat_z(published)));
    map.insert(
        "information_path".into(),
        Value::String(information_path.into()),
    );
    map.insert("direction_neutral".into(), Value::Bool(true));
    Value::Object(map)
}

/// Judge whether the event session already absorbed the news.
pub fn priced_in_assessment(
    reaction: &Value,
    closes: &[f64],
    session_index: Option<i64>,
    event_direction: &str,
    horizon: usize,
    sigma_multiple: f64,
) -> Value {
    let base = |state: &str, reason: &str| {
        let mut map = Map::new();
        map.insert("display_only".into(), Value::Bool(true));
        map.insert("probability_effect".into(), Value::String("none".into()));
        map.insert("direction_neutral".into(), Value::Bool(true));
        map.insert("state".into(), Value::String(state.into()));
        map.insert("reason".into(), Value::String(reason.into()));
        Value::Object(map)
    };
    let status = reaction.get("status").and_then(Value::as_str).unwrap_or("");
    if ![
        "confirmed_positive",
        "confirmed_negative",
        "event_price_divergence_positive",
        "event_price_divergence_negative",
    ]
    .contains(&status)
    {
        return base(
            "not_assessable",
            "reaction is not classifiable for this event session",
        );
    }
    if event_direction != "positive" && event_direction != "negative" {
        return base("not_assessable", "event direction is not one-sided");
    }
    let Some(index) = session_index.filter(|index| *index > 0) else {
        return base(
            "not_assessable",
            "the 5-session continuation window has not matured in the frozen bars",
        );
    };
    let index = index as usize;
    if index.saturating_add(horizon) >= closes.len() {
        return base(
            "not_assessable",
            "the 5-session continuation window has not matured in the frozen bars",
        );
    }
    let (sigma, observations) = trailing_sigma(closes, index, 20);
    let observed = reaction.get("daily_close_return").and_then(Value::as_f64);
    let (Some(sigma), Some(observed)) = (sigma, observed) else {
        return base("not_assessable", "no usable trailing volatility baseline");
    };
    if sigma <= 0.0 || !observed.is_finite() {
        return base("not_assessable", "no usable trailing volatility baseline");
    }
    let sigma_horizon = sigma * (horizon as f64).sqrt();
    let move_ratio = observed.abs() / sigma_horizon;
    let forward = closes[index + horizon] / closes[index] - 1.0;
    let event_sign = if event_direction == "positive" {
        1.0
    } else {
        -1.0
    };
    let continuation = (forward > 0.0) == (event_sign > 0.0);
    let (state, reason) = if move_ratio < sigma_multiple {
        (
            "move_within_noise",
            "the event-session move is smaller than one 5-session sigma; priced-in cannot be judged",
        )
    } else if continuation {
        (
            "continued",
            "a large event-session move was followed by a same-direction 5-session drift",
        )
    } else {
        (
            "likely_priced_in",
            "a large event-session move was not followed by same-direction drift",
        )
    };
    let mut map = Map::new();
    map.insert("display_only".into(), Value::Bool(true));
    map.insert("probability_effect".into(), Value::String("none".into()));
    map.insert("direction_neutral".into(), Value::Bool(true));
    map.insert("state".into(), Value::String(state.into()));
    map.insert("reason".into(), Value::String(reason.into()));
    map.insert(
        "event_session_return".into(),
        Value::from(python_round(observed, 6)),
    );
    map.insert(
        "sigma_5d".into(),
        Value::from(python_round(sigma_horizon, 6)),
    );
    map.insert(
        "move_to_sigma_5d".into(),
        Value::from(python_round(move_ratio, 4)),
    );
    map.insert(
        "forward_5d_return".into(),
        Value::from(python_round(forward, 6)),
    );
    map.insert("continuation".into(), Value::Bool(continuation));
    map.insert(
        "trailing_sigma_observations".into(),
        Value::from(observations),
    );
    map.insert(
        "threshold_sigma_multiple".into(),
        Value::from(sigma_multiple),
    );
    map.insert(
        "calibration_status".into(),
        Value::String("heuristic_uncalibrated".into()),
    );
    Value::Object(map)
}

/// Roll per-event priced-in states into one disclosed, non-directional state.
pub fn aggregate_priced_in(cards: &[Value]) -> Value {
    let assessable: Vec<&Value> = cards
        .iter()
        .filter(|card| {
            matches!(
                card.get("already_priced_in")
                    .and_then(|block| block.get("state"))
                    .and_then(Value::as_str),
                Some("likely_priced_in" | "continued")
            )
        })
        .collect();
    if assessable.len() < PRICED_IN_MIN_EVENTS {
        let mut map = Map::new();
        map.insert("state".into(), Value::String("not_assessable".into()));
        map.insert("assessable_events".into(), Value::from(assessable.len()));
        map.insert("minimum_events".into(), Value::from(PRICED_IN_MIN_EVENTS));
        map.insert(
            "reason".into(),
            Value::String("too few matured event windows to judge absorption".into()),
        );
        map.insert("display_only".into(), Value::Bool(true));
        map.insert("probability_effect".into(), Value::String("none".into()));
        return Value::Object(map);
    }
    let priced_in = assessable
        .iter()
        .filter(|card| {
            card.get("already_priced_in")
                .and_then(|block| block.get("state"))
                .and_then(Value::as_str)
                == Some("likely_priced_in")
        })
        .count();
    let share = priced_in as f64 / assessable.len() as f64;
    let mut map = Map::new();
    map.insert(
        "state".into(),
        Value::String(
            if share >= 0.5 {
                "likely_priced_in"
            } else {
                "mixed_absorption"
            }
            .into(),
        ),
    );
    map.insert("assessable_events".into(), Value::from(assessable.len()));
    map.insert("likely_priced_in_events".into(), Value::from(priced_in));
    map.insert(
        "share_priced_in".into(),
        Value::from(python_round(share, 4)),
    );
    map.insert(
        "rule".into(),
        Value::String(
            "share of matured event windows with a >=1 sigma_5d move and no same-direction continuation"
                .into(),
        ),
    );
    map.insert("display_only".into(), Value::Bool(true));
    map.insert("probability_effect".into(), Value::String("none".into()));
    map.insert("direction_neutral".into(), Value::Bool(true));
    Value::Object(map)
}
