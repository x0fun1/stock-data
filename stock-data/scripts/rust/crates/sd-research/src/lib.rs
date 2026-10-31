#![forbid(unsafe_code)]
//! Quantitative researchers.
//!
//! * **A** `quant` - the only production path (v2 drift shrinkage).
//! * **B** `factor` - shadow factor selection.
//! * **C** `ml` - shadow compact daily refit.
//! * **D** `factor_backtest` - descriptive 5D diagnostic, never a probability.
//!
//! Researchers receive a verified [`sd_core::interfaces::MarketView`]; they never
//! read files, call providers or mutate shared state.
//!
//! Modules are added by their work package so an empty module can never be
//! mistaken for an implemented path:
//!
//! | module | work package |
//! |---|---|
//! | math_utils, quant | R04 |
//! | factor, ml, factor_backtest, momentum, selection, feasibility, purged_cv | R06 |

pub mod factor;
pub mod factor_backtest;
pub mod feasibility;
pub mod math_utils;
pub mod ml;
pub mod momentum;
pub mod purged_cv;
pub mod quant;
pub mod result;
pub mod selection;
