//! Research experiment entry points (R11).
//!
//! Each experiment re-runs a frozen protocol over provided data and returns a
//! deterministic report; nothing here changes the production path.

pub mod momentum_ab;
pub mod research_b_ab;
pub mod research_candidates;
pub mod selection_ab;
