//! One definition of "the News layer was assessed".
//!
//! The three states are distinct and none may be collapsed into another:
//! `complete` (assessed, no warnings), `partial` (assessed with disclosed warnings),
//! `not_assessed` (no evidence produced). Only the last may be described as
//! unassessed; unknown values fail closed.

/// Assessed with no disclosed warnings.
pub const NEWS_STATUS_COMPLETE: &str = "complete";
/// Assessed with one or more disclosed warnings.
pub const NEWS_STATUS_PARTIAL: &str = "partial";
/// No News evidence was produced.
pub const NEWS_STATUS_NOT_ASSESSED: &str = "not_assessed";

/// Legacy aliases still written by older artifacts; `valid` meant `complete`.
pub const ASSESSED_NEWS_STATUSES: [&str; 3] = ["complete", "valid", "partial"];
/// Statuses that must never be described as assessed.
pub const UNASSESSED_NEWS_STATUSES: [&str; 8] = [
    "not_assessed",
    "unavailable",
    "failed",
    "invalid",
    "empty",
    "blocked",
    "skipped",
    "not_applicable",
];

/// Return true exactly when the News layer produced assessed evidence.
///
/// Unknown values fail closed, so a corrupted artifact can never be described as
/// evaluated.
pub fn news_assessed(status: Option<&str>) -> bool {
    match status {
        Some(value) => ASSESSED_NEWS_STATUSES.contains(&value.trim().to_lowercase().as_str()),
        None => false,
    }
}

/// Map an artifact status onto the three disclosed states.
pub fn news_status_label(status: Option<&str>) -> &'static str {
    let value = status.unwrap_or("").trim().to_lowercase();
    if !news_assessed(Some(&value)) {
        return NEWS_STATUS_NOT_ASSESSED;
    }
    if value == "complete" || value == "valid" {
        NEWS_STATUS_COMPLETE
    } else {
        NEWS_STATUS_PARTIAL
    }
}
