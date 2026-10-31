//! Verified exchange calendars, decision/target session resolution and coverage.
//!
//! A time zone is not a trading calendar. The runtime reads a versioned resource
//! generated from a pinned calendar library (R03) and never approximates sessions
//! from observed bars: an unverified calendar can only produce diagnostics.
//!
//! A-CAL-01: the calendar distinguishes three instants per session.
//!
//! * [`CalendarSession::close_at`] is the continuous-trading close.
//! * [`CalendarSession::auction_end_at`] is the latest official auction-completion
//!   boundary, or `None` when no closing-auction regime applies / the historical
//!   boundary is unverified.
//! * [`CalendarSession::complete_at`] is the earliest instant at which that session's
//!   daily bar can be confirmed complete. Only `complete_at` is used to resolve the
//!   latest complete close: a session without it is *skipped*, never offset by an
//!   assumed number of minutes.
//!
//! The boundary is sourced, not extrapolated (HKEX closing-auction latest end
//! 16:10 HKT normal / 12:10 HKT half day from 2016-07-25; no closing auction before
//! 2008-05-26 or between the 2009 suspension and 2016; the 2008-05-26..2009-03-20
//! historical auction boundary is unverified; the US closing auction concludes at
//! the core close). Provenance travels with the resource; a resource without
//! verifiable completion semantics can never produce `library_verified` evidence.
//!
//! Ports `data_checks._resolve_session_axis` (calendar part) and
//! `data_checks._recompute_window_coverage`, plus the session helpers from
//! `forecast_policy`.

use chrono::{DateTime, NaiveDate, Utc};
use serde_json::{Map, Value};

use crate::error::{SdError, SdResult};
use crate::policy;
use crate::versions::{CALENDAR_RESOURCE_SCHEMA_VERSION, READABLE_CALENDAR_RESOURCE_SCHEMAS};

/// Markets with no closing-auction extension beyond the continuous close.
///
/// The NYSE closing auction concludes at the core close (16:00 ET, or 13:00 ET on
/// an early close that is already folded into the continuous close), so for these
/// markets the continuous close is itself the sourced completion boundary. Every
/// other market may operate a closing auction whose completion is later than the
/// continuous close and therefore needs explicit completion metadata.
pub const NO_AUCTION_EXTENSION_MARKETS: [&str; 1] = ["US"];

/// True when the market can have an auction completion later than the continuous close.
pub fn market_has_auction_extension(market: &str) -> bool {
    !NO_AUCTION_EXTENSION_MARKETS.contains(&market)
}

/// One exchange session with its continuous close and completion boundaries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CalendarSession {
    /// Session date (`YYYY-MM-DD`).
    pub date: String,
    /// Continuous-trading close instant in UTC (`...Z`).
    pub close_at: String,
    /// Latest official auction-completion boundary in UTC, or `None` when no
    /// closing-auction regime applies or the historical boundary is unverified.
    pub auction_end_at: Option<String>,
    /// Earliest confirmable complete-daily-bar instant in UTC; `None` = unverified.
    pub complete_at: Option<String>,
}

impl CalendarSession {
    /// Parsed session date.
    pub fn parsed_date(&self) -> SdResult<NaiveDate> {
        policy::parse_iso_date(&self.date)
    }

    /// Parsed continuous-trading close instant.
    pub fn parsed_close(&self) -> SdResult<DateTime<Utc>> {
        crate::contracts::parse_timestamp_str(&self.close_at)
    }

    /// Parsed auction-completion boundary, when one is declared.
    pub fn parsed_auction_end(&self) -> SdResult<Option<DateTime<Utc>>> {
        self.auction_end_at
            .as_deref()
            .map(crate::contracts::parse_timestamp_str)
            .transpose()
    }

    /// Parsed confirmable-complete boundary, when one is declared.
    pub fn parsed_complete(&self) -> SdResult<Option<DateTime<Utc>>> {
        self.complete_at
            .as_deref()
            .map(crate::contracts::parse_timestamp_str)
            .transpose()
    }

    /// The completion boundary used to resolve the latest complete close.
    ///
    /// `None` means "unverified": callers must skip the session and report the
    /// unverified scope instead of assuming the continuous close.
    pub fn completion_boundary(&self) -> Option<DateTime<Utc>> {
        self.parsed_complete().ok().flatten()
    }
}

/// One official source backing a completion boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionSource {
    pub id: String,
    pub url: String,
    pub retrieved_at_utc: String,
    pub sha256: String,
}

/// One explicitly unverified applicability scope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnverifiedScope {
    pub id: String,
    pub markets: Vec<String>,
    pub from: Option<String>,
    pub to: Option<String>,
    pub affects: String,
    pub reason: String,
}

/// One market rule inside the completion provenance block.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionMarket {
    pub market: String,
    pub calendar: String,
    pub scheme: String,
    pub applies_from: Option<String>,
    pub applies_to: Option<String>,
}

/// Sourced completion provenance carried by a schema-2.0 resource.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionProvenance {
    pub rule_version: String,
    pub markets: Vec<CompletionMarket>,
    pub sources: Vec<CompletionSource>,
    pub unverified_scope: Vec<UnverifiedScope>,
    pub verified_session_count: usize,
    pub unverified_session_count: usize,
    raw: Value,
}

impl CompletionProvenance {
    /// The raw provenance block, so evidence can be re-emitted byte-stably.
    pub fn value(&self) -> &Value {
        &self.raw
    }

    /// True when one of the declared market rules covers `market`.
    pub fn covers_market(&self, market: &str) -> bool {
        self.markets.iter().any(|rule| rule.market == market)
    }

    /// True when the provenance leaves any session completion unverified.
    pub fn declares_unverified_sessions(&self) -> bool {
        self.unverified_session_count != 0
    }

    /// Parse and validate a completion provenance block.
    pub fn from_json(value: &Value) -> SdResult<Self> {
        let Value::Object(object) = value else {
            return Err(SdError::validation(
                "calendar completion provenance must be an object",
            ));
        };
        if object
            .get("rule_version")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .is_empty()
        {
            return Err(SdError::validation(
                "calendar completion provenance is missing rule_version",
            ));
        }
        let rule_version = object
            .get("rule_version")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let Some(market_rows) = object.get("markets").and_then(Value::as_array) else {
            return Err(SdError::validation(
                "calendar completion provenance is missing markets",
            ));
        };
        if market_rows.is_empty() {
            return Err(SdError::validation(
                "calendar completion provenance has no market rules",
            ));
        }
        let mut markets = Vec::with_capacity(market_rows.len());
        for row in market_rows {
            let market = row
                .get("market")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let calendar = row
                .get("calendar")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let scheme = row
                .get("scheme")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if market.is_empty() || calendar.is_empty() || scheme.is_empty() {
                return Err(SdError::validation(
                    "calendar completion market rule needs market, calendar and scheme",
                ));
            }
            let applies_from = optional_string(row.get("applies_from"))?;
            let applies_to = optional_string(row.get("applies_to"))?;
            markets.push(CompletionMarket {
                market,
                calendar,
                scheme,
                applies_from,
                applies_to,
            });
        }
        let Some(source_rows) = object.get("sources").and_then(Value::as_array) else {
            return Err(SdError::validation(
                "calendar completion provenance is missing sources",
            ));
        };
        if source_rows.is_empty() {
            return Err(SdError::validation(
                "calendar completion provenance has no official sources",
            ));
        }
        let mut sources = Vec::with_capacity(source_rows.len());
        for row in source_rows {
            let id = row
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let url = row
                .get("url")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let retrieved_at_utc = row
                .get("retrieved_at_utc")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let sha256 = row
                .get("sha256")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if id.is_empty()
                || !(url.starts_with("https://") || url.starts_with("http://"))
                || retrieved_at_utc.is_empty()
                || sha256.len() != 64
            {
                return Err(SdError::validation(
                    "calendar completion source needs id, http(s) url, retrieved_at_utc and sha256",
                ));
            }
            sources.push(CompletionSource {
                id,
                url,
                retrieved_at_utc,
                sha256,
            });
        }
        let mut unverified_scope = Vec::new();
        if let Some(rows) = object.get("unverified_scope").and_then(Value::as_array) {
            for row in rows {
                let id = row
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let affects = row
                    .get("affects")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let reason = row
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                if id.is_empty() || affects.is_empty() || reason.is_empty() {
                    return Err(SdError::validation(
                        "calendar unverified scope needs id, affects and reason",
                    ));
                }
                let markets = row
                    .get("markets")
                    .and_then(Value::as_array)
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default();
                unverified_scope.push(UnverifiedScope {
                    id,
                    markets,
                    from: optional_string(row.get("from"))?,
                    to: optional_string(row.get("to"))?,
                    affects,
                    reason,
                });
            }
        } else {
            return Err(SdError::validation(
                "calendar completion provenance is missing unverified_scope",
            ));
        }
        let verified_session_count = object
            .get("verified_session_count")
            .and_then(Value::as_u64)
            .unwrap_or(0) as usize;
        let unverified_session_count = object
            .get("unverified_session_count")
            .and_then(Value::as_u64)
            .unwrap_or(0) as usize;
        Ok(Self {
            rule_version,
            markets,
            sources,
            unverified_scope,
            verified_session_count,
            unverified_session_count,
            raw: value.clone(),
        })
    }
}

fn optional_string(value: Option<&Value>) -> SdResult<Option<String>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(SdError::validation(
            "calendar provenance string field has an invalid type",
        )),
    }
}

/// Parse an explicit-null-or-UTC-string session boundary from a schema-2.0 row.
fn parse_optional_utc(value: &Value, date: &str, field: &str) -> SdResult<Option<String>> {
    match value {
        Value::Null => Ok(None),
        Value::String(text) if text.ends_with('Z') => {
            crate::contracts::parse_timestamp_str(text)?;
            Ok(Some(text.clone()))
        }
        Value::String(_) => Err(SdError::validation(format!(
            "calendar session {date} {field} must be a UTC instant ending in Z"
        ))),
        _ => Err(SdError::validation(format!(
            "calendar session {date} {field} must be a UTC instant or null"
        ))),
    }
}

/// Parse an optional boundary from a frozen snapshot row.
///
/// An absent key or an explicit `null` both mean "no completion boundary is
/// declared"; a present instant must be UTC, fall on the session's local date and not
/// precede the continuous close.
fn optional_snapshot_instant(
    value: Option<&Value>,
    close_at: &str,
    day: NaiveDate,
    zone: &chrono_tz::Tz,
    field: &str,
) -> SdResult<Option<String>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => {
            let instant = crate::contracts::parse_timestamp_str(text)?;
            if instant.with_timezone(zone).date_naive() != day {
                return Err(SdError::validation(format!(
                    "session_calendar {field} does not fall on its local session date"
                )));
            }
            if instant < crate::contracts::parse_timestamp_str(close_at)? {
                return Err(SdError::validation(format!(
                    "session_calendar {field} precedes the continuous close"
                )));
            }
            Ok(Some(text.clone()))
        }
        Some(_) => Err(SdError::validation(format!(
            "session_calendar {field} must be a UTC instant or null"
        ))),
    }
}

/// How this resource establishes completion semantics.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompletionSemantics {
    /// Schema-2.0 completion provenance (official sources + unverified scope).
    Declared(CompletionProvenance),
    /// No metadata, but the market has no auction extension: the continuous close
    /// is the sourced completion boundary (US).
    NoAuctionExtensionRule,
    /// No metadata and the market can have an auction extension: unverified.
    Unverified { reason: String },
}

impl CompletionSemantics {
    /// True when the latest-complete-close resolution may be treated as verified.
    pub fn is_verified(&self) -> bool {
        matches!(self, Self::Declared(_) | Self::NoAuctionExtensionRule)
    }

    /// The declared provenance, when the resource carries it.
    pub fn provenance(&self) -> Option<&CompletionProvenance> {
        match self {
            Self::Declared(provenance) => Some(provenance),
            _ => None,
        }
    }

    /// Why completion semantics are unverified, when they are.
    pub fn unverified_reason(&self) -> Option<&str> {
        match self {
            Self::Unverified { reason } => Some(reason),
            _ => None,
        }
    }

    /// Stable label recorded in diagnostics/evidence.
    pub fn label(&self) -> &'static str {
        match self {
            Self::Declared(_) => "declared_schema_2_0_completion",
            Self::NoAuctionExtensionRule => "no_auction_extension_rule",
            Self::Unverified { .. } => "unverified",
        }
    }
}

/// A verified exchange schedule.
///
/// `source` is provenance, not decoration: a schedule whose source is not a real
/// exchange calendar can never make a forecast production-eligible.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CalendarResource {
    pub market: String,
    pub calendar: String,
    pub timezone: String,
    pub source: String,
    pub coverage_start: String,
    pub coverage_end: String,
    pub sessions: Vec<CalendarSession>,
    /// How the completion boundary was established.
    pub completion: CompletionSemantics,
}

impl CalendarResource {
    /// Parse and validate one resource document.
    ///
    /// Validation is strict: the schema label must be readable, sessions must be
    /// strictly increasing unique dates, every instant must be an explicit UTC `Z`
    /// timestamp, and the declared coverage must match the first/last session.
    ///
    /// Schema 2.0 additionally requires the sourced `completion` provenance and the
    /// per-session `auction_end_at` / `complete_at` fields (explicit `null` is
    /// meaningful: unverified). Schema 1.0 has only the continuous close; it is
    /// readable for diagnostics but can never establish verified completion, so a
    /// runtime must not treat it as `library_verified` evidence.
    ///
    /// A malformed resource is a configuration error, never a silent fallback to
    /// observed bars.
    pub fn from_json(value: &Value) -> SdResult<Self> {
        let Value::Object(object) = value else {
            return Err(SdError::validation("calendar resource must be an object"));
        };
        let schema = object
            .get("resource_schema")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !READABLE_CALENDAR_RESOURCE_SCHEMAS.contains(&schema) {
            return Err(SdError::validation(format!(
                "unsupported calendar resource schema {schema:?}; readable schemas are {READABLE_CALENDAR_RESOURCE_SCHEMAS:?}"
            )));
        }
        let legacy = schema != CALENDAR_RESOURCE_SCHEMA_VERSION;
        let text = |key: &str| -> SdResult<String> {
            object
                .get(key)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or_else(|| {
                    SdError::validation(format!("calendar resource is missing string field {key}"))
                })
        };
        let market = text("market")?;
        let calendar = text("calendar")?;
        let timezone = text("timezone")?;
        let source = text("source")?;
        let coverage_start = text("coverage_start")?;
        let coverage_end = text("coverage_end")?;
        let zone: chrono_tz::Tz = timezone
            .parse()
            .map_err(|_| SdError::validation(format!("unknown exchange timezone: {timezone}")))?;
        // Schema 2.0 must carry sourced completion semantics that cover this market.
        let completion = if legacy {
            CompletionSemantics::Unverified {
                reason: format!(
                    "calendar resource schema {schema} carries only the continuous close; \
                     completion semantics are unverified for the latest-complete-close resolver"
                ),
            }
        } else {
            let Some(block) = object.get("completion") else {
                return Err(SdError::validation(
                    "schema-2.0 calendar resource is missing the completion provenance block",
                ));
            };
            let provenance = CompletionProvenance::from_json(block)?;
            if !provenance.covers_market(&market) {
                return Err(SdError::validation(format!(
                    "calendar completion provenance has no rule for market {market}"
                )));
            }
            if provenance.verified_session_count == 0 {
                return Err(SdError::validation(
                    "calendar completion provenance declares no verified sessions",
                ));
            }
            CompletionSemantics::Declared(provenance)
        };
        let Some(rows) = object.get("sessions").and_then(Value::as_array) else {
            return Err(SdError::validation(
                "calendar resource is missing the sessions array",
            ));
        };
        let mut sessions = Vec::with_capacity(rows.len());
        let mut previous: Option<NaiveDate> = None;
        let mut session_verified = 0usize;
        let mut session_unverified = 0usize;
        for row in rows {
            let Some(date) = row.get("date").and_then(Value::as_str) else {
                return Err(SdError::validation("calendar session is missing its date"));
            };
            let Some(close_at) = row.get("close_at").and_then(Value::as_str) else {
                return Err(SdError::validation(format!(
                    "calendar session {date} is missing close_at"
                )));
            };
            let parsed_date = policy::parse_iso_date(date)?;
            // Reject anything that is not an explicit UTC instant: a local-time close
            // would silently change the decision session across DST boundaries.
            if !close_at.ends_with('Z') {
                return Err(SdError::validation(format!(
                    "calendar session {date} close_at must be a UTC instant ending in Z"
                )));
            }
            let parsed_close = crate::contracts::parse_timestamp_str(close_at)?;
            if parsed_close.with_timezone(&zone).date_naive() != parsed_date {
                return Err(SdError::validation(format!(
                    "calendar session {date} close_at does not fall on its local session date"
                )));
            }
            let (auction_end_at, complete_at) = if legacy {
                // Schema 1.0 has no completion metadata. A market with no auction
                // extension still has a sourced completion boundary (the continuous
                // close); every other market's completion stays unknown.
                if market_has_auction_extension(&market) {
                    (None, None)
                } else {
                    (Some(close_at.to_string()), Some(close_at.to_string()))
                }
            } else {
                let auction = row.get("auction_end_at").ok_or_else(|| {
                    SdError::validation(format!(
                        "schema-2.0 calendar session {date} is missing auction_end_at (explicit null is required)"
                    ))
                })?;
                let complete = row.get("complete_at").ok_or_else(|| {
                    SdError::validation(format!(
                        "schema-2.0 calendar session {date} is missing complete_at (explicit null is required)"
                    ))
                })?;
                let auction_end_at = parse_optional_utc(auction, date, "auction_end_at")?;
                let complete_at = parse_optional_utc(complete, date, "complete_at")?;
                if let Some(value) = &auction_end_at {
                    let instant = crate::contracts::parse_timestamp_str(value)?;
                    if instant.with_timezone(&zone).date_naive() != parsed_date {
                        return Err(SdError::validation(format!(
                            "calendar session {date} auction_end_at does not fall on its local session date"
                        )));
                    }
                    if instant < parsed_close {
                        return Err(SdError::validation(format!(
                            "calendar session {date} auction_end_at precedes its continuous close"
                        )));
                    }
                }
                if let Some(value) = &complete_at {
                    let instant = crate::contracts::parse_timestamp_str(value)?;
                    if instant.with_timezone(&zone).date_naive() != parsed_date {
                        return Err(SdError::validation(format!(
                            "calendar session {date} complete_at does not fall on its local session date"
                        )));
                    }
                    if instant < parsed_close {
                        return Err(SdError::validation(format!(
                            "calendar session {date} complete_at precedes its continuous close"
                        )));
                    }
                    if let Some(auction) = &auction_end_at {
                        if instant < crate::contracts::parse_timestamp_str(auction)? {
                            return Err(SdError::validation(format!(
                                "calendar session {date} complete_at precedes its auction_end_at"
                            )));
                        }
                    }
                }
                (auction_end_at, complete_at)
            };
            if complete_at.is_some() {
                session_verified += 1;
            } else {
                session_unverified += 1;
            }
            if let Some(previous) = previous {
                if parsed_date <= previous {
                    return Err(SdError::validation(format!(
                        "calendar sessions must be strictly increasing; {date} follows {}",
                        policy::iso_date(previous)
                    )));
                }
            }
            previous = Some(parsed_date);
            sessions.push(CalendarSession {
                date: date.to_string(),
                close_at: close_at.to_string(),
                auction_end_at,
                complete_at,
            });
        }
        let Some(first) = sessions.first() else {
            return Err(SdError::validation("calendar resource has no sessions"));
        };
        let last = sessions.last().expect("non-empty");
        if first.date != coverage_start || last.date != coverage_end {
            return Err(SdError::validation(format!(
                "calendar coverage {}..{} does not match its sessions {}..{}",
                coverage_start, coverage_end, first.date, last.date
            )));
        }
        // The declared provenance counts must match the rows: a resource cannot claim
        // more verified sessions than it carries.
        if let CompletionSemantics::Declared(provenance) = &completion {
            if provenance.verified_session_count != session_verified
                || provenance.unverified_session_count != session_unverified
            {
                return Err(SdError::validation(format!(
                    "calendar completion counts ({} verified / {} unverified) do not match its sessions ({} / {})",
                    provenance.verified_session_count,
                    provenance.unverified_session_count,
                    session_verified,
                    session_unverified
                )));
            }
        }
        Ok(Self {
            market,
            calendar,
            timezone,
            source,
            coverage_start,
            coverage_end,
            sessions,
            completion,
        })
    }

    /// Parse a `session_calendar` block carried inside a frozen snapshot/market domain.
    ///
    /// The frozen block uses `{source, timezone, coverage_start, coverage_end,
    /// sessions}` and close instants may carry an explicit `+00:00` offset.
    /// Validation mirrors `data_checks.calendar_sessions`: the timezone must be the
    /// market's exchange zone, coverage must be ordered, every session must be unique,
    /// ordered and inside coverage, and each close instant must fall on its own local
    /// session date.
    ///
    /// Schema-2.0 provenance (`completion`) and per-session `auction_end_at` /
    /// `complete_at` are optional so frozen snapshots created before A-CAL-01 stay
    /// readable without rewriting their bytes:
    ///
    /// * a completion block makes the block self-describing and verified;
    /// * without one, a market with no auction extension (US) still resolves the
    ///   completion boundary as the continuous close by the sourced rule;
    /// * without one, a market with a possible auction extension (HK) is
    ///   [`CompletionSemantics::Unverified`] and its sessions carry no completion
    ///   boundary, so they are skipped instead of being offset by an assumed amount.
    pub fn from_snapshot_calendar(value: &Value, market: &str) -> SdResult<Self> {
        let Value::Object(object) = value else {
            return Err(SdError::validation(
                "source-backed session_calendar is unavailable",
            ));
        };
        let Some(source) = object
            .get("source")
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
        else {
            return Err(SdError::validation(
                "source-backed session_calendar is unavailable",
            ));
        };
        let expected_zone = policy::exchange_timezone(market)
            .ok_or_else(|| SdError::validation(format!("unsupported market {market}")))?;
        let timezone = object
            .get("timezone")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if timezone != expected_zone {
            return Err(SdError::validation(
                "session_calendar timezone does not match the requested market",
            ));
        }
        let coverage_start = object
            .get("coverage_start")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let coverage_end = object
            .get("coverage_end")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let start = policy::parse_iso_date(coverage_start)?;
        let end = policy::parse_iso_date(coverage_end)?;
        if start > end {
            return Err(SdError::validation("session_calendar coverage is reversed"));
        }
        let Some(rows) = object
            .get("sessions")
            .and_then(Value::as_array)
            .filter(|rows| !rows.is_empty())
        else {
            return Err(SdError::validation(
                "session_calendar.sessions must be non-empty",
            ));
        };
        let completion = match object.get("completion") {
            Some(block) => {
                let provenance = CompletionProvenance::from_json(block)?;
                if !provenance.covers_market(market) {
                    return Err(SdError::validation(
                        "session_calendar completion provenance does not cover the requested market",
                    ));
                }
                CompletionSemantics::Declared(provenance)
            }
            None if market_has_auction_extension(market) => CompletionSemantics::Unverified {
                reason: "frozen session_calendar carries no completion metadata and this market can have an auction completion later than the continuous close".to_string(),
            },
            None => CompletionSemantics::NoAuctionExtensionRule,
        };
        let no_extension = matches!(completion, CompletionSemantics::NoAuctionExtensionRule);
        let zone: chrono_tz::Tz = timezone
            .parse()
            .map_err(|_| SdError::validation(format!("unknown exchange timezone: {timezone}")))?;
        let mut sessions = Vec::with_capacity(rows.len());
        let mut previous: Option<NaiveDate> = None;
        for row in rows {
            let Some(date) = row.get("date").and_then(Value::as_str) else {
                return Err(SdError::validation(
                    "session_calendar session is missing its date",
                ));
            };
            let Some(close_at) = row.get("close_at").and_then(Value::as_str) else {
                return Err(SdError::validation(
                    "session_calendar session is missing close_at",
                ));
            };
            let day = policy::parse_iso_date(date)?;
            let close = crate::contracts::parse_timestamp_str(close_at)?;
            let local_day = close.with_timezone(&zone).date_naive();
            let ordered = previous.map(|previous| day > previous).unwrap_or(true);
            if local_day != day || day < start || day > end || !ordered {
                return Err(SdError::validation(
                    "session_calendar dates/close timestamps must be ordered, unique and within coverage",
                ));
            }
            // Absent keys mean a pre-A-CAL-01 frozen block; explicit null means the
            // producer marked the boundary unverified. Neither is silently offset.
            let auction_absent = row.get("auction_end_at").is_none();
            let auction_end_at = match optional_snapshot_instant(
                row.get("auction_end_at"),
                close_at,
                day,
                &zone,
                "auction_end_at",
            )? {
                Some(value) => Some(value),
                None if no_extension && auction_absent => Some(close_at.to_string()),
                None => None,
            };
            let complete_absent = row.get("complete_at").is_none();
            let complete_at = match optional_snapshot_instant(
                row.get("complete_at"),
                close_at,
                day,
                &zone,
                "complete_at",
            )? {
                Some(value) => Some(value),
                None if no_extension && complete_absent => Some(close_at.to_string()),
                None => None,
            };
            if let (Some(complete), Some(auction)) = (&complete_at, &auction_end_at) {
                if crate::contracts::parse_timestamp_str(complete)?
                    < crate::contracts::parse_timestamp_str(auction)?
                {
                    return Err(SdError::validation(format!(
                        "session_calendar {date} complete_at precedes its auction_end_at"
                    )));
                }
            }
            previous = Some(day);
            sessions.push(CalendarSession {
                date: date.to_string(),
                close_at: close_at.to_string(),
                auction_end_at,
                complete_at,
            });
        }
        Ok(Self {
            market: market.to_string(),
            calendar: match market {
                "US" => "XNYS".to_string(),
                "HK" => "XHKG".to_string(),
                other => other.to_string(),
            },
            timezone: timezone.to_string(),
            source: source.to_string(),
            coverage_start: coverage_start.to_string(),
            coverage_end: coverage_end.to_string(),
            sessions,
            completion,
        })
    }

    /// Session dates in order.
    pub fn session_dates(&self) -> Vec<NaiveDate> {
        self.sessions
            .iter()
            .filter_map(|session| session.parsed_date().ok())
            .collect()
    }

    /// True when this resource establishes a sourced/verified completion boundary.
    pub fn completion_is_verified(&self) -> bool {
        self.completion.is_verified()
    }

    /// The explicit unverified reason, when completion semantics are unverified.
    pub fn completion_unverified_reason(&self) -> Option<&str> {
        self.completion.unverified_reason()
    }

    /// Sessions whose completion boundary is unknown (skipped by resolution).
    pub fn sessions_without_completion(&self) -> Vec<&CalendarSession> {
        self.sessions
            .iter()
            .filter(|session| session.complete_at.is_none())
            .collect()
    }

    /// The latest session whose *confirmable-complete* boundary is at or before `asof`.
    ///
    /// A session without a completion boundary is skipped, never approximated from its
    /// continuous close: an as-of between the continuous close and the auction
    /// completion therefore resolves to the previous complete session. A completion
    /// instant exactly equal to the as-of counts as completed.
    pub fn decision_session(&self, asof: DateTime<Utc>) -> Option<NaiveDate> {
        let mut decision = None;
        for session in &self.sessions {
            let Some(complete) = session.completion_boundary() else {
                continue;
            };
            if complete <= asof {
                decision = session.parsed_date().ok();
            } else {
                break;
            }
        }
        decision
    }

    /// Sessions whose completion boundary is known and at or before `asof`, in order.
    ///
    /// Sessions with an unverified completion boundary are excluded: they cannot be
    /// used as confirmed closed sessions. Sessions after the first still-future
    /// boundary are excluded as well (completion instants are ordered with dates).
    pub fn closed_sessions(&self, asof: DateTime<Utc>) -> Vec<&CalendarSession> {
        let mut closed = Vec::new();
        for session in &self.sessions {
            match session.completion_boundary() {
                Some(complete) if complete <= asof => closed.push(session),
                Some(_) => break,
                None => continue,
            }
        }
        closed
    }

    /// The `horizon`-th session strictly after `decision`.
    pub fn target_session(&self, decision: NaiveDate, horizon: u32) -> Option<NaiveDate> {
        let dates = self.session_dates();
        policy::horizon_session(&dates, decision, horizon)
    }

    /// True when the resource covers `(start_exclusive, end_inclusive]`.
    pub fn covers(&self, start_exclusive: &str, end_inclusive: &str) -> bool {
        self.coverage_start.as_str() <= start_exclusive
            && self.coverage_end.as_str() >= end_inclusive
    }

    /// True when the sampling anchor is inside the covered range.
    pub fn anchors_phase(&self) -> bool {
        self.sessions
            .iter()
            .any(|session| session.date == policy::iso_date(policy::sampling_anchor()))
    }

    /// Session dates strictly after `start_exclusive` and at/before `end_inclusive`.
    pub fn sessions_in_window(
        &self,
        start_exclusive: &str,
        end_inclusive: &str,
    ) -> Vec<&CalendarSession> {
        self.sessions
            .iter()
            .filter(|session| {
                policy::window_contains(&session.date, start_exclusive, end_inclusive)
            })
            .collect()
    }

    /// Anchored sampling ordinal, or `None` when the anchor/session is unavailable.
    pub fn session_ordinal(&self, date: NaiveDate) -> Option<i64> {
        policy::session_ordinal(&self.session_dates(), date)
    }

    /// Three-state sampling: `Some(v)` when the phase is anchored, `None` when not.
    pub fn is_sampled(&self, date: NaiveDate) -> Option<bool> {
        self.session_ordinal(date)
            .map(|ordinal| policy::is_sampled_ordinal(Some(ordinal)))
    }
}

/// Bars-versus-calendar axis verdict (`_resolve_session_axis`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AxisVerdict {
    /// Session axis status label: `exchange_sessions` or `estimated_observed_sessions`.
    pub calendar_status: String,
    /// `LATEST_OBSERVATION_UNRESOLVED` when the last bar is not the latest closed session.
    pub latest_observation_unresolved: Option<String>,
    /// `SEVERE_MISSING_BARS` when the bar axis differs from the resolved session axis.
    pub missing_sessions: Option<String>,
    /// Number of closed sessions used as the expected axis.
    pub closed_session_count: usize,
    /// True when the resource establishes verified completion semantics.
    pub completion_verified: bool,
    /// Sessions in the resource whose completion boundary is unverified.
    pub unverified_completion_sessions: usize,
}

/// Compare the frozen bar axis against the resolved session axis.
pub fn resolve_bar_axis(
    resource: &CalendarResource,
    asof: DateTime<Utc>,
    bars_dates: &[String],
) -> SdResult<AxisVerdict> {
    let Some(first) = bars_dates.first() else {
        return Err(SdError::validation("no closed OHLCV bars"));
    };
    let last = bars_dates.last().expect("non-empty");
    let closed = resource.closed_sessions(asof);
    let completion_verified = resource.completion_is_verified();
    let unverified_completion_sessions = resource.sessions_without_completion().len();
    let Some(latest) = closed.last() else {
        return Ok(AxisVerdict {
            calendar_status: "exchange_sessions".to_string(),
            latest_observation_unresolved: Some(
                "latest bar does not match the latest closed exchange session None".to_string(),
            ),
            missing_sessions: None,
            closed_session_count: 0,
            completion_verified,
            unverified_completion_sessions,
        });
    };
    let mut verdict = AxisVerdict {
        calendar_status: "exchange_sessions".to_string(),
        latest_observation_unresolved: None,
        missing_sessions: None,
        closed_session_count: closed.len(),
        completion_verified,
        unverified_completion_sessions,
    };
    if &latest.date != last {
        verdict.latest_observation_unresolved = Some(format!(
            "latest bar {last} does not match the latest closed exchange session {}",
            latest.date
        ));
    }
    let expected_axis: Vec<String> = closed
        .iter()
        .map(|session| session.date.clone())
        .filter(|date| date.as_str() >= first.as_str() && date.as_str() <= last.as_str())
        .collect();
    if expected_axis != bars_dates {
        verdict.missing_sessions = Some(
            "daily bars do not match the resolved exchange session axis; confirmed sessions are missing".to_string(),
        );
    }
    Ok(verdict)
}

/// Recompute the 52-week window coverage from the calendar and frozen bars.
///
/// The declared coverage is a producer claim and never evidence: a receipt whose
/// bars start months after the boundary can still claim `complete`.
#[derive(Debug, Clone, PartialEq)]
pub struct CoverageRecompute {
    pub expected_sessions: usize,
    pub observed_sessions: usize,
    pub missing_sessions: Vec<String>,
    pub missing_session_count: usize,
    pub unexpected_sessions: Vec<String>,
    pub duplicate_sessions: Vec<String>,
    pub calendar_covers_window: bool,
    pub axis_matches_calendar: bool,
    pub complete: bool,
    pub declaration_mismatches: Vec<String>,
}

/// Recompute window coverage.
pub fn recompute_window_coverage(
    resource: &CalendarResource,
    bars_dates: &[String],
    start_exclusive: &str,
    end_inclusive: &str,
    declared: &Value,
) -> CoverageRecompute {
    let expected: Vec<String> = resource
        .sessions_in_window(start_exclusive, end_inclusive)
        .into_iter()
        .map(|session| session.date.clone())
        .collect();
    let observed: Vec<String> = bars_dates.to_vec();
    let expected_set: std::collections::BTreeSet<&String> = expected.iter().collect();
    let observed_set: std::collections::BTreeSet<&String> = observed.iter().collect();
    let mut duplicates: Vec<String> = Vec::new();
    for (index, day) in observed.iter().enumerate() {
        if observed.iter().skip(index + 1).any(|other| other == day) && !duplicates.contains(day) {
            duplicates.push(day.clone());
        }
    }
    duplicates.sort();
    let missing: Vec<String> = expected
        .iter()
        .filter(|day| !observed_set.contains(*day))
        .cloned()
        .collect();
    let unexpected: Vec<String> = observed
        .iter()
        .filter(|day| !expected_set.contains(*day))
        .cloned()
        .collect();
    let calendar_covers_window = resource.coverage_start.as_str() <= start_exclusive
        && resource.coverage_end.as_str() >= end_inclusive;
    let axis_matches_calendar = observed == expected;
    let complete = calendar_covers_window
        && !expected.is_empty()
        && missing.is_empty()
        && unexpected.is_empty()
        && duplicates.is_empty()
        && axis_matches_calendar;
    let mut mismatches = Vec::new();
    for (label, declared_value, actual) in [
        (
            "expected_sessions",
            declared.get("expected_sessions").and_then(Value::as_i64),
            expected.len(),
        ),
        (
            "observed_sessions",
            declared.get("observed_sessions").and_then(Value::as_i64),
            observed.len(),
        ),
        (
            "missing_session_count",
            declared
                .get("missing_session_count")
                .and_then(Value::as_i64),
            missing.len(),
        ),
    ] {
        if let Some(declared_value) = declared_value {
            if declared_value != actual as i64 {
                mismatches.push(format!(
                    "{label}: declared {declared_value}, recomputed {actual}"
                ));
            }
        }
    }
    let missing_session_count = missing.len();
    CoverageRecompute {
        expected_sessions: expected.len(),
        observed_sessions: observed.len(),
        missing_sessions: missing,
        missing_session_count,
        unexpected_sessions: unexpected,
        duplicate_sessions: duplicates,
        calendar_covers_window,
        axis_matches_calendar,
        complete,
        declaration_mismatches: mismatches,
    }
}

/// Recompute window coverage from an explicit session-date axis.
///
/// Used when the caller already holds the resolved session axis (a frozen or
/// library schedule) instead of a [`CalendarResource`] with provenance.
pub fn recompute_window_coverage_from_dates(
    session_dates: &[NaiveDate],
    observed: &[String],
    start_exclusive: &str,
    end_inclusive: &str,
    declared: &serde_json::Map<String, serde_json::Value>,
) -> CoverageRecompute {
    let expected: Vec<String> = session_dates
        .iter()
        .map(|date| policy::iso_date(*date))
        .filter(|date| policy::window_contains(date, start_exclusive, end_inclusive))
        .collect();
    let expected_set: std::collections::BTreeSet<&String> = expected.iter().collect();
    let observed_set: std::collections::BTreeSet<&String> = observed.iter().collect();
    let mut duplicates: Vec<String> = Vec::new();
    for (index, day) in observed.iter().enumerate() {
        if observed.iter().skip(index + 1).any(|other| other == day) && !duplicates.contains(day) {
            duplicates.push(day.clone());
        }
    }
    duplicates.sort();
    let missing: Vec<String> = expected
        .iter()
        .filter(|day| !observed_set.contains(*day))
        .cloned()
        .collect();
    let unexpected: Vec<String> = observed
        .iter()
        .filter(|day| !expected_set.contains(*day))
        .cloned()
        .collect();
    let calendar_covers_window = session_dates
        .first()
        .map(|first| policy::iso_date(*first).as_str() <= start_exclusive)
        .unwrap_or(false)
        && session_dates
            .last()
            .map(|last| policy::iso_date(*last).as_str() >= end_inclusive)
            .unwrap_or(false);
    let axis_matches_calendar = observed == expected.as_slice();
    let complete = calendar_covers_window
        && !expected.is_empty()
        && missing.is_empty()
        && unexpected.is_empty()
        && duplicates.is_empty()
        && axis_matches_calendar;
    let mut mismatches = Vec::new();
    for (label, declared_value, actual) in [
        (
            "expected_sessions",
            declared
                .get("expected_sessions")
                .and_then(|value| value.as_i64()),
            expected.len(),
        ),
        (
            "observed_sessions",
            declared
                .get("observed_sessions")
                .and_then(|value| value.as_i64()),
            observed.len(),
        ),
        (
            "missing_session_count",
            declared
                .get("missing_session_count")
                .and_then(|value| value.as_i64()),
            missing.len(),
        ),
    ] {
        if let Some(declared_value) = declared_value {
            if declared_value != actual as i64 {
                mismatches.push(format!(
                    "{label}: declared {declared_value}, recomputed {actual}"
                ));
            }
        }
    }
    let missing_session_count = missing.len();
    CoverageRecompute {
        expected_sessions: expected.len(),
        observed_sessions: observed.len(),
        missing_sessions: missing,
        missing_session_count,
        unexpected_sessions: unexpected,
        duplicate_sessions: duplicates,
        calendar_covers_window,
        axis_matches_calendar,
        complete,
        declaration_mismatches: mismatches,
    }
}

/// Serialisable coverage block in the legacy key order (`_recompute_window_coverage`).
pub fn coverage_value(coverage: &CoverageRecompute) -> Value {
    let mut map = Map::new();
    map.insert(
        "expected_sessions".into(),
        Value::from(coverage.expected_sessions),
    );
    map.insert(
        "observed_sessions".into(),
        Value::from(coverage.observed_sessions),
    );
    map.insert(
        "missing_sessions".into(),
        Value::Array(
            coverage
                .missing_sessions
                .iter()
                .map(|day| Value::String(day.clone()))
                .collect(),
        ),
    );
    map.insert(
        "missing_session_count".into(),
        Value::from(coverage.missing_session_count),
    );
    map.insert(
        "unexpected_sessions".into(),
        Value::Array(
            coverage
                .unexpected_sessions
                .iter()
                .map(|day| Value::String(day.clone()))
                .collect(),
        ),
    );
    map.insert(
        "duplicate_sessions".into(),
        Value::Array(
            coverage
                .duplicate_sessions
                .iter()
                .map(|day| Value::String(day.clone()))
                .collect(),
        ),
    );
    map.insert(
        "calendar_covers_window".into(),
        Value::Bool(coverage.calendar_covers_window),
    );
    map.insert(
        "axis_matches_calendar".into(),
        Value::Bool(coverage.axis_matches_calendar),
    );
    map.insert("complete".into(), Value::Bool(coverage.complete));
    map.insert(
        "declaration_mismatches".into(),
        Value::Array(
            coverage
                .declaration_mismatches
                .iter()
                .map(|item| Value::String(item.clone()))
                .collect(),
        ),
    );
    Value::Object(map)
}
