#![forbid(unsafe_code)]
//! News layer.
//!
//! Boundaries (R07): the input type contains identity, as-of, snapshot, articles,
//! frozen bars and coverage only. It has no Quant field and returns no probability,
//! so "News changes Quant" is unrepresentable rather than merely forbidden. The
//! per-headline bullish/bearish judgement is supplied by the host as a frozen file
//! and only validated here.
//!
//! Modules: `contracts, normalize, timing, deduplicate, decay, classify, impact,
//! reaction, status, ai_assessment, pipeline` arrive with R07.
pub mod ai_assessment;
pub mod classify;
pub mod contracts;
pub mod decay;
pub mod deduplicate;
pub mod impact;
pub mod normalize;
pub mod pipeline;
pub mod reaction;
pub mod status;
pub mod timing;
