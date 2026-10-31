//! R15 offline Yahoo gateway wiring: preflight rejection, fail-closed screen route and
//! the legacy envelope merge. No network is touched by these cases.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_stock-data")
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn run(arguments: &[&str], extra: &[(&str, &str)]) -> Output {
    let mut command = Command::new(binary());
    command
        .args(arguments)
        .current_dir(repo_root())
        .env_remove("STOCK_DATA_YAHOO_CACHE_DIR");
    for (key, value) in extra {
        command.env(key, value);
    }
    command.output().expect("run stock-data")
}

fn stdout_json(output: &Output) -> Value {
    serde_json::from_str(String::from_utf8_lossy(&output.stdout).trim())
        .unwrap_or_else(|error| panic!("stdout is not JSON ({error})"))
}

#[test]
fn yahoo_provider_requires_an_explicit_cache_dir() {
    let output = run(
        &[
            "gateway",
            "--function",
            "yahoo_history",
            "--params-json",
            "{\"symbol\":\"AAPL\"}",
        ],
        &[],
    );
    assert_eq!(output.status.code(), Some(1));
    let envelope = stdout_json(&output);
    assert_eq!(envelope["status"], "error");
    assert_eq!(envelope["error_type"], "InvalidParameters");
    assert!(
        envelope["error_message"]
            .as_str()
            .unwrap_or_default()
            .contains("Explicit writable cache_dir required"),
        "unexpected message: {envelope}"
    );
}

#[test]
fn yahoo_batch_preflight_rejects_eleven_symbols_before_any_request() {
    let cache = std::env::temp_dir().join(format!("sd-yahoo-gateway-cache-{}", std::process::id()));
    std::fs::create_dir_all(&cache).expect("cache dir");
    let symbols = (0..11)
        .map(|index| format!("\"S{index}\""))
        .collect::<Vec<_>>()
        .join(",");
    let params = format!("{{\"symbols\":[{symbols}],\"period\":\"1y\"}}");
    let output = run(
        &[
            "gateway",
            "--function",
            "yahoo_history_batch",
            "--params-json",
            &params,
            "--cache-dir",
            cache.to_str().expect("cache path"),
        ],
        &[],
    );
    assert_eq!(output.status.code(), Some(1));
    let envelope = stdout_json(&output);
    assert_eq!(envelope["error_type"], "InvalidParameters");
    assert!(envelope["error_message"]
        .as_str()
        .unwrap_or_default()
        .contains("1 to 10 symbols"));
}

#[test]
fn yahoo_screen_fails_closed_without_a_legacy_route() {
    let cache = std::env::temp_dir().join(format!("sd-yahoo-gateway-cache-{}", std::process::id()));
    std::fs::create_dir_all(&cache).expect("cache dir");
    let output = run(
        &[
            "gateway",
            "--function",
            "yahoo_screen",
            "--params-json",
            "{\"query\":{\"operator\":\"eq\"}}",
            "--cache-dir",
            cache.to_str().expect("cache path"),
        ],
        &[],
    );
    assert_eq!(output.status.code(), Some(1), "no fabricated success");
    let envelope = stdout_json(&output);
    assert_eq!(envelope["status"], "error");
    // S06: a native-only capability gap is reported explicitly; no fallback envelope is
    // invented and the historical `NoLegacyRoute` projection is gone.
    assert_eq!(envelope["error_type"], "UnsupportedCapability");
    assert!(
        envelope["error_message"]
            .as_str()
            .unwrap_or_default()
            .contains("screener"),
        "{}",
        envelope["error_message"]
    );
    assert!(envelope.get("fallback_attempted").is_none());
    assert!(envelope.get("fallback_used").is_none());
}

#[test]
fn yahoo_function_list_includes_all_twenty_routes() {
    let output = run(&["gateway", "--list"], &[]);
    assert_eq!(output.status.code(), Some(0));
    let document = stdout_json(&output);
    let entries = document["data"].as_array().expect("data array").clone();
    let names: Vec<String> = entries
        .iter()
        .filter_map(|entry| {
            entry
                .get("function")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .collect();
    for expected in [
        "yahoo_history",
        "yahoo_history_batch",
        "yahoo_quote",
        "yahoo_profile",
        "yahoo_financials",
        "yahoo_search",
        "yahoo_statistics",
        "yahoo_analysis",
        "yahoo_holders",
        "yahoo_options",
        "yahoo_earnings",
        "yahoo_funds",
        "yahoo_screen",
        "stock_kline_yahoo",
        "yahoo_quote_summary",
        "key_statistics",
        "analyst_estimates",
        "institutional_holders",
        "financial_statements_yahoo",
        "options_chain",
    ] {
        assert!(
            names.iter().any(|name| name == expected),
            "missing {expected} in --list"
        );
    }
}
