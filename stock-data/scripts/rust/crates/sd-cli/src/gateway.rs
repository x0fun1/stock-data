//! `stock-data gateway` - the local function gateway (`global_stock_data.py`).
//!
//! One JSON envelope per invocation. R12 executes the 12 local functions; the
//! provider-backed functions arrive with R13-R16 and until then fail honestly with
//! `NotImplementedError` (never a fake success).

use std::path::{Path, PathBuf};

use clap::Args;
use serde_json::{Map, Value};

use sd_core::json_text::python_default;
use sd_core::security::{redact_text, sanitize_data};
use sd_providers::china;
use sd_providers::envelope::{self, data_status};
use sd_providers::http::ReqwestTransport;
use sd_providers::local;
use sd_providers::official::ProviderEnv;
use sd_providers::official_sources;
use sd_providers::registry;
use sd_providers::transport::JobContext;
use sd_providers::transport::ProviderError;
use sd_providers::yahoo_gateway;

use chrono::Utc;

use crate::output;

/// Arguments for `gateway`.
#[derive(Debug, Args)]
pub struct GatewayArgs {
    /// List allowed functions and parameters.
    #[arg(long)]
    pub list: bool,
    /// Run a function from --list.
    #[arg(long)]
    pub function: Option<String>,
    /// JSON object of keyword arguments.
    #[arg(long, conflicts_with = "params_file")]
    pub params_json: Option<String>,
    /// UTF-8 JSON file, or - for stdin.
    #[arg(long)]
    pub params_file: Option<String>,
    /// Write the full envelope to a new file; stdout then contains only a receipt.
    #[arg(long)]
    pub output: Option<String>,
    /// Explicit writable yfinance cookie/timezone cache (Yahoo routes only).
    #[arg(long)]
    pub cache_dir: Option<String>,
}

/// Python annotation kind used by `_validate_parameters`: only plain annotations are
/// type-checked, unions (`int | None`) fall through exactly like the legacy code.
fn annotation_kind(annotation: &str) -> Option<&str> {
    let kind = annotation.split('[').next().unwrap_or(annotation);
    matches!(kind, "str" | "int" | "float" | "bool" | "dict" | "list").then_some(kind)
}

fn annotation_matches(kind: &str, value: &Value) -> bool {
    match kind {
        "str" => value.is_string(),
        "int" => matches!(value, Value::Number(number) if number.is_i64() || number.is_u64()),
        "float" => {
            matches!(value, Value::Number(number) if number.as_f64().map(f64::is_finite).unwrap_or(false))
        }
        "bool" => value.is_boolean(),
        "dict" => value.is_object(),
        "list" => value.is_array(),
        _ => true,
    }
}

/// Validate bound parameters against the frozen signature (`_validate_parameters`).
fn validate_parameters(
    spec: &sd_providers::registry_generated::FunctionSpec,
    params: &Map<String, Value>,
) -> Result<(), String> {
    let parameters: Value =
        serde_json::from_str(spec.parameters_json).unwrap_or(Value::Array(Vec::new()));
    let parameters = parameters.as_array().cloned().unwrap_or_default();
    let known: Vec<(String, bool, Option<String>, Option<Value>)> = parameters
        .iter()
        .filter_map(|row| {
            let name = row.get("name")?.as_str()?.to_string();
            let required = row
                .get("required")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let annotation = row
                .get("type")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let default = row.get("default").cloned();
            Some((name, required, annotation, default))
        })
        .collect();
    for (name, required, _, default) in &known {
        if *required && !params.contains_key(name) {
            return Err(format!(
                "Invalid function parameters: missing a required argument: '{name}'"
            ));
        }
        let _ = default;
    }
    for name in params.keys() {
        if !known.iter().any(|(known_name, _, _, _)| known_name == name) {
            return Err(format!(
                "Invalid function parameters: got an unexpected keyword argument '{name}'"
            ));
        }
    }
    for (name, _, annotation, default) in &known {
        let Some(value) = params.get(name) else {
            continue;
        };
        if value.is_null() && default.as_ref().map(Value::is_null).unwrap_or(false) {
            continue;
        }
        let Some(annotation) = annotation else {
            continue;
        };
        let Some(kind) = annotation_kind(annotation) else {
            continue;
        };
        if !annotation_matches(kind, value) {
            return Err(format!("Parameter {name} must have type {annotation}."));
        }
    }
    Ok(())
}

/// Mirror the common `json.JSONDecodeError` messages (line/column/char are 1-based
/// for line/column and 0-based for the character offset, exactly like CPython).
fn python_json_error(text: &str, error: &serde_json::Error) -> String {
    if text.trim().is_empty() {
        return "Expecting value: line 1 column 1 (char 0)".to_string();
    }
    let line = error.line();
    let column = error.column();
    let char_offset = text
        .chars()
        .take_while(|character| *character != '\n')
        .count()
        .min(column.saturating_sub(1));
    let message = error.to_string();
    if message.contains("key must be a string") || message.contains("key must be") {
        format!(
            "Expecting property name enclosed in double quotes: line {line} column {column} (char {char_offset})"
        )
    } else {
        format!("Expecting value: line {line} column {column} (char {char_offset})")
    }
}

fn load_parameters(args: &GatewayArgs) -> Result<Map<String, Value>, (String, String)> {
    let text = if let Some(text) = &args.params_json {
        text.clone()
    } else if let Some(file) = &args.params_file {
        if file == "-" {
            let mut buffer = String::new();
            std::io::Read::read_to_string(&mut std::io::stdin(), &mut buffer)
                .map_err(|error| ("OSError".to_string(), error.to_string()))?;
            buffer
        } else {
            std::fs::read_to_string(file)
                .map(|text| text.strip_prefix('\u{feff}').unwrap_or(&text).to_string())
                .map_err(|error| ("FileNotFoundError".to_string(), error.to_string()))?
        }
    } else {
        return Ok(Map::new());
    };
    let value: Value = serde_json::from_str(&text).map_err(|error| {
        (
            "JSONDecodeError".to_string(),
            python_json_error(&text, &error),
        )
    })?;
    match value {
        Value::Object(map) => Ok(map),
        _ => Err((
            "ValueError".to_string(),
            "Parameters must be a JSON object.".to_string(),
        )),
    }
}

fn error_message(message: &str) -> String {
    redact_text(&Value::String(message.to_string()))
        .chars()
        .take(1000)
        .collect()
}

fn write_envelope(path: &Path, envelope: &Value) -> Result<PathBuf, (String, String)> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| ("OSError".to_string(), error.to_string()))?;
    }
    let text = python_default(envelope);
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    let mut file = options.open(path).map_err(|error| {
        let kind = if error.kind() == std::io::ErrorKind::AlreadyExists {
            "FileExistsError"
        } else {
            "OSError"
        };
        (kind.to_string(), error.to_string())
    })?;
    use std::io::Write;
    writeln!(file, "{text}").map_err(|error| ("OSError".to_string(), error.to_string()))?;
    Ok(std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf()))
}

/// Merge a provider/fallback envelope into the legacy gateway envelope: the legacy CLI
/// does `envelope.update(data)` and then restores `source` and `function` (`main`).
fn merge_provider_envelope(
    base: Value,
    data: &Value,
    source: Option<&'static str>,
    function: Option<&str>,
) -> Value {
    let Some(extra) = data.as_object() else {
        return base;
    };
    let mut merged = match base {
        Value::Object(map) => map,
        other => return other,
    };
    for (key, value) in extra {
        merged.insert(key.clone(), value.clone());
    }
    if let Some(source) = source {
        merged.insert("source".into(), Value::String(source.to_string()));
    }
    if let Some(function) = function {
        merged.insert("function".into(), Value::String(function.to_string()));
    }
    Value::Object(merged)
}

/// Execute the gateway subcommand, returning the process exit code.
pub fn run(args: &GatewayArgs) -> u8 {
    let timestamp = output::timestamp_now();
    let source: Option<&'static str>;
    let function_name: Option<String>;
    let mut data: Option<Value> = None;
    let mut failure: Option<(String, String)> = None;
    let mut function_source: Option<&'static str> = None;
    let mut is_provider_yahoo = false;

    if args.list {
        source = Some("local");
        function_name = Some("list".to_string());
        if args.params_json.is_some() || args.params_file.is_some() {
            failure = Some((
                "ValueError".to_string(),
                "--list does not accept parameters.".to_string(),
            ));
        } else {
            data = Some(Value::Array(registry::list_entries()));
        }
    } else if let Some(function) = args.function.as_deref() {
        source = registry::find(function).map(|spec| spec.source);
        function_source = source;
        function_name = Some(function.to_string());
        match registry::find(function) {
            None => {
                failure = Some((
                    "ValueError".to_string(),
                    "Function is not in the allowed list; use --list.".to_string(),
                ));
            }
            Some(spec) => match load_parameters(args) {
                Err(error) => failure = Some(error),
                Ok(params) => match validate_parameters(spec, &params) {
                    Err(message) => failure = Some(("ValueError".to_string(), message)),
                    Ok(()) => {
                        if local::is_local_function(function) {
                            match local::call(function, &params) {
                                Ok(result) => data = Some(result),
                                Err(error) => {
                                    failure =
                                        Some((error.kind.error_type().to_string(), error.message))
                                }
                            }
                        } else if china::is_function(function) {
                            let transport = ReqwestTransport::new();
                            let mut context = JobContext::default();
                            match china::call(function, &params, &transport, &mut context) {
                                Ok(result) => data = Some(result),
                                Err(error) => {
                                    failure =
                                        Some((error.kind.error_type().to_string(), error.message))
                                }
                            }
                        } else if official_sources::is_function(function) {
                            let transport = ReqwestTransport::new();
                            let mut context = JobContext::default();
                            let env = ProviderEnv::from_process_env();
                            match official_sources::call(
                                function,
                                &params,
                                &env,
                                Utc::now(),
                                &transport,
                                &mut context,
                            ) {
                                Ok(result) => data = Some(result),
                                Err(error) => {
                                    failure =
                                        Some((error.kind.error_type().to_string(), error.message))
                                }
                            }
                        } else if yahoo_gateway::is_function(function) {
                            let transport = ReqwestTransport::new();
                            let mut context = JobContext::default();
                            let cache = args.cache_dir.as_ref().map(PathBuf::from).or_else(|| {
                                std::env::var("STOCK_DATA_YAHOO_CACHE_DIR")
                                    .ok()
                                    .filter(|value| !value.is_empty())
                                    .map(PathBuf::from)
                            });
                            if yahoo_gateway::is_provider(function) {
                                is_provider_yahoo = true;
                            }
                            if yahoo_gateway::is_legacy(function) {
                                match yahoo_gateway::call_legacy(
                                    function,
                                    &params,
                                    &transport,
                                    &mut context,
                                ) {
                                    Ok(result) => data = Some(result),
                                    Err(error) => {
                                        failure = Some((
                                            error.kind.error_type().to_string(),
                                            error.message,
                                        ))
                                    }
                                }
                            } else {
                                let timeout = params
                                    .get("timeout")
                                    .and_then(Value::as_f64)
                                    .filter(|value| *value > 0.0)
                                    .unwrap_or(15.0);
                                let job = sd_providers::transport::JobState::new(
                                    sd_providers::transport::JobContext::new(
                                        timeout,
                                        120.0,
                                        30,
                                        sd_providers::transport::MAX_RESPONSE_BYTES,
                                    ),
                                );
                                match yahoo_gateway::call_with_job(
                                    function, &params, cache, &transport, &job,
                                ) {
                                    Ok(result) => data = Some(result),
                                    Err((error_type, message)) => {
                                        failure = Some((error_type, message))
                                    }
                                }
                            }
                        } else {
                            failure = Some((
                                "NotImplementedError".to_string(),
                                format!(
                                    "gateway function {function} is not implemented by this build"
                                ),
                            ));
                        }
                    }
                },
            },
        }
    } else {
        source = None;
        function_name = None;
        failure = Some((
            "ValueError".to_string(),
            "one of the arguments --list --function is required".to_string(),
        ));
    }

    let _ = function_source;
    let envelope = match (&failure, &data) {
        (Some((error_type, message)), _) => envelope::error_envelope(
            source,
            function_name.as_deref(),
            error_type,
            &error_message(message),
            &timestamp,
        ),
        (None, Some(data)) => {
            let status = data_status(data);
            let _ = status;
            let base = envelope::success_envelope(
                source,
                function_name.as_deref().unwrap_or(""),
                data.clone(),
                &timestamp,
            );
            if is_provider_yahoo {
                merge_provider_envelope(base, data, source, function_name.as_deref())
            } else {
                base
            }
        }
        (None, None) => envelope::success_envelope(
            source,
            function_name.as_deref().unwrap_or(""),
            Value::Null,
            &timestamp,
        ),
    };
    let status = envelope
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("error")
        .to_string();
    let exit_code = if status == "error" || status == "failed" {
        1
    } else {
        0
    };
    match &args.output {
        Some(raw) => {
            let path = PathBuf::from(raw);
            match write_envelope(&path, &envelope) {
                Ok(resolved) => {
                    let data_value = data.clone().unwrap_or(Value::Null);
                    let receipt =
                        envelope::output_receipt(&envelope, &resolved, &data_value, false);
                    output::stdout_json(&receipt);
                    exit_code
                }
                Err((error_type, message)) => {
                    let error = envelope::error_envelope(
                        source,
                        function_name.as_deref(),
                        &error_type,
                        &error_message(&message),
                        &output::timestamp_now(),
                    );
                    output::stdout_json(&error);
                    1
                }
            }
        }
        None => {
            output::stdout_json(&sanitize_data(&envelope));
            exit_code
        }
    }
}

#[allow(dead_code)]
fn _keep_provider_error(error: ProviderError) -> String {
    error.message
}
