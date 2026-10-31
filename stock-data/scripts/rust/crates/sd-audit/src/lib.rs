#![forbid(unsafe_code)]
//! Independent audit (path E).
//!
//! Hard rule: this crate must **not** depend on `sd-research`. It rebuilds labels,
//! base rates, volatility and the probability from the frozen market data with its
//! own implementation, so a mutated estimator is caught instead of reproduced.
//! Sharing policy constants, data types and a verified calendar from `sd-core` is
//! allowed; sharing the estimator under audit is not.
//!
//! Modules: `probability_quality` and `audit` arrive with R05.
pub mod audit;
pub mod probability_quality;
