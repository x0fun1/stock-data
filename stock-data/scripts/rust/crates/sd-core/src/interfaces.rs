//! Cross-layer interfaces frozen in R01 and extended by their owning packages.
//!
//! | interface | owner |
//! |---|---|
//! | `CalendarProvider` / `CalendarResource` | R03 (`sd-core::calendar`, `sd-storage::calendar_resource`) |
//! | `MarketView` / `VerifiedSnapshot` | R03 (`sd-storage::snapshot`) |
//! | `OriginPrediction` | R04 (`sd-research::quant`) |
//! | `AuditVerdict` | R05 (`sd-audit`) |
//! | `NewsCandidate` / `NewsAssessment` | R07 (`sd-news`) |
//! | `GatewayCall` / `GatewayResult` | R12 (`sd-providers`) |

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub use crate::calendar::{CalendarResource, CalendarSession};

/// Supplies verified exchange schedules. A provider that cannot name a session is
/// not allowed to approximate one; it returns `None` and the caller degrades.
pub trait CalendarProvider {
    /// Verified resource for a market (`US`/`HK`).
    fn resource(&self, market: &str) -> Option<&CalendarResource>;

    /// Sessions strictly after `start_exclusive` and at/before `end_inclusive`.
    fn sessions_in_window(
        &self,
        market: &str,
        start_exclusive: &str,
        end_inclusive: &str,
    ) -> Vec<NaiveDate> {
        self.resource(market)
            .map(|resource| {
                resource
                    .sessions_in_window(start_exclusive, end_inclusive)
                    .into_iter()
                    .filter_map(|session| session.parsed_date().ok())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// True when the schedule covers the whole window.
    fn covers(&self, market: &str, start_exclusive: &str, end_inclusive: &str) -> bool {
        self.resource(market)
            .map(|resource| resource.covers(start_exclusive, end_inclusive))
            .unwrap_or(false)
    }
}

/// One daily OHLCV observation as frozen in a snapshot.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Bar {
    pub date: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

/// Verified market view handed to the researchers: bars plus the provenance that
/// the gates checked. Researchers never read files or networks themselves.
#[derive(Debug, Clone, PartialEq)]
pub struct MarketView {
    pub ticker: String,
    pub market: String,
    pub horizon: String,
    pub asof_timestamp: String,
    pub bars: Vec<Bar>,
    pub calendar: Option<CalendarResource>,
    pub price_basis: Option<String>,
    pub adjustment: Option<String>,
    pub actual_source: Option<String>,
    pub currency: Option<String>,
    pub unit: Option<String>,
}

impl MarketView {
    /// Bar dates in order.
    pub fn bar_dates(&self) -> Vec<String> {
        self.bars.iter().map(|bar| bar.date.clone()).collect()
    }
}

/// Identity of a frozen snapshot (a subset of `manifest.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SnapshotIdentity {
    pub snapshot_id: String,
    pub ticker: String,
    pub market: String,
    pub horizon: String,
    pub asof_timestamp: String,
    pub schema_version: String,
    pub snapshot_content_sha256: String,
}

/// The v2 drift-shrinkage transform published with every probability.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProbabilityTransform {
    pub kind: String,
    pub version: String,
    pub k: u32,
    pub mu: f64,
    pub horizon_sessions: u32,
    pub n_eff: f64,
    pub base_rate_at_origin: f64,
    pub sigma: f64,
    pub p_vol: f64,
}

/// One origin's production prediction (the current day and every replay day use
/// the same code path).
#[derive(Debug, Clone, PartialEq)]
pub struct OriginPrediction {
    pub origin: NaiveDate,
    pub target_session: Option<NaiveDate>,
    pub probability: f64,
    pub raw_probability: Option<f64>,
    pub transform: ProbabilityTransform,
}

/// Independent audit verdict. `reproduced` is about arithmetic; `reason_codes`
/// carries the policy rejections.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AuditVerdict {
    pub accepted: bool,
    pub reproduced: Option<bool>,
    pub audited_value: Option<f64>,
    pub expected_value: Option<f64>,
    pub tolerance: f64,
    pub reason_codes: Vec<String>,
    pub details: Value,
}

/// A gateway invocation (function + validated parameters).
#[derive(Debug, Clone, PartialEq)]
pub struct GatewayCall {
    pub function: String,
    pub parameters: Value,
}

/// A gateway result before envelope construction.
#[derive(Debug, Clone, PartialEq)]
pub struct GatewayResult {
    pub source: String,
    pub payload: Value,
    pub record_count: Option<i64>,
}
