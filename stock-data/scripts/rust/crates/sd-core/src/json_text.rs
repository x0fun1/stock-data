//! Python-compatible JSON text profiles.
//!
//! The legacy runtime writes JSON in several distinct shapes and hashes some of
//! them; the shape is therefore a disk contract, not cosmetics. R01 needs the two
//! shapes the CLI already emits (compact stdout messages and `ensure_ascii`
//! pretty-printed schema documents); R02 adds the byte-exact canonical/digest
//! profiles with differential tests against Python.
//!
//! Rules reproduced here:
//!
//! * `json.dumps(value)` default separators are `", "` and `": "`;
//! * `indent=2` prints one item per line with two-space indentation, `": "` after
//!   keys, and no trailing comma;
//! * `ensure_ascii=True` escapes every non-ASCII code point as `\uXXXX` using
//!   lowercase hex (surrogate pairs above the BMP);
//! * floats use Python's `repr(float)` shape: shortest round-trip digits, `e+NN`
//!   / `e-NN` exponent form for very large/small magnitudes, `-0.0` preserved.

use serde_json::{Map, Value};

/// Python `repr(float)`-compatible rendering.
///
/// CPython prints the shortest round-tripping digits and switches to exponential
/// notation when the decimal exponent is `< -4` or `>= 16`; the exponent always
/// carries a sign and at least two digits (`1e-07`, `1.2345e+17`), and integral
/// values keep a trailing `.0`.
pub fn format_python_float(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value.is_sign_negative() {
            "-inf".to_string()
        } else {
            "inf".to_string()
        };
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        };
    }
    // Rust's `{:e}` yields the shortest round-trip scientific form ("1e20",
    // "1.2345678901234568e17"), which is exactly the digit sequence Python prints.
    let scientific = format!("{value:e}");
    let (mantissa, exponent_text) = match scientific.split_once('e') {
        Some((mantissa, exponent)) => (mantissa, exponent),
        None => (scientific.as_str(), "0"),
    };
    let Ok(exponent) = exponent_text.parse::<i32>() else {
        return scientific;
    };
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let prefix = if negative { "-" } else { "" };
    if !(-4..16).contains(&exponent) {
        let sign = if exponent < 0 { '-' } else { '+' };
        let head = &digits[..1];
        let tail = &digits[1..];
        let mantissa_text = if tail.is_empty() {
            head.to_string()
        } else {
            format!("{head}.{tail}")
        };
        return format!("{prefix}{mantissa_text}e{sign}{:02}", exponent.abs());
    }
    if exponent >= 0 {
        let split = (exponent + 1) as usize;
        let integer = if digits.len() >= split {
            digits[..split].to_string()
        } else {
            format!("{digits}{}", "0".repeat(split - digits.len()))
        };
        let fraction = if digits.len() > split {
            digits[split..].to_string()
        } else {
            "0".to_string()
        };
        format!("{prefix}{integer}.{fraction}")
    } else {
        let zeros = "0".repeat((-exponent - 1) as usize);
        format!("{prefix}0.{zeros}{digits}")
    }
}

fn escape_string(text: &str, ascii_only: bool) -> String {
    let mut output = String::with_capacity(text.len() + 2);
    output.push('"');
    for character in text.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            '\u{0008}' => output.push_str("\\b"),
            '\u{000c}' => output.push_str("\\f"),
            control if (control as u32) < 0x20 => {
                output.push_str(&format!("\\u{:04x}", control as u32))
            }
            other if ascii_only && (other as u32) > 0x7f => {
                let code = other as u32;
                if code <= 0xffff {
                    output.push_str(&format!("\\u{code:04x}"));
                } else {
                    let adjusted = code - 0x1_0000;
                    let high = 0xd800 + (adjusted >> 10);
                    let low = 0xdc00 + (adjusted & 0x3ff);
                    output.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
                }
            }
            other => output.push(other),
        }
    }
    output.push('"');
    output
}

fn write_value(
    output: &mut String,
    value: &Value,
    ascii_only: bool,
    indent: Option<usize>,
    depth: usize,
) {
    write_value_spaced(output, value, ascii_only, indent, depth, false);
}

/// Shared writer; `spaced` uses Python's default `, ` separator when single-line.
fn write_value_spaced(
    output: &mut String,
    value: &Value,
    ascii_only: bool,
    indent: Option<usize>,
    depth: usize,
    spaced: bool,
) {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(flag) => output.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => {
            // With `arbitrary_precision` the raw token is available: an integer that
            // does not fit i64/u64 (a 30-digit test value, a share count) must be
            // printed verbatim, while float tokens are normalised to Python's
            // `repr(float)` shape. Without this split a big integer would silently
            // become an f64 and change the artifact hash.
            let raw = number.to_string();
            if raw.contains(['.', 'e', 'E']) {
                match number.as_f64() {
                    Some(float) => output.push_str(&format_python_float(float)),
                    None => output.push_str(&raw),
                }
            } else {
                output.push_str(&raw);
            }
        }
        Value::String(text) => output.push_str(&escape_string(text, ascii_only)),
        Value::Array(items) => {
            if items.is_empty() {
                output.push_str("[]");
                return;
            }
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    if spaced && indent.is_none() {
                        output.push_str(", ");
                    } else {
                        output.push(',');
                    }
                }
                if let Some(width) = indent {
                    output.push('\n');
                    output.push_str(&" ".repeat(width * (depth + 1)));
                }
                write_value_spaced(output, item, ascii_only, indent, depth + 1, spaced);
            }
            if let Some(width) = indent {
                output.push('\n');
                output.push_str(&" ".repeat(width * depth));
            }
            output.push(']');
        }
        Value::Object(object) => {
            if object.is_empty() {
                output.push_str("{}");
                return;
            }
            output.push('{');
            for (index, (key, item)) in object.iter().enumerate() {
                if index > 0 {
                    if spaced && indent.is_none() {
                        output.push_str(", ");
                    } else {
                        output.push(',');
                    }
                }
                if let Some(width) = indent {
                    output.push('\n');
                    output.push_str(&" ".repeat(width * (depth + 1)));
                }
                output.push_str(&escape_string(key, ascii_only));
                output.push_str(": ");
                write_value(output, item, ascii_only, indent, depth + 1);
            }
            if let Some(width) = indent {
                output.push('\n');
                output.push_str(&" ".repeat(width * depth));
            }
            output.push('}');
        }
    }
}

/// `json.dumps(value, ensure_ascii=False)` - default separators (`, `), single line.
pub fn python_default(value: &Value) -> String {
    let mut output = String::new();
    write_value_spaced(&mut output, value, false, None, 0, true);
    output
}

/// `json.dumps(value, ensure_ascii=False, separators=(",", ":"))` - canonical form.
pub fn python_compact(value: &Value) -> String {
    let mut output = String::new();
    write_value(&mut output, value, false, None, 0);
    output
}

/// `json.dumps(value, ensure_ascii=True, indent=2)` - the `schema` command shape.
pub fn python_pretty_ascii(value: &Value) -> String {
    let mut output = String::new();
    write_value(&mut output, value, true, Some(2), 0);
    output
}

/// `jsonio.json_payload`: `ensure_ascii=False, indent=2` plus a trailing newline.
pub fn python_readable(value: &Value) -> String {
    let mut output = String::new();
    write_value(&mut output, value, false, Some(2), 0);
    output.push('\n');
    output
}

/// Rebuild a [`Value`] with object keys sorted (used before canonical serialization).
pub fn sorted_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut sorted: Vec<(&String, &Value)> = object.iter().collect();
            sorted.sort_by(|left, right| left.0.cmp(right.0));
            let mut rebuilt = Map::new();
            for (key, item) in sorted {
                rebuilt.insert(key.clone(), sorted_value(item));
            }
            Value::Object(rebuilt)
        }
        Value::Array(items) => Value::Array(items.iter().map(sorted_value).collect()),
        other => other.clone(),
    }
}

/// `jsonio.canonical_bytes`: sorted keys, `(",", ":")`, UTF-8, no trailing newline.
///
/// Non-finite numbers cannot be represented by [`Value`]; the guard against them
/// lives in [`crate::numbers::FiniteFloat`] so an invalid estimator result is
/// rejected where it is produced instead of being silently written as `null`.
pub fn canonical_text(value: &Value) -> String {
    canonical_text_with_ascii(value, false)
}

/// Canonical bytes with `ensure_ascii` selectable: the stage-digest profile escapes
/// non-ASCII, the snapshot/ledger profiles do not.
pub fn canonical_text_with_ascii(value: &Value, ascii_only: bool) -> String {
    fn write(output: &mut String, value: &Value, ascii_only: bool) {
        match value {
            Value::Object(object) => {
                let mut sorted: Vec<(&String, &Value)> = object.iter().collect();
                sorted.sort_by(|left, right| left.0.cmp(right.0));
                output.push('{');
                for (index, (key, item)) in sorted.into_iter().enumerate() {
                    if index > 0 {
                        output.push(',');
                    }
                    output.push_str(&escape_string(key, ascii_only));
                    output.push(':');
                    write(output, item, ascii_only);
                }
                output.push('}');
            }
            Value::Array(items) => {
                output.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        output.push(',');
                    }
                    write(output, item, ascii_only);
                }
                output.push(']');
            }
            other => write_value(output, other, ascii_only, None, 0),
        }
    }
    let mut output = String::new();
    write(&mut output, value, ascii_only);
    output
}
