#![forbid(unsafe_code)]
//! Provider gateway.
//!
//! `registry_generated.rs` is produced from the frozen R00 evidence by
//! `tools/refactor/gen_rust_assets.py`; it carries the 56 function signatures the
//! CLI must accept. Execution, transport policy, budgets and per-source parsers are
//! added by R12-R16:
//!
//! | module | work package |
//! |---|---|
//! | registry, transport, envelope, local | R12 |
//! | sina, tencent, eastmoney | R13 |
//! | sec, finra, cboe, treasury, cftc, nasdaq | R14 |
//! | yahoo/* | R15 |
//! | collector | R16 |

pub mod cboe;
pub mod cftc;
pub mod china;
pub mod collector;
pub mod common;
pub mod eastmoney;
pub mod envelope;
pub mod finra;
pub mod http;
pub mod local;
pub mod nasdaq;
pub mod official;
pub mod official_sources;
pub mod registry;
pub mod registry_generated;
pub mod sec;
pub mod sina;
pub mod tencent;
pub mod transport;
pub mod treasury;
pub mod yahoo_compat;
pub mod yahoo_gateway;
pub mod yahoo_legacy;
pub mod yahoo_provider;
pub mod yahoo_session;
pub mod yfinance_normalize;
