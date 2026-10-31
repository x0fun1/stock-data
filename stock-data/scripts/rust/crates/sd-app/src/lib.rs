#![forbid(unsafe_code)]
//! Application layer: it wires frozen inputs to the researchers, the independent
//! auditor, the news layer and the storage layer, and owns the run lifecycle.
//!
//! | module | work package |
//! |---|---|
//! | consensus | R05 |
//! | report, synthesis, delivery | R08 |
//! | orchestrator, experiments/* | R11 |
//! | selfcheck | R17 (registry seeded in R01) |

pub mod cleanup_gate;
pub mod consensus;
pub mod delivery;
pub mod experiments;
pub mod orchestrator;
pub mod report;
pub mod selfcheck;
pub mod selfcheck_generated;
pub mod synthesis;
