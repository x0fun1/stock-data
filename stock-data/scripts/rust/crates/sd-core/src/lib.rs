#![forbid(unsafe_code)]
//! Core value objects, version labels, policy constants and validation.
//!
//! This crate performs no filesystem, network or process I/O so the same
//! definitions are usable by the researchers, the independent auditor, the news
//! layer, the storage layer and the provider layer without creating a cycle.
//!
//! Layering rule (from the refactor plan): `sd-audit` must never depend on
//! `sd-research`; `sd-news` must never depend on research/audit/app/providers.
//! Only those crates and `sd-app` may depend on this one.
//!
//! Modules are added by the work package that owns them, so an empty module never
//! stands in for an unimplemented capability:
//!
//! | module | work package |
//! |---|---|
//! | versions, policy, error, contracts, security, clock, interfaces | R01 |
//! | json_value (ordered legacy JSON helpers) | R02 |
//! | calendar, market_data, data_checks | R03 |
//! | statistics, python_rng | R04 |
//! | gate_policy, reliability | R05 |

pub mod calendar;
pub mod clock;
pub mod contracts;
pub mod data_checks;
pub mod error;
pub mod float_sum;
pub mod gate_policy;
pub mod interfaces;
pub mod json_text;
pub mod market_data;
pub mod numbers;
pub mod policy;
pub mod random;
pub mod reliability;
pub mod security;
pub mod statistics;
pub mod versions;
