//! Neutral access to the normalized OHLCV series inside a frozen snapshot.
//!
//! Port of `quant_research/market_data.py`. Two price-series readers exist on
//! purpose: the production/shadow/audit prediction target is the provider
//! split-adjusted **close** (no dividend reinvestment), while the composite momentum
//! feature may use an explicitly recorded adjusted series. Mixing the bases would
//! silently change the target definition, so the basis is returned, never assumed.

use chrono::NaiveDate;
use serde_json::{Map, Value};

use crate::error::{SdError, SdResult};
use crate::interfaces::Bar;
use crate::policy;

/// Epoch ordinal of 1969-12-29 (a Monday); a non-Monday epoch would collide
/// adjacent weekdays in the ordinal formula.
const MONDAY_EPOCH_ORDINAL: i64 = 719_160;

fn text(value: Option<&Value>) -> Option<&str> {
    value.and_then(Value::as_str)
}

/// Normalize OHLCV rows, refusing a blocked snapshot unless read for diagnosis.
pub fn market_bars(block: &Value, diagnostic: bool) -> SdResult<Vec<Bar>> {
    let eligible = block
        .get("_data_validation")
        .and_then(|value| value.get("forecast_eligible"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !eligible && !diagnostic {
        return Err(SdError::validation(
            "prediction eligibility is blocked by fatal data gates; pass diagnostic=True only for read-only diagnostics that will not produce a probability",
        ));
    }
    let Some(bars) = block
        .get("data")
        .and_then(|value| value.get("bars"))
        .and_then(Value::as_array)
    else {
        return Err(SdError::validation(
            "market data must contain normalized daily bars",
        ));
    };
    let mut result = Vec::with_capacity(bars.len());
    for bar in bars {
        let date = crate::contracts::py_str(bar.get("date").unwrap_or(&Value::Null));
        let date = date.get(..10).unwrap_or(&date).to_string();
        let number = |key: &str| -> SdResult<f64> {
            let raw = bar
                .get(key)
                .ok_or_else(|| SdError::validation(format!("market bar is missing {key}")))?;
            let value = match raw {
                Value::Number(number) => number.as_f64(),
                Value::String(text) => text.trim().parse::<f64>().ok(),
                _ => None,
            }
            .ok_or_else(|| SdError::validation(format!("market bar has non-numeric {key}")))?;
            if !value.is_finite() {
                return Err(SdError::validation(format!(
                    "non-finite {key} in market bar"
                )));
            }
            Ok(value)
        };
        result.push(Bar {
            date,
            open: number("open")?,
            high: number("high")?,
            low: number("low")?,
            close: number("close")?,
            volume: number("volume")?,
        });
    }
    Ok(result)
}

/// Simple return `end / start - 1`.
pub fn simple_return(start: f64, end: f64) -> SdResult<f64> {
    if !start.is_finite() || !end.is_finite() || start <= 0.0 {
        return Err(SdError::validation(
            "simple return requires finite positive start and finite end",
        ));
    }
    Ok(end / start - 1.0)
}

fn check_ordered_dates(dates: &[String]) -> SdResult<()> {
    let mut seen: Vec<&String> = Vec::new();
    for date in dates {
        if seen.contains(&date) {
            return Err(SdError::validation(
                "duplicate trading dates in market bars",
            ));
        }
        seen.push(date);
    }
    if dates.windows(2).any(|pair| pair[0] > pair[1]) {
        return Err(SdError::validation(
            "market bars must be sorted ascending by date",
        ));
    }
    Ok(())
}

/// `(dates, prices, provenance)` on a single, explicitly recorded basis.
pub fn adjusted_close_series(block: &Value) -> SdResult<(Vec<String>, Vec<f64>, Value)> {
    let data = block.get("data").filter(|value| value.is_object());
    let Some(bars) = data
        .and_then(|value| value.get("bars"))
        .and_then(Value::as_array)
        .filter(|rows| !rows.is_empty())
    else {
        return Err(SdError::validation(
            "market data must contain normalized daily bars",
        ));
    };
    let declared = text(block.get("adjustment"))
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    let all_adjusted = bars.iter().all(|bar| match bar.get("adj_close") {
        Some(Value::Number(number)) => number
            .as_f64()
            .map(|value| value.is_finite() && value > 0.0)
            .unwrap_or(false),
        _ => false,
    });
    let mut dates = Vec::with_capacity(bars.len());
    let mut values = Vec::with_capacity(bars.len());
    for bar in bars {
        let date = crate::contracts::py_str(bar.get("date").unwrap_or(&Value::Null));
        let date = date.get(..10).unwrap_or(&date).to_string();
        let close = match bar.get("close") {
            Some(Value::Number(number)) => number.as_f64(),
            _ => None,
        };
        let Some(close) = close.filter(|value| value.is_finite()) else {
            return Err(SdError::validation(format!("non-finite close on {date}")));
        };
        dates.push(date);
        if all_adjusted {
            let adjusted = match bar.get("adj_close") {
                Some(Value::Number(number)) => number.as_f64().unwrap_or(close),
                _ => close,
            };
            values.push(adjusted);
        } else {
            values.push(close);
        }
    }
    check_ordered_dates(&dates)?;
    if values.iter().any(|value| *value <= 0.0) {
        return Err(SdError::validation(
            "momentum requires strictly positive prices",
        ));
    }
    let (basis_status, price_field) = if all_adjusted {
        (
            if matches!(declared.as_str(), "adjusted" | "adjust") {
                "verified_adjusted"
            } else {
                "declared_adjusted"
            },
            "adjusted_close",
        )
    } else if matches!(declared.as_str(), "adjusted" | "adjust") {
        ("declared_adjusted", "close_declared_adjusted")
    } else {
        ("unverified", "close")
    };
    let mut provenance = Map::new();
    provenance.insert("price_field".into(), Value::String(price_field.into()));
    provenance.insert("basis_status".into(), Value::String(basis_status.into()));
    provenance.insert(
        "declared_adjustment".into(),
        if declared.is_empty() {
            Value::Null
        } else {
            Value::String(declared)
        },
    );
    provenance.insert("sessions".into(), Value::from(dates.len()));
    provenance.insert(
        "first_date".into(),
        Value::String(dates.first().cloned().unwrap_or_default()),
    );
    provenance.insert(
        "last_date".into(),
        Value::String(dates.last().cloned().unwrap_or_default()),
    );
    Ok((dates, values, Value::Object(provenance)))
}

/// Canonical price series for every production, shadow and audit prediction.
///
/// Uses the provider split-adjusted `close` directly and refuses to substitute a
/// dividend-adjusted `adj_close`, which would silently change the target.
pub fn prediction_price_series(block: &Value) -> SdResult<(Vec<String>, Vec<f64>, Value)> {
    let data = block.get("data").filter(|value| value.is_object());
    let Some(bars) = data
        .and_then(|value| value.get("bars"))
        .and_then(Value::as_array)
        .filter(|rows| !rows.is_empty())
    else {
        return Err(SdError::validation(
            "market data must contain normalized daily bars",
        ));
    };
    let declared = text(block.get("price_basis"));
    if declared != Some(policy::PRICE_BASIS) {
        return Err(SdError::validation(format!(
            "price basis {} is not the policy basis {}; a dividend-reinvested series cannot serve the 5D price-direction target",
            crate::contracts::py_repr(&block.get("price_basis").cloned().unwrap_or(Value::Null)),
            crate::contracts::py_repr(&Value::String(policy::PRICE_BASIS.to_string()))
        )));
    }
    let mut dates = Vec::with_capacity(bars.len());
    let mut values = Vec::with_capacity(bars.len());
    for bar in bars {
        let day = crate::contracts::py_str(bar.get("date").unwrap_or(&Value::Null));
        let day = day.get(..10).unwrap_or(&day).to_string();
        let close = match bar.get("close") {
            Some(Value::Number(number)) => number.as_f64(),
            _ => None,
        };
        let Some(close) = close.filter(|value| value.is_finite() && *value > 0.0) else {
            return Err(SdError::validation(format!(
                "non-finite or non-positive close on {day}"
            )));
        };
        dates.push(day);
        values.push(close);
    }
    check_ordered_dates(&dates)?;
    let mut provenance = Map::new();
    provenance.insert("price_field".into(), Value::String("close".into()));
    provenance.insert(
        "price_basis".into(),
        Value::String(policy::PRICE_BASIS.into()),
    );
    provenance.insert(
        "price_basis_source".into(),
        Value::String(
            text(block.get("price_basis_source"))
                .filter(|value| !value.is_empty())
                .unwrap_or(policy::PRICE_BASIS_SOURCE)
                .to_string(),
        ),
    );
    provenance.insert(
        "price_basis_evidence".into(),
        Value::String("declared_by_provider".into()),
    );
    provenance.insert(
        "price_basis_evidence_status".into(),
        Value::String("declared_not_independently_verified".into()),
    );
    provenance.insert("dividends_reinvested".into(), Value::Bool(false));
    provenance.insert(
        "target_version".into(),
        Value::String(policy::TARGET_VERSION.into()),
    );
    provenance.insert(
        "horizon_sessions".into(),
        Value::from(policy::FORECAST_HORIZON_SESSIONS),
    );
    provenance.insert("sessions".into(), Value::from(dates.len()));
    provenance.insert(
        "first_date".into(),
        Value::String(dates.first().cloned().unwrap_or_default()),
    );
    provenance.insert(
        "last_date".into(),
        Value::String(dates.last().cloned().unwrap_or_default()),
    );
    Ok((dates, values, Value::Object(provenance)))
}

/// Frozen session calendar carried by the block (normalized or unwrapped data).
pub fn calendar_session_list(block: &Value) -> Option<Vec<NaiveDate>> {
    let calendar = block
        .get("data")
        .and_then(|value| value.get("session_calendar"))
        .or_else(|| block.get("session_calendar"))?;
    let rows = calendar.get("sessions")?.as_array()?;
    Some(
        rows.iter()
            .filter_map(|row| row.get("date").and_then(Value::as_str))
            .filter_map(|date| policy::parse_iso_date(date).ok())
            .collect(),
    )
}

/// Select the fixed exchange-session phase (anchor 2000-01-03, ordinal % modulus == 0).
pub fn fixed_phase_sample(
    dates: &[String],
    sessions: &[NaiveDate],
    modulus: u32,
) -> (Vec<String>, Value) {
    let mut ordinals: Vec<Option<i64>> = Vec::with_capacity(dates.len());
    for day in dates {
        let ordinal = policy::parse_iso_date(day)
            .ok()
            .and_then(|date| policy::session_ordinal(sessions, date));
        ordinals.push(ordinal);
    }
    let anchored = ordinals.iter().any(Option::is_some);
    let sampled: Vec<String> = dates
        .iter()
        .zip(ordinals.iter())
        .filter(|(_, ordinal)| policy::is_sampled_ordinal(**ordinal))
        .map(|(day, _)| day.clone())
        .collect();
    let mut provenance = Map::new();
    provenance.insert(
        "method".into(),
        Value::String("fixed_exchange_session_phase".into()),
    );
    provenance.insert(
        "status".into(),
        Value::String(if anchored { "anchored" } else { "unanchored" }.into()),
    );
    provenance.insert(
        "anchor_session".into(),
        Value::String(policy::iso_date(policy::sampling_anchor())),
    );
    provenance.insert("modulus".into(), Value::from(modulus));
    provenance.insert("phase".into(), Value::from(0));
    provenance.insert("session_gap".into(), Value::from(modulus));
    provenance.insert("sample_size".into(), Value::from(sampled.len()));
    provenance.insert(
        "dates_without_session_ordinal".into(),
        Value::from(ordinals.iter().filter(|value| value.is_none()).count()),
    );
    provenance.insert("phase_window_independent".into(), Value::Bool(anchored));
    provenance.insert(
        "role".into(),
        Value::String("throttle_only_not_qualification".into()),
    );
    provenance.insert(
        "session_gap_is_sampling_policy_not_label_independence".into(),
        Value::Bool(true),
    );
    if !anchored {
        provenance.insert(
            "fallback".into(),
            Value::String("all_rows_when_phase_unanchored".into()),
        );
        return (dates.to_vec(), Value::Object(provenance));
    }
    (sampled, Value::Object(provenance))
}

fn python_round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

/// Report total/matured/non-overlapping counts and coverage for evaluation.
pub fn evaluation_sample(predictions: &[Value], sessions: Option<&[NaiveDate]>) -> Value {
    let matured: Vec<&Value> = predictions
        .iter()
        .filter(|row| row.get("status").and_then(Value::as_str) == Some("matured"))
        .filter(|row| matches!(row.get("probability"), Some(Value::Number(_))))
        .collect();
    let mut result = Map::new();
    let Some(sessions) = sessions else {
        result.insert("sampled_predictions".into(), Value::Array(Vec::new()));
        result.insert("total_predictions".into(), Value::from(predictions.len()));
        result.insert("matured_predictions".into(), Value::from(matured.len()));
        result.insert("nonoverlap_predictions".into(), Value::from(0));
        result.insert("coverage_ratio".into(), Value::Null);
        let mut sampling = Map::new();
        sampling.insert("method".into(), Value::String("unavailable".into()));
        result.insert("sampling".into(), Value::Object(sampling));
        return Value::Object(result);
    };
    let ordinals: Vec<(String, Option<i64>)> = sessions
        .iter()
        .map(|date| {
            (
                policy::iso_date(*date),
                policy::session_ordinal(sessions, *date),
            )
        })
        .collect();
    let mut decision_dates: Vec<String> = predictions
        .iter()
        .filter_map(|row| row.get("decision_session").and_then(Value::as_str))
        .filter(|day| ordinals.iter().any(|(date, _)| date == day))
        .map(str::to_string)
        .collect();
    decision_dates.sort();
    decision_dates.dedup();
    if decision_dates.is_empty() {
        result.insert("sampled_predictions".into(), Value::Array(Vec::new()));
        result.insert("total_predictions".into(), Value::from(predictions.len()));
        result.insert("matured_predictions".into(), Value::from(matured.len()));
        result.insert("nonoverlap_predictions".into(), Value::from(0));
        result.insert("coverage_ratio".into(), Value::Null);
        let mut sampling = Map::new();
        sampling.insert("method".into(), Value::String("unavailable".into()));
        result.insert("sampling".into(), Value::Object(sampling));
        return Value::Object(result);
    }
    let first = decision_dates.first().cloned().unwrap_or_default();
    let last = decision_dates.last().cloned().unwrap_or_default();
    let anchored = ordinals.iter().any(|(_, ordinal)| ordinal.is_some());
    let expected: Vec<String> = ordinals
        .iter()
        .filter(|(date, ordinal)| {
            date.as_str() >= first.as_str()
                && date.as_str() <= last.as_str()
                && policy::is_sampled_ordinal(*ordinal)
        })
        .map(|(date, _)| date.clone())
        .collect();
    let sampled_rows: Vec<Value> = if anchored {
        let mut by_date: Vec<(String, &Value)> = Vec::new();
        for row in &matured {
            if let Some(day) = row.get("decision_session").and_then(Value::as_str) {
                by_date.push((day.to_string(), row));
            }
        }
        expected
            .iter()
            .filter_map(|day| {
                by_date
                    .iter()
                    .find(|(date, _)| date == day)
                    .map(|(_, row)| (*row).clone())
            })
            .collect()
    } else {
        matured.iter().map(|row| (*row).clone()).collect()
    };
    let indices: Vec<i64> = predictions
        .iter()
        .filter(|row| row.get("status").and_then(Value::as_str) == Some("matured"))
        .filter_map(|row| row.get("decision_index").and_then(Value::as_i64))
        .collect();
    let nonoverlap = non_overlapping(&indices, policy::FORECAST_HORIZON_SESSIONS as i64).len();
    result.insert(
        "sampled_predictions".into(),
        Value::Array(sampled_rows.clone()),
    );
    result.insert("total_predictions".into(), Value::from(predictions.len()));
    result.insert("matured_predictions".into(), Value::from(matured.len()));
    result.insert("nonoverlap_predictions".into(), Value::from(nonoverlap));
    result.insert(
        "expected_sampled_sessions".into(),
        Value::from(expected.len()),
    );
    result.insert(
        "coverage_ratio".into(),
        if anchored && !expected.is_empty() {
            Value::from(python_round4(
                sampled_rows.len() as f64 / expected.len() as f64,
            ))
        } else {
            Value::Null
        },
    );
    let mut sampling = Map::new();
    sampling.insert(
        "method".into(),
        Value::String("fixed_exchange_session_phase".into()),
    );
    sampling.insert(
        "status".into(),
        Value::String(if anchored { "anchored" } else { "unanchored" }.into()),
    );
    sampling.insert(
        "anchor_session".into(),
        Value::String(policy::iso_date(policy::sampling_anchor())),
    );
    sampling.insert("modulus".into(), Value::from(policy::SAMPLING_MODULUS));
    sampling.insert("phase".into(), Value::from(0));
    sampling.insert("session_gap".into(), Value::from(policy::SAMPLING_MODULUS));
    sampling.insert(
        "session_gap_is_sampling_policy_not_label_independence".into(),
        Value::Bool(true),
    );
    sampling.insert("phase_window_independent".into(), Value::Bool(anchored));
    sampling.insert(
        "role".into(),
        Value::String("throttle_only_not_qualification".into()),
    );
    result.insert("sampling".into(), Value::Object(sampling));
    Value::Object(result)
}

/// Map session t to `close[t+h] / close[t] - 1`; the final horizon rows are unlabeled.
pub fn forward_returns(closes: &[f64], horizon: usize) -> Vec<Option<f64>> {
    let mut result = vec![None; closes.len()];
    let end = closes.len().saturating_sub(horizon);
    for index in 0..end {
        result[index] = simple_return(closes[index], closes[index + horizon]).ok();
    }
    result
}

/// Greedy non-overlapping index selection (legacy spacing).
pub fn non_overlapping(indices: &[i64], horizon: i64) -> Vec<i64> {
    let mut sorted = indices.to_vec();
    sorted.sort_unstable();
    let mut kept = Vec::new();
    let mut last_end = -1i64;
    for index in sorted {
        if index > last_end {
            kept.push(index);
            last_end = index + horizon;
        }
    }
    kept
}

/// Deterministic weekday count from a fixed Monday epoch.
///
/// Depends only on the calendar date, so the same history keeps the same phase no
/// matter where a rolling window starts. It counts Monday-Friday and therefore does
/// not know exchange holidays: callers must still enforce the session gap.
pub fn business_day_ordinal(day: &str) -> SdResult<i64> {
    let date = policy::parse_iso_date(day)?;
    let proleptic_epoch =
        NaiveDate::from_ymd_opt(1, 1, 1).expect("the proleptic epoch is a valid date");
    // Python `date.toordinal()` numbers 0001-01-01 as 1.
    let to_ordinal = (date - proleptic_epoch).num_days() + 1;
    let offset = to_ordinal - MONDAY_EPOCH_ORDINAL;
    let weeks = offset.div_euclid(7);
    let remainder = offset.rem_euclid(7);
    Ok(weeks * 5 + remainder.min(5))
}

/// Non-overlapping sample with a window-independent calendar candidate phase.
pub fn anchored_non_overlapping(
    dates: &[String],
    indices: &[i64],
    horizon: i64,
    phase: i64,
) -> (Vec<i64>, Value) {
    let required_gap = (horizon + 1).max(1);
    let weekday_step = (horizon + 2).max(2);
    let mut usable: Vec<i64> = indices
        .iter()
        .copied()
        .filter(|index| *index >= 0 && (*index as usize) < dates.len())
        .collect();
    usable.sort_unstable();
    let candidates: Vec<i64> = usable
        .iter()
        .copied()
        .filter(|index| {
            business_day_ordinal(&dates[*index as usize])
                .map(|ordinal| ordinal.rem_euclid(weekday_step) == phase)
                .unwrap_or(false)
        })
        .collect();
    let mut kept = Vec::new();
    let mut dropped_for_overlap = 0;
    let mut last_end = -1i64;
    for index in &candidates {
        if *index > last_end {
            kept.push(*index);
            last_end = *index + horizon;
        } else {
            dropped_for_overlap += 1;
        }
    }
    let mut provenance = Map::new();
    provenance.insert(
        "method".into(),
        Value::String("absolute_weekday_phase_then_session_gap".into()),
    );
    provenance.insert("phase".into(), Value::from(phase));
    provenance.insert("weekday_step".into(), Value::from(weekday_step));
    provenance.insert("required_session_gap".into(), Value::from(required_gap));
    provenance.insert("candidates".into(), Value::from(candidates.len()));
    provenance.insert("kept".into(), Value::from(kept.len()));
    provenance.insert(
        "dropped_for_overlap".into(),
        Value::from(dropped_for_overlap),
    );
    provenance.insert(
        "weekday_ordinal_ignores_exchange_holidays".into(),
        Value::Bool(true),
    );
    provenance.insert(
        "candidate_phase_window_independent".into(),
        Value::Bool(true),
    );
    provenance.insert(
        "final_membership_window_independent".into(),
        Value::Bool(false),
    );
    if kept.is_empty() {
        let fallback = non_overlapping(&usable, horizon);
        provenance.insert(
            "method".into(),
            Value::String("legacy_spacing_fallback".into()),
        );
        provenance.insert("kept".into(), Value::from(fallback.len()));
        provenance.insert(
            "candidate_phase_window_independent".into(),
            Value::Bool(false),
        );
        provenance.insert(
            "fallback_reason".into(),
            Value::String("no_phase_candidates".into()),
        );
        return (fallback, Value::Object(provenance));
    }
    (kept, Value::Object(provenance))
}
