//! The fixed-52-week / 5-session forecast policy.
//!
//! Port of `stock-data/scripts/quant_research/forecast_policy.py`. Every
//! producer and auditor reads the constants and helpers below instead of
//! re-deriving the window, horizon, sampling phase or version labels.

use chrono::{Datelike, NaiveDate};
use serde_json::{Map, Value};

use crate::error::SdError;

pub use crate::versions::POLICY_VERSION;

/// History window length. The window is `(decision - 364d, decision]`.
pub const HISTORY_CALENDAR_DAYS: i64 = 364;
/// Forecast target: close of the 5th exchange session after the decision.
pub const FORECAST_HORIZON_SESSIONS: u32 = 5;
/// Label used by requests and reports.
pub const FORECAST_HORIZON_LABEL: &str = "5D";
/// Window is right-closed.
pub const WINDOW_CLOSED: &str = "right";

/// Evaluation sampling gap between non-overlapping labels.
pub const NONOVERLAP_GAP_SESSIONS: u32 = 6;
/// Valid replay (window-internal) non-overlapping floor, disclosure-only under v2.
pub const MIN_VALID_OOS_NONOVERLAP: u32 = 12;
/// Independent forward (prospective) non-overlapping floor.
pub const MIN_PROSPECTIVE_NONOVERLAP: u32 = 30;
/// Minimum paired replay observations before a bootstrap interval is reported.
pub const MIN_REPLAY_OBSERVATIONS: u32 = 30;
/// A prospective ledger record only earns forward-evidence status when written
/// within this window after the decision close.
pub const PROSPECTIVE_RECORDING_GRACE_HOURS: i64 = 12;
/// The qualification floor applied to effective (valid_rows / horizon) samples.
pub const MIN_REPLAY_EFFECTIVE_SAMPLES: u32 = 12;

/// Reference session for the fixed sampling phase.
pub const SAMPLING_ANCHOR_SESSION: (i32, u32, u32) = (2000, 1, 3);
/// Sampling selects ordinals divisible by this modulus.
pub const SAMPLING_MODULUS: u32 = 6;

pub const PRODUCTION_PATHS: &[&str] = &["quant"];
pub const SHADOW_PATHS: &[&str] = &["factor", "ml"];
pub const DIAGNOSTIC_PATHS: &[&str] = &["factor_backtest"];

pub const MODEL_QUANT: &str = "quant_drift_shrinkage_v2";
pub const MODEL_FACTOR: &str = "factor_restricted_two_honest_v1";
pub const MODEL_ML: &str = "ml_compact_daily_v1";
pub const MODEL_FACTOR_BACKTEST: &str = "factor_backtest_5d_descriptive_v1";

pub const FEATURE_QUANT: &str = "roc20_v1";
pub const FEATURE_FACTOR: &str = "factor_bucket_v1";
pub const FEATURE_ML: &str = "compact_v1";
pub const FEATURE_FACTOR_BACKTEST: &str = "factor_diagnostic_v1";

/// Frozen 5D target version.
pub const TARGET_VERSION: &str = "split_adjusted_price_direction_5d_v1";
/// The 5D target excludes cash dividends.
pub const TARGET_DIVIDEND_ADJUSTED: bool = false;
/// Price basis required by this policy.
pub const PRICE_BASIS: &str = "split_adjusted";
/// Provenance label for that basis.
pub const PRICE_BASIS_SOURCE: &str = "provider_declared_split_adjusted_close";
/// Human-readable price-basis note.
pub const PRICE_BASIS_NOTE: &str = "provider close, split-adjusted, cash dividends not reinvested; fields that include dividend adjustment are not accepted as this basis";
/// The collector's canonical provider declaration that maps to [`PRICE_BASIS`].
/// A literal `adjustment="split_adjusted"` is *not* the production convention.
pub const PROVIDER_ADJUSTMENT_DECLARATION: &str = "provider";

/// Legacy v1 shrinkage constants (kept so historic ledger rows stay auditable).
pub const SHRINKAGE_PRIOR_STRENGTH: u32 = 12;
pub const SHRINKAGE_MIN_MATCHES: u32 = 12;
pub const SHRINKAGE_VERSION: &str = "shrinkage_v1";
pub const ROC_LOOKBACK_SESSIONS: u32 = 20;

/// v2 production estimator.
pub const DRIFT_SHRINKAGE_VERSION: &str = "drift_shrinkage_v2";
pub const DRIFT_SHRINKAGE_K: u32 = 50;
pub const DRIFT_MU_ANNUAL: f64 = 0.06;
pub const DRIFT_VOL_WINDOW_SESSIONS: u32 = 250;
pub const DRIFT_MIN_VOL_SESSIONS: u32 = 20;
pub const SESSIONS_PER_YEAR: f64 = 252.0;
/// ROC20 is disclosed, never weighted.
pub const FEATURE_ROLE_IN_PROBABILITY: &str = "disclosure_only";

pub const FACTOR_MIN_BUCKET_OBSERVATIONS: u32 = 6;
pub const ML_MIN_TRAIN_ROWS: u32 = 60;
pub const ML_WARMUP_SESSIONS: u32 = 20;
pub const FACTOR_BACKTEST_HOLDOUT_MIN: u32 = 20;

/// Receipt stages required for a complete forecast run, in order.
pub const REQUIRED_STAGES: &[&str] = &[
    "request",
    "collection",
    "data",
    "quant",
    "audit",
    "news",
    "cross-validation",
    "risk",
    "final",
];

pub const ERROR_UNSUPPORTED_HORIZON: &str = "UNSUPPORTED_FORECAST_HORIZON";
pub const ERROR_FIXED_WINDOW_CONFLICT: &str = "FIXED_WINDOW_CONFLICT";
pub const ERROR_INSUFFICIENT_COVERAGE: &str = "INSUFFICIENT_HISTORY_COVERAGE";
pub const ERROR_CALENDAR_UNVERIFIED: &str = "CALENDAR_UNVERIFIED";
pub const ERROR_PRICE_BASIS_UNVERIFIED: &str = "PRICE_BASIS_UNVERIFIED";
pub const ERROR_POLICY_MISMATCH: &str = "POLICY_VERSION_MISMATCH";
pub const ERROR_TARGET_SESSION_UNRESOLVED: &str = "TARGET_SESSION_UNRESOLVED";

/// Supported markets and their exchange time zone (IANA). A time zone is *not* a
/// trading calendar; the calendar is supplied by `CalendarProvider`.
pub const EXCHANGE_TIMEZONES: &[(&str, &str)] =
    &[("US", "America/New_York"), ("HK", "Asia/Hong_Kong")];
/// Supported horizons (sessions).
pub const HORIZONS: &[(&str, u32)] = &[("1D", 1), ("5D", 5), ("20D", 20)];
/// Supported intents.
pub const INTENTS: &[&str] = &["forecast", "descriptive", "factor_research"];
/// Supported asset types.
pub const ASSET_TYPES: &[&str] = &["stock", "etf", "index", "unknown"];
/// Supported request modes.
pub const MODES: &[&str] = &["standard", "strict"];
/// Supported markets.
pub const MARKETS: &[&str] = &["US", "HK"];

/// IANA time zone for a market, when supported.
pub fn exchange_timezone(market: &str) -> Option<&'static str> {
    EXCHANGE_TIMEZONES
        .iter()
        .find(|(name, _)| *name == market)
        .map(|(_, zone)| *zone)
}

/// Sessions for a horizon label.
pub fn horizon_sessions(label: &str) -> Option<u32> {
    HORIZONS
        .iter()
        .find(|(name, _)| *name == label)
        .map(|(_, count)| *count)
}

/// Model version for a researcher path.
pub fn model_version(path: &str) -> Option<&'static str> {
    match path {
        "quant" => Some(MODEL_QUANT),
        "factor" => Some(MODEL_FACTOR),
        "ml" => Some(MODEL_ML),
        "factor_backtest" => Some(MODEL_FACTOR_BACKTEST),
        _ => None,
    }
}

/// Feature version for a researcher path.
pub fn feature_version(path: &str) -> Option<&'static str> {
    match path {
        "quant" => Some(FEATURE_QUANT),
        "factor" => Some(FEATURE_FACTOR),
        "ml" => Some(FEATURE_ML),
        "factor_backtest" => Some(FEATURE_FACTOR_BACKTEST),
        _ => None,
    }
}

/// Role of a researcher path: production, shadow or diagnostic.
pub fn role_for(path: &str) -> Result<&'static str, SdError> {
    if PRODUCTION_PATHS.contains(&path) {
        Ok("production")
    } else if SHADOW_PATHS.contains(&path) {
        Ok("shadow")
    } else if DIAGNOSTIC_PATHS.contains(&path) {
        Ok("diagnostic")
    } else {
        Err(SdError::validation(format!(
            "unknown researcher path: {path}"
        )))
    }
}

/// The sampling anchor as a date.
pub fn sampling_anchor() -> NaiveDate {
    NaiveDate::from_ymd_opt(
        SAMPLING_ANCHOR_SESSION.0,
        SAMPLING_ANCHOR_SESSION.1,
        SAMPLING_ANCHOR_SESSION.2,
    )
    .expect("the frozen sampling anchor is a valid date")
}

/// Serialisable policy block written into requests, snapshots and reports.
///
/// The key order is part of the byte contract (`policy_metadata()` in Python is a
/// literal dict) so the map is built in that exact order.
pub fn policy_metadata() -> Value {
    let mut root = Map::new();
    root.insert(
        "policy_version".into(),
        Value::String(POLICY_VERSION.into()),
    );
    root.insert(
        "history_calendar_days".into(),
        Value::from(HISTORY_CALENDAR_DAYS),
    );
    root.insert(
        "horizon_sessions".into(),
        Value::from(FORECAST_HORIZON_SESSIONS),
    );
    root.insert(
        "horizon_label".into(),
        Value::String(FORECAST_HORIZON_LABEL.into()),
    );
    root.insert("window_closed".into(), Value::String(WINDOW_CLOSED.into()));
    root.insert(
        "target_version".into(),
        Value::String(TARGET_VERSION.into()),
    );

    let mut models = Map::new();
    for (name, version) in [
        ("quant", MODEL_QUANT),
        ("factor", MODEL_FACTOR),
        ("ml", MODEL_ML),
        ("factor_backtest", MODEL_FACTOR_BACKTEST),
    ] {
        models.insert(name.into(), Value::String(version.into()));
    }
    root.insert("model_versions".into(), Value::Object(models));

    let mut features = Map::new();
    for (name, version) in [
        ("quant", FEATURE_QUANT),
        ("factor", FEATURE_FACTOR),
        ("ml", FEATURE_ML),
        ("factor_backtest", FEATURE_FACTOR_BACKTEST),
    ] {
        features.insert(name.into(), Value::String(version.into()));
    }
    root.insert("feature_versions".into(), Value::Object(features));

    root.insert("production_paths".into(), strings(PRODUCTION_PATHS));
    root.insert("shadow_paths".into(), strings(SHADOW_PATHS));
    root.insert("diagnostic_paths".into(), strings(DIAGNOSTIC_PATHS));
    root.insert(
        "nonoverlap_gap_sessions".into(),
        Value::from(NONOVERLAP_GAP_SESSIONS),
    );
    root.insert(
        "min_valid_oos_nonoverlap".into(),
        Value::from(MIN_VALID_OOS_NONOVERLAP),
    );
    root.insert(
        "min_prospective_nonoverlap".into(),
        Value::from(MIN_PROSPECTIVE_NONOVERLAP),
    );
    root.insert("price_basis".into(), Value::String(PRICE_BASIS.into()));
    root.insert(
        "price_basis_source".into(),
        Value::String(PRICE_BASIS_SOURCE.into()),
    );

    let mut basis = Map::new();
    basis.insert("split_adjusted".into(), Value::Bool(true));
    basis.insert(
        "dividend_adjusted".into(),
        Value::Bool(TARGET_DIVIDEND_ADJUSTED),
    );
    basis.insert(
        "evidence_status".into(),
        Value::String("declared_by_provider_not_independently_verified".into()),
    );
    basis.insert(
        "dividend_flip_bias".into(),
        Value::String(
            "negative_on_dividend_payers; label_flip_rate_disclosed_not_corrected".into(),
        ),
    );
    root.insert("target_price_basis".into(), Value::Object(basis));

    let mut estimator = Map::new();
    estimator.insert(
        "type".into(),
        Value::String("causal_drift_shrinkage".into()),
    );
    estimator.insert(
        "version".into(),
        Value::String(DRIFT_SHRINKAGE_VERSION.into()),
    );
    estimator.insert("k".into(), Value::from(DRIFT_SHRINKAGE_K));
    estimator.insert("mu_annual".into(), Value::from(DRIFT_MU_ANNUAL));
    estimator.insert(
        "vol_window_sessions".into(),
        Value::from(DRIFT_VOL_WINDOW_SESSIONS),
    );
    estimator.insert(
        "max_history_sessions".into(),
        Value::from(DRIFT_VOL_WINDOW_SESSIONS),
    );
    estimator.insert(
        "roc_role".into(),
        Value::String(FEATURE_ROLE_IN_PROBABILITY.into()),
    );
    root.insert("estimator".into(), Value::Object(estimator));

    let mut floors = Map::new();
    floors.insert(
        "min_replay_effective_samples".into(),
        Value::from(MIN_REPLAY_EFFECTIVE_SAMPLES),
    );
    floors.insert(
        "min_replay_observations".into(),
        Value::from(MIN_REPLAY_OBSERVATIONS),
    );
    floors.insert(
        "min_prospective_nonoverlap".into(),
        Value::from(MIN_PROSPECTIVE_NONOVERLAP),
    );
    floors.insert("prospective_floor_retained".into(), Value::Bool(true));
    floors.insert(
        "primary_evidence".into(),
        Value::String("in_window_causal_prefix_replay_with_block_bootstrap".into()),
    );
    floors.insert(
        "independent_holdout".into(),
        Value::String("not_available_inside_a_single_52_week_window".into()),
    );
    floors.insert(
        "prospective_recording_grace_hours".into(),
        Value::from(PROSPECTIVE_RECORDING_GRACE_HOURS),
    );
    root.insert("evidence_floors".into(), Value::Object(floors));

    let mut coverage = Map::new();
    coverage.insert(
        "expected_sessions_source".into(),
        Value::String("resolved_exchange_calendar".into()),
    );
    coverage.insert(
        "observed_sessions_source".into(),
        Value::String("frozen_bars".into()),
    );
    coverage.insert("declaration_is_not_evidence".into(), Value::Bool(true));
    coverage.insert(
        "unverified_calendar_cannot_claim_complete".into(),
        Value::Bool(true),
    );
    root.insert("coverage_rule".into(), Value::Object(coverage));

    root.insert(
        "sampling_anchor_session".into(),
        Value::String(iso_date(sampling_anchor())),
    );
    root.insert("sampling_modulus".into(), Value::from(SAMPLING_MODULUS));
    root.insert(
        "sampling_role".into(),
        Value::String("throttle_only_not_qualification".into()),
    );
    root.insert(
        "calibration_policy".into(),
        Value::String("disabled_by_policy".into()),
    );
    Value::Object(root)
}

fn strings(values: &[&str]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|item| Value::String((*item).into()))
            .collect(),
    )
}

/// ISO date (`YYYY-MM-DD`).
pub fn iso_date(date: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", date.year(), date.month(), date.day())
}

/// Parse an ISO date, rejecting forms Python's `date.fromisoformat` rejects would
/// be too strict; the legacy runtime only ever produces extended dates.
pub fn parse_iso_date(text: &str) -> Result<NaiveDate, SdError> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map_err(|_| SdError::validation(format!("Invalid isoformat string: '{text}'")))
}

/// Forecast intent accepts exactly 5D; descriptive/factor research keeps range support.
pub fn validate_forecast_horizon(horizon: &str, intent: &str) -> Result<String, SdError> {
    let label = horizon.trim().to_uppercase();
    if intent == "forecast" && label != FORECAST_HORIZON_LABEL {
        return Err(SdError::policy(
            ERROR_UNSUPPORTED_HORIZON,
            format!("forecast horizon must be {FORECAST_HORIZON_LABEL} under {POLICY_VERSION}; got {label}"),
        ));
    }
    Ok(label)
}

/// Return `(window_start_exclusive, window_end_inclusive)` as ISO dates.
pub fn window_bounds(decision_session: NaiveDate) -> (String, String) {
    let start = decision_session - chrono::Duration::days(HISTORY_CALENDAR_DAYS);
    (iso_date(start), iso_date(decision_session))
}

/// True when `start_exclusive < session <= end_inclusive` (lexicographic ISO compare
/// is exact for `YYYY-MM-DD`).
pub fn window_contains(session: &str, start_exclusive: &str, end_inclusive: &str) -> bool {
    start_exclusive < session && session <= end_inclusive
}

/// Reject user-supplied windows that do not match the policy boundary.
pub fn validate_declared_window(
    declared_start: Option<&str>,
    declared_end: Option<&str>,
    decision_session: NaiveDate,
    allow_missing: bool,
) -> Result<(String, String), SdError> {
    let (start, end) = window_bounds(decision_session);
    if declared_start.is_none() && declared_end.is_none() {
        if allow_missing {
            return Ok((start, end));
        }
        return Err(SdError::policy(
            ERROR_FIXED_WINDOW_CONFLICT,
            "history window is required",
        ));
    }
    if let Some(value) = declared_start {
        if value != start {
            return Err(SdError::policy(
                ERROR_FIXED_WINDOW_CONFLICT,
                format!("history start {value} does not match policy boundary {start}"),
            ));
        }
    }
    if let Some(value) = declared_end {
        if value != end {
            return Err(SdError::policy(
                ERROR_FIXED_WINDOW_CONFLICT,
                format!("history end {value} does not match policy boundary {end}"),
            ));
        }
    }
    Ok((start, end))
}

/// Zero-based ordinal of `session` on the supplied calendar, anchored at
/// 2000-01-03. Returns `None` when the anchor is absent, the session is absent, or
/// the session precedes the anchor (so the phase is `unanchored`, never "not sampled").
pub fn session_ordinal(sessions: &[NaiveDate], session: NaiveDate) -> Option<i64> {
    let anchor = sampling_anchor();
    let anchor_index = sessions.iter().position(|item| *item == anchor)?;
    let index = sessions.iter().position(|item| *item == session)?;
    if index < anchor_index {
        return None;
    }
    Some((index - anchor_index) as i64)
}

/// True when the ordinal participates in the fixed sampling phase.
pub fn is_sampled_ordinal(ordinal: Option<i64>) -> bool {
    matches!(ordinal, Some(value) if value % i64::from(SAMPLING_MODULUS) == 0)
}

/// Sessions strictly after `start_exclusive` and at/before `end_inclusive`.
pub fn sessions_between<'a>(
    sessions: &'a [NaiveDate],
    start_exclusive: &str,
    end_inclusive: &str,
) -> Vec<&'a NaiveDate> {
    sessions
        .iter()
        .filter(|item| window_contains(&iso_date(**item), start_exclusive, end_inclusive))
        .collect()
}

/// Close-to-close target session, counted on the exchange calendar.
pub fn horizon_session(
    sessions: &[NaiveDate],
    decision: NaiveDate,
    horizon: u32,
) -> Option<NaiveDate> {
    let index = sessions.iter().position(|item| *item == decision)?;
    sessions.get(index + horizon as usize).copied()
}

/// Identity fields stamped on every prediction and ledger record (ordered).
pub fn model_identity(path: &str) -> Result<Value, SdError> {
    let (Some(model), Some(feature)) = (model_version(path), feature_version(path)) else {
        return Err(SdError::validation(format!(
            "unknown researcher path: {path}"
        )));
    };
    let mut map = Map::new();
    map.insert(
        "policy_version".into(),
        Value::String(POLICY_VERSION.into()),
    );
    map.insert("model_version".into(), Value::String(model.into()));
    map.insert("feature_version".into(), Value::String(feature.into()));
    map.insert(
        "target_version".into(),
        Value::String(TARGET_VERSION.into()),
    );
    Ok(Value::Object(map))
}

/// Ledger grouping key for a ticker/market/path.
pub fn ledger_group_key(ticker: &str, market: &str, path: &str) -> Result<String, SdError> {
    let (Some(model), Some(feature)) = (model_version(path), feature_version(path)) else {
        return Err(SdError::validation(format!(
            "unknown researcher path: {path}"
        )));
    };
    Ok([
        ticker.to_uppercase(),
        market.to_uppercase(),
        POLICY_VERSION.to_string(),
        model.to_string(),
        feature.to_string(),
        TARGET_VERSION.to_string(),
    ]
    .join("|"))
}

/// Human-readable frozen-window sentence used by reports.
pub fn describe_window(decision_session: &str) -> String {
    let (start, end) = match parse_iso_date(decision_session) {
        Ok(date) => window_bounds(date),
        Err(_) => (decision_session.to_string(), decision_session.to_string()),
    };
    format!("截至{end}的52周（{HISTORY_CALENDAR_DAYS}个自然日，窗口({start}, {end}]）")
}

/// Disclose the dividend label basis instead of pretending dividends are neutral.
pub fn dividend_label_disclosure() -> Value {
    let mut map = Map::new();
    map.insert(
        "target_version".into(),
        Value::String(TARGET_VERSION.into()),
    );
    map.insert("price_basis".into(), Value::String(PRICE_BASIS.into()));
    map.insert(
        "dividend_adjusted".into(),
        Value::Bool(TARGET_DIVIDEND_ADJUSTED),
    );
    map.insert("flip_rate".into(), Value::Null);
    map.insert(
        "status".into(),
        Value::String("declared_not_quantified".into()),
    );
    map.insert(
        "direction".into(),
        Value::String("systematically_negative_for_dividend_payers".into()),
    );
    map.insert(
        "reason".into(),
        Value::String("the frozen snapshot carries no dividend history, so label flips cannot be counted from it".into()),
    );
    Value::Object(map)
}
