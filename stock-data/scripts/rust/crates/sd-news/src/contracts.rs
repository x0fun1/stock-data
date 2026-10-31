//! Restricted input contract for News/Sentiment analysts (`news/contracts.py`).
//!
//! Deliberately contains no Quant result: the News layer may not see the production
//! probability, directional edge or eligibility, so it cannot rationalise one.

use serde_json::{Map, Value};

/// Inputs visible to the News layer.
#[derive(Debug, Clone)]
pub struct NewsInput {
    /// Ticker symbol.
    pub ticker: String,
    /// Market code (`US` or `CN`).
    pub market: String,
    /// Horizon label (`5D`).
    pub horizon: String,
    /// Decision timestamp the run is anchored on.
    pub asof_timestamp: String,
    /// Frozen snapshot identity.
    pub snapshot_id: String,
    /// Three-state News status (`complete`, `partial`, `not_assessed`).
    pub news_status: String,
    /// Source-declared sentiment block, if the source supplied one.
    pub source_sentiment: Option<Value>,
    /// Point-in-time articles.
    pub articles: Vec<Value>,
    /// Market bars available at the decision.
    pub bars: Vec<Value>,
    /// Upstream warnings carried through.
    pub warnings: Vec<String>,
    /// Source coverage disclosure.
    pub coverage: Map<String, Value>,
}

impl NewsInput {
    /// Numeric session count carried by the horizon label (`5D` -> 5).
    pub fn horizon_sessions(&self) -> i64 {
        let digits: String = self
            .horizon
            .chars()
            .filter(|character| character.is_ascii_digit())
            .collect();
        digits.parse::<i64>().unwrap_or(5)
    }
}
