//! Offline selfcheck migration registry.
//!
//! The 68 active checks and 24 retired reasons come from the frozen inventory
//! (`docs/refactor/implementation-inventory.json`). Each work package flips its own
//! entries from `not_implemented` to `implemented` when the real assertion exists;
//! a pending entry is reported and fails the run, never counted as passing.

pub mod cases;
pub mod legacy_r03;
pub mod legacy_r04;
pub mod legacy_r05;
pub mod legacy_r06;
pub mod legacy_r07;
pub mod legacy_r08;
pub mod legacy_r09;
pub mod legacy_r10;
pub mod legacy_r11;
pub mod legacy_r15;
pub mod legacy_r17;
pub mod registry;

pub use registry::{
    list_document, outcome_document, registry, run, CheckEntry, CheckStatus, RunOutcome,
};
