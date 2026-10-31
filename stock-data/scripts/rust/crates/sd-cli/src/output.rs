//! Output channels and exit codes.
//!
//! The legacy runtime has two incompatible channels and hosts branch on them, so
//! they are preserved exactly:
//!
//! | entry point | error output | exit |
//! |---|---|---|
//! | `global_stock_data.py` (gateway) | JSON envelope on **stdout** | 1 |
//! | `quant_research.py` (research) | JSON object on **stderr** | 2 |
//!
//! JSON is rendered with the Python separators (`, ` / `: `) so the text matches
//! what an existing host already parses.

use serde_json::{Map, Value};

use sd_core::error::SdError;

use sd_core::json_text::python_default;

/// Print one JSON document on stdout.
pub fn stdout_json(value: &Value) {
    println!("{}", python_default(value));
}

/// Print one JSON document on stderr.
pub fn stderr_json(value: &Value) {
    eprintln!("{}", python_default(value));
}

/// Research error object (`stderr`, exit 2).
pub fn research_error(error: &SdError) -> Value {
    let mut document = Map::new();
    document.insert("status".into(), Value::String("error".into()));
    document.insert(
        "error_type".into(),
        Value::String(error.error_type().into()),
    );
    document.insert(
        "error".into(),
        Value::String(sd_core::security::bounded_text(
            &Value::String(error.to_string()),
            600,
        )),
    );
    if let Some(code) = error.code() {
        document.insert("code".into(), Value::String(code.into()));
    }
    Value::Object(document)
}

/// Current UTC timestamp in the legacy on-disk form.
pub fn timestamp_now() -> String {
    use sd_core::clock::{Clock, SystemClock};
    SystemClock.utc_now()
}
