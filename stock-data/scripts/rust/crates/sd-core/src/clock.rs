//! Injected clocks.
//!
//! Every artifact that records a timestamp takes its clock from here so tests can
//! freeze time. The formatting helper reproduces Python's
//! `datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")`, which is the
//! exact on-disk form the legacy runtime writes (`2026-10-30T16:24:56.294805Z`).

use chrono::{DateTime, SecondsFormat, Utc};

/// Wall clock abstraction.
pub trait Clock: Send + Sync {
    /// Current instant.
    fn now(&self) -> DateTime<Utc>;

    /// Current instant formatted the way the legacy runtime writes timestamps.
    fn utc_now(&self) -> String {
        format_utc_timestamp(self.now())
    }
}

/// Real system clock.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> DateTime<Utc> {
        Utc::now()
    }
}

/// A clock frozen at one instant (fixtures, tests, `--captured-at-utc`).
#[derive(Debug, Clone, Copy)]
pub struct FixedClock {
    instant: DateTime<Utc>,
}

impl FixedClock {
    /// Freeze at an instant.
    pub fn new(instant: DateTime<Utc>) -> Self {
        Self { instant }
    }

    /// Freeze at a Python-style timestamp string (`...Z` and offsets accepted).
    pub fn from_timestamp(value: &str) -> Result<Self, crate::error::SdError> {
        Ok(Self::new(crate::contracts::parse_timestamp_str(value)?))
    }
}

impl Clock for FixedClock {
    fn now(&self) -> DateTime<Utc> {
        self.instant
    }
}

/// Format an instant exactly like Python's
/// `datetime.isoformat().replace("+00:00", "Z")`.
///
/// Python omits the fractional part when the microsecond field is zero and
/// otherwise prints exactly six digits. Nanoseconds beyond microseconds are
/// truncated, matching `fromisoformat`'s precision.
pub fn format_utc_timestamp(instant: DateTime<Utc>) -> String {
    let nanos = instant.timestamp_subsec_nanos();
    let micros = nanos / 1_000;
    if micros == 0 {
        instant.to_rfc3339_opts(SecondsFormat::Secs, true)
    } else {
        let base = instant.to_rfc3339_opts(SecondsFormat::Micros, true);
        // `SecondsFormat::Micros` already prints six digits; normalise a `+00:00`
        // suffix to `Z` for the rare backend that does not.
        base.replace("+00:00", "Z")
    }
}
