#![forbid(unsafe_code)]
//! On-disk formats and file lifecycle.
//!
//! | module | work package |
//! |---|---|
//! | legacy_json, jsonio, path_guard, atomic_write | R02 |
//! | calendar_resource, stock_data_adapter, snapshot | R03 |
//! | ledger | R09 |
//! | run_workspace, cleanup, retention, locks | R10 |
//!
//! Historical artifacts must stay readable: snapshot schemas 1.0-1.3 and ledger
//! schemas 1.0-3.1 are diagnosed, and only the current schema can be written.

pub mod atomic_write;
pub mod calendar_resource;
pub mod cleanup;
pub mod freeze;
pub mod jsonio;
pub mod ledger;
pub mod legacy_json;
pub mod locks;
pub mod path_guard;
pub mod retention;
pub mod run_workspace;
pub mod snapshot;
pub mod stock_data_adapter;
