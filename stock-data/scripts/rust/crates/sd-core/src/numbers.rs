//! Finite-number guard.
//!
//! The legacy writers call `json.dumps(..., allow_nan=False)`, so a NaN or infinity
//! reaching a probability, statistic or timestamp aborts the write with
//! `ValueError: Out of range float values are not JSON compliant: nan`. Rust's
//! `serde_json::Value` cannot hold a non-finite number at all (it becomes `null`),
//! which would *silently* degrade the artifact instead of failing. [`FiniteFloat`]
//! closes that gap at the point where an estimator produces a value.

use crate::error::{SdError, SdResult};

/// A validated finite `f64`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FiniteFloat(f64);

impl FiniteFloat {
    /// Validate finiteness with the legacy error message.
    pub fn new(value: f64) -> SdResult<Self> {
        if value.is_finite() {
            Ok(Self(value))
        } else {
            Err(SdError::validation(format!(
                "Out of range float values are not JSON compliant: {}",
                crate::json_text::format_python_float(value)
            )))
        }
    }

    /// The underlying value (always finite).
    pub fn get(self) -> f64 {
        self.0
    }
}

impl TryFrom<f64> for FiniteFloat {
    type Error = SdError;

    fn try_from(value: f64) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

/// Reject non-finite members of a JSON payload.
///
/// `serde_json` cannot construct a non-finite number, so this is a defensive check
/// used before hashing external payloads; it documents the guarantee the legacy
/// `allow_nan=False` gave.
pub fn ensure_finite_payload(value: &serde_json::Value) -> SdResult<()> {
    match value {
        serde_json::Value::Number(number) => {
            if let Some(float) = number.as_f64() {
                if !float.is_finite() {
                    return Err(SdError::validation(format!(
                        "Out of range float values are not JSON compliant: {}",
                        crate::json_text::format_python_float(float)
                    )));
                }
            }
            Ok(())
        }
        serde_json::Value::Array(items) => items.iter().try_for_each(ensure_finite_payload),
        serde_json::Value::Object(object) => object.values().try_for_each(ensure_finite_payload),
        _ => Ok(()),
    }
}
