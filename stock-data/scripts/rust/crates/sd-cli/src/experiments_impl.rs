//! Execution of the four `stock-data experiments` entry points.
//!
//! The modules in `sd-app::experiments` hold the deterministic protocols; this file is
//! the file I/O shell that mirrors each legacy script's `main()`: read frozen payloads,
//! write the same JSON/Markdown artifacts and print the same compact summary.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use sd_app::experiments::{momentum_ab, research_b_ab, research_candidates, selection_ab};
use sd_core::json_text::{python_pretty_ascii, python_readable};
use sd_storage::snapshot::load_snapshot;

use crate::experiments::ExperimentsCommand;

fn read_json(path: &Path) -> Result<Value, String> {
    let text = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    serde_json::from_str(text).map_err(|error| error.to_string())
}

fn write_text(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    std::fs::write(path, text).map_err(|error| error.to_string())
}

fn write_pretty(path: &Path, value: &Value) -> Result<(), String> {
    write_text(path, &(python_readable(value) + "\n"))
}

fn write_json_sorted(path: &Path, value: &Value) -> Result<(), String> {
    write_text(path, &python_pretty_ascii(value))
}

/// Recursively sort object keys so `json.dumps(..., sort_keys=True)` output matches.
fn sorted(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for key in keys {
                out.insert(key.clone(), sorted(&map[key]));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

fn collections_map(directory: &Path) -> Result<Map<String, Value>, String> {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(directory)
        .map_err(|_| format!("no collection payloads under {}", directory.display()))?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
        .collect();
    entries.sort();
    if entries.is_empty() {
        return Err(format!(
            "no collection payloads under {}",
            directory.display()
        ));
    }
    let mut payloads = Map::new();
    for path in entries {
        let stem = path
            .file_stem()
            .map(|value| value.to_string_lossy().to_string())
            .unwrap_or_default();
        payloads.insert(stem, read_json(&path)?);
    }
    Ok(payloads)
}

fn pool_manifest(path: &Option<String>) -> Result<Option<Map<String, Value>>, String> {
    let Some(path) = path else {
        return Ok(None);
    };
    let path = PathBuf::from(path);
    let value = read_json(&path)?;
    let Some(mut map) = value.as_object().cloned() else {
        return Err("pool manifest must contain a JSON object".into());
    };
    map.insert(
        "manifest_path".into(),
        Value::String(path.to_string_lossy().to_string()),
    );
    Ok(Some(map))
}

fn run_momentum(args: &crate::experiments::MomentumAbArgs) -> Result<u8, String> {
    let payloads = collections_map(Path::new(&args.collections_dir))?;
    let manifest = pool_manifest(&args.pool_manifest)?;
    let panel = momentum_ab::load_panel(&payloads, manifest.as_ref())?;
    let report = momentum_ab::build_report(&panel);
    write_pretty(Path::new(&args.output_json), &report)?;
    write_text(
        Path::new(&args.output_md),
        &momentum_ab::render_markdown(&report),
    )?;
    println!(
        "{}",
        sd_core::json_text::python_default(&json!({
            "verdict": report["verdict"].clone(),
            "claim": report["incremental_value_claim"].clone(),
            "symbols": report["pool"]["symbol_count"].clone(),
            "sessions": report["pool"]["sessions"].clone(),
        }))
    );
    Ok(0)
}

fn run_selection(args: &crate::experiments::PoolExperimentArgs) -> Result<u8, String> {
    if args.horizon != "5D" {
        return Err(format!(
            "the Rust selection experiment is registered for 5D only; got {}",
            args.horizon
        ));
    }
    let pool = read_json(Path::new(&args.pool))?;
    let collections = collections_map(Path::new(&args.collections_dir))?;
    let entries = selection_ab::load_pool(&pool, &collections, &args.asof)?;
    let report = selection_ab::evaluate(&entries);
    let output = Path::new(&args.output_dir);
    std::fs::create_dir_all(output).map_err(|error| error.to_string())?;
    write_json_sorted(&output.join("selection_ab.json"), &sorted(&report))?;
    write_text(
        &output.join("selection_ab.md"),
        &selection_ab::render_markdown(&report),
    )?;
    let summary = json!({
        "pool": report["pool"].clone(),
        "pooled": report["pooled"].clone(),
        "decision": report["decision"].clone(),
    });
    println!("{}", python_pretty_ascii(&sorted(&summary)));
    Ok(0)
}

fn run_research_b(args: &crate::experiments::PoolExperimentArgs) -> Result<u8, String> {
    if args.horizon != "5D" {
        return Err(format!(
            "the Rust research-B experiment is registered for 5D only; got {}",
            args.horizon
        ));
    }
    let pool = read_json(Path::new(&args.pool))?;
    let collections = collections_map(Path::new(&args.collections_dir))?;
    let entries = research_b_ab::load_pool(&pool, &collections, &args.asof)?;
    let report = research_b_ab::evaluate(&entries)?;
    let output = Path::new(&args.output_dir);
    std::fs::create_dir_all(output).map_err(|error| error.to_string())?;
    write_json_sorted(&output.join("research_b_ab.json"), &sorted(&report))?;
    write_text(
        &output.join("research_b_ab.md"),
        &research_b_ab::render_markdown(&report),
    )?;
    let comparisons = report["comparisons"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    let mut compact = Map::new();
    for (name, record) in &comparisons {
        compact.insert(
            name.clone(),
            json!({
                "sessions": record["sessions"].clone(),
                "mean_delta": record["mean_delta"].clone(),
                "interval_status": record["interval_status"].clone(),
                "verdict": record["verdict"].clone(),
            }),
        );
    }
    println!(
        "{}",
        python_pretty_ascii(&sorted(&json!({
            "pool": entries.len(),
            "comparisons": Value::Object(compact),
        })))
    );
    Ok(0)
}

fn run_candidates(args: &crate::experiments::ResearchCandidatesArgs) -> Result<u8, String> {
    let mut reports: Vec<Value> = Vec::new();
    for snapshot_dir in &args.snapshot_dir {
        let loaded = load_snapshot(snapshot_dir).map_err(|error| error.message().to_string())?;
        let mut market = loaded
            .domains
            .get("market")
            .cloned()
            .unwrap_or_else(|| json!({"data": {}}));
        if let Some(root) = market.as_object_mut() {
            root.insert("_data_validation".into(), loaded.frozen_validation.clone());
        }
        let bars = sd_core::market_data::market_bars(&market, false)
            .map_err(|error| error.message().to_string())?
            .iter()
            .map(|bar| {
                json!({
                    "date": bar.date,
                    "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "volume": bar.volume,
                })
            })
            .collect::<Vec<Value>>();
        let ticker = loaded
            .manifest
            .get("ticker")
            .map(sd_core::contracts::py_str)
            .unwrap_or_default();
        let horizon = loaded
            .manifest
            .get("horizon")
            .map(sd_core::contracts::py_str)
            .unwrap_or_default();
        let report = research_candidates::report_for_snapshot(
            &snapshot_dir.to_string_lossy(),
            &ticker,
            &horizon,
            &bars,
        )?;
        reports.push(report);
    }
    let text = python_readable(&Value::Array(reports));
    match &args.output {
        Some(output) => write_text(output, &(text + "\n"))?,
        None => println!("{text}"),
    }
    Ok(0)
}

/// Execute one experiment subcommand, returning the process exit code.
pub fn execute(command: &ExperimentsCommand) -> u8 {
    let outcome = match command {
        ExperimentsCommand::MomentumAb(args) => run_momentum(args),
        ExperimentsCommand::SelectionAb(args) => run_selection(args),
        ExperimentsCommand::ResearchBAb(args) => run_research_b(args),
        ExperimentsCommand::ResearchCandidates(args) => run_candidates(args),
    };
    match outcome {
        Ok(code) => code,
        Err(message) => {
            crate::output::stderr_json(&json!({
                "status": "error",
                "error_type": "ValueError",
                "error": sd_core::security::bounded_text(&Value::String(message), 600),
            }));
            2
        }
    }
}
