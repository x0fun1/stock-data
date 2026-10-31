//! Migrated R11 checks: orchestrator end-to-end paths, sampling tri-state, A/B protocol
//! statistics and selection statistics.
//!
//! T07/T37/T34/T45 drive the real orchestrator/CLI paths; F27/F28 assert the registered
//! statistics functions directly (no files, no network).

use std::path::{Path, PathBuf};
use std::process::Command;

use chrono::{Duration, NaiveDate};
use serde_json::{json, Map, Value};

use crate::experiments::momentum_ab;
use crate::orchestrator::{run_analysis, AnalysisOptions};
use sd_core::policy::{is_sampled_ordinal, session_ordinal};
use sd_core::random::PythonRandom;
use sd_research::momentum::{compute as momentum_compute, score_at as momentum_score_at};
use sd_research::selection::{
    multiple_testing_disclosure, overlapping_ic, stability_verdict, MINIMUM_OBSERVATIONS,
};

use crate::selfcheck::registry::CheckFn;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

/// Runtime selfcheck fixtures come from the mounted Skill (launcher exports
/// `STOCK_DATA_SKILL_ROOT`); the compile-time workspace path is only a development
/// fallback, never a production install-path requirement.
fn fixtures() -> PathBuf {
    if let Ok(root) = std::env::var("STOCK_DATA_SKILL_ROOT") {
        let candidate = PathBuf::from(root).join("scripts/rust/tests/fixtures");
        if candidate.is_dir() {
            return candidate;
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn scratch_dir(label: &str) -> PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-selfcheck-r11-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch dir");
    directory
}

fn read_json(path: &Path) -> Result<Value, String> {
    let text = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
    serde_json::from_str(&text).map_err(|error| error.to_string())
}

fn analyse(snapshot: &Path, root: &Path, artifacts: &str) -> Result<Value, String> {
    let options = AnalysisOptions {
        artifact_profile: artifacts,
        ..AnalysisOptions::default()
    };
    let location = run_analysis(snapshot, root, &options)?;
    read_json(&location.join("analysis.json"))
}

fn weekday_sessions(count: usize, end: NaiveDate) -> Vec<NaiveDate> {
    let mut days = Vec::with_capacity(count);
    let mut cursor = end;
    while days.len() < count {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            days.push(cursor);
        }
        cursor -= Duration::days(1);
    }
    days.reverse();
    days
}

/// T07: an unverified calendar blocks the formal probability.
pub fn legacy_t07() -> Result<(), String> {
    let work = scratch_dir("t07");
    let snapshot = fixtures().join("snapshot/missing-calendar");
    let payload = analyse(&snapshot, &work, "audit").map_err(failure)?;
    let result = (|| {
        if !payload["consensus"]["prob_up"].is_null() {
            return Err(failure(
                "an unverified calendar must not publish a probability",
            ));
        }
        let codes: Vec<String> = payload["consensus"]["reason_codes"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .map(|row| row.as_str().unwrap_or("").to_string())
                    .collect()
            })
            .unwrap_or_default();
        if !codes.iter().any(|code| code == "CALENDAR_UNVERIFIED") {
            return Err(failure(format!(
                "CALENDAR_UNVERIFIED must be disclosed: {codes:?}"
            )));
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&work);
    result
}

/// T37: the sampling phase is tri-state; unanchored stays `null`, not `false`.
pub fn legacy_t37() -> Result<(), String> {
    let sessions: Vec<NaiveDate> = (0..14)
        .filter_map(|offset| {
            NaiveDate::from_ymd_opt(2000, 1, 3)?.checked_add_signed(Duration::days(offset))
        })
        .filter(|date| date.format("%u").to_string().parse::<u32>().unwrap() < 6)
        .collect();
    let anchored = sessions
        .first()
        .copied()
        .ok_or_else(|| failure("sessions"))?;
    let sampled = |day: NaiveDate| -> Option<bool> {
        Some(is_sampled_ordinal(Some(session_ordinal(&sessions, day)?)))
    };
    if sampled(anchored) != Some(true) {
        return Err(failure("ordinal 0 must be sampled"));
    }
    let second = sessions
        .get(1)
        .copied()
        .ok_or_else(|| failure("sessions"))?;
    if sampled(second) != Some(false) {
        return Err(failure("ordinal 1 must be a verified non-sampled decision"));
    }
    let outside = NaiveDate::from_ymd_opt(2026, 10, 23).expect("date");
    if session_ordinal(&sessions, outside).is_some() {
        return Err(failure(
            "a decision outside the anchored calendar must be unanchored",
        ));
    }

    let work = scratch_dir("t37");
    let snapshot = fixtures().join("snapshot/complete-window");
    let payload = analyse(&snapshot, &work, "audit").map_err(failure)?;
    let result = (|| {
        let sampling = &payload["consensus"]["sampling"];
        if sampling["status"].as_str() != Some("unanchored_calendar") {
            return Err(failure(format!(
                "a 52-week calendar cannot be anchored: {sampling}"
            )));
        }
        if !sampling["decision_sampled"].is_null()
            || !payload["consensus"]["decision_sampled"].is_null()
        {
            return Err(failure("an unanchored phase must stay null"));
        }
        if sampling["counts_as_nonoverlap"].as_bool() != Some(false) {
            return Err(failure(
                "an unanchored phase must not count as non-overlapping evidence",
            ));
        }
        if payload["consensus"]["evidence"]["oos_sampling_status"].as_str() != Some("unanchored") {
            return Err(failure(format!(
                "the replay sampling status must agree with the consensus block: {}",
                payload["consensus"]["evidence"]["oos_sampling_status"]
            )));
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&work);
    result
}

/// F27: the A/B protocol decides from the registered rule, not from the data.
pub fn legacy_f27() -> Result<(), String> {
    // Ablation reproduces the frozen formula with one weight set to zero.
    let flat = json!({"risk_adjusted": 50.0, "regression_trend": 30.0, "relative_strength": 20.0});
    for (name, value) in momentum_ab::ablation_scores(flat.as_object().expect("flat")) {
        let score = value.as_f64().unwrap_or(f64::NAN);
        if (score - 100.0).abs() > 1e-9 {
            return Err(failure(format!(
                "dropping {name} must preserve a flat composite: {score}"
            )));
        }
    }
    let partial =
        json!({"risk_adjusted": 50.0, "regression_trend": 30.0, "relative_strength": 0.0});
    let renorm = momentum_ab::ablation_scores(partial.as_object().expect("partial"));
    let relative = renorm
        .get("relative_strength")
        .and_then(Value::as_f64)
        .unwrap_or(f64::NAN);
    if (relative - 100.0).abs() > 1e-9 {
        return Err(failure(
            "a zero-weight component must vanish under renormalisation",
        ));
    }

    // The bootstrap is reproducible and brackets the sample mean.
    let series: Vec<f64> = (0..120)
        .map(|index| 0.02 * (index as f64 / 7.0).sin() + 0.01)
        .collect();
    let first = momentum_ab::block_bootstrap_mean_ci(&series, 6, 400, 0.95, 12345)
        .ok_or_else(|| failure("bootstrap interval"))?;
    let second = momentum_ab::block_bootstrap_mean_ci(&series, 6, 400, 0.95, 12345)
        .ok_or_else(|| failure("bootstrap interval"))?;
    if first != second {
        return Err(failure(
            "the bootstrap must be deterministic for a fixed seed",
        ));
    }
    let mean = series.iter().copied().sum::<f64>() / series.len() as f64;
    if !(first.0 < mean && mean < first.1) {
        return Err(failure(format!(
            "interval {first:?} must contain the mean {mean}"
        )));
    }
    let wide = momentum_ab::block_bootstrap_mean_ci(&series, 24, 400, 0.95, 12345)
        .ok_or_else(|| failure("wide bootstrap interval"))?;
    if (wide.1 - wide.0) < (first.1 - first.0) - 1e-12 {
        return Err(failure("longer blocks must not narrow the interval"));
    }

    // Verdict rule: every primary horizon must clear zero; report-only never decides.
    let record = |delta: f64, interval: (f64, f64)| json!({"status": "evaluated", "delta_ic": delta, "ci_delta": [interval.0, interval.1]});
    let better = json!({
        "5": record(0.017, (0.005, 0.030)),
        "10": record(0.020, (0.002, 0.039)),
        "20": record(0.005, (-0.020, 0.030)),
    });
    let verdict = |value: Value| -> String {
        momentum_ab::decide_verdict(value.as_object().expect("verdict map")).to_string()
    };
    if verdict(better.clone()) != "composite_better" {
        return Err(failure(
            "both primary horizons clearing zero must read as better",
        ));
    }
    let mut marginal = better.clone();
    marginal["10"] = record(0.020, (-0.0002, 0.039));
    if verdict(marginal) != "indistinguishable" {
        return Err(failure(
            "an interval containing zero must not count as better",
        ));
    }
    let worse = json!({
        "5": record(-0.017, (-0.030, -0.005)),
        "10": record(-0.020, (-0.039, -0.002)),
    });
    if verdict(worse) != "composite_worse" {
        return Err(failure(
            "both primary horizons below zero must read as worse",
        ));
    }
    let skipped = json!({
        "5": {"status": "no_usable_sessions"},
        "10": record(0.02, (0.01, 0.03)),
    });
    if verdict(skipped) != "indistinguishable" {
        return Err(failure("an unevaluable primary horizon must fail closed"));
    }

    // Durability compares the disclosure slice with the registered verdict.
    if momentum_ab::assess_durability(
        "composite_better",
        &json!({"status": "evaluated", "verdict": "composite_better"}),
    ) != "consistent_across_pool_slices"
    {
        return Err(failure("matching slice verdicts must read as consistent"));
    }
    if momentum_ab::assess_durability(
        "composite_better",
        &json!({"status": "evaluated", "verdict": "indistinguishable"}),
    ) != "pool_dependent"
    {
        return Err(failure(
            "a flipping disclosure slice must read as pool dependent",
        ));
    }
    if momentum_ab::assess_durability("composite_better", &json!({"status": "not_applicable"}))
        != "not_assessed"
    {
        return Err(failure("a missing disclosure slice must be unassessed"));
    }

    // The offline point entry must agree with the report path it was extracted from.
    let end = NaiveDate::from_ymd_opt(2026, 10, 23).expect("date");
    let dates: Vec<String> = weekday_sessions(90, end)
        .iter()
        .map(ToString::to_string)
        .collect();
    let mut rng = PythonRandom::new(47);
    let mut prices: Vec<f64> = vec![100.0];
    for _ in 1..90 {
        let previous = *prices.last().expect("price");
        prices.push(previous * rng.gauss(0.001, 0.011).exp());
    }
    let mut benchmark = Map::new();
    let mut level = 100.0f64;
    for session in &dates {
        level *= rng.gauss(0.0, 0.009).exp();
        benchmark.insert(session.clone(), Value::from(level));
    }
    let benchmark_value = Value::Object(benchmark);
    let block = momentum_compute(
        &prices,
        &dates,
        Some(&benchmark_value),
        "SYN",
        Some("BMK"),
        "declared",
        None,
        None,
    )?;
    let point = momentum_score_at(&prices, &dates, Some(&benchmark_value), 89, None);
    if point["status"] != block["score_status"] {
        return Err(failure("point entry and report path disagree on status"));
    }
    let block_score = block["momentum_score_20d"].as_f64();
    let point_score = point["score"].as_f64();
    match (block_score, point_score) {
        (Some(left), Some(right)) if (left - right).abs() <= 1e-12 => {}
        _ => return Err(failure("point entry and report path disagree on the score")),
    }

    // Panel subsetting keeps the declared benchmark untouched.
    let mut closes = Vec::new();
    for symbol in ["AAA", "BBB", "XLK"] {
        closes.push((symbol.to_string(), prices.clone()));
    }
    let panel = momentum_ab::Panel {
        dates: dates.clone(),
        closes,
        benchmark: dates
            .iter()
            .enumerate()
            .map(|(index, day)| (day.clone(), 100.0 + index as f64))
            .collect(),
        dropped: Default::default(),
        asset_types: [
            ("AAA".to_string(), "stock".to_string()),
            ("BBB".to_string(), "stock".to_string()),
            ("XLK".to_string(), "etf".to_string()),
        ]
        .into_iter()
        .collect(),
        pool_manifest: None,
    };
    let symbols: std::collections::BTreeSet<String> =
        ["AAA".to_string(), "BBB".to_string()].into_iter().collect();
    let sliced = momentum_ab::subset_panel(&panel, &symbols);
    let kept: std::collections::BTreeSet<String> = sliced
        .closes
        .iter()
        .map(|(symbol, _)| symbol.clone())
        .collect();
    if kept != symbols {
        return Err(failure("subsetting must drop only the excluded symbols"));
    }
    if sliced.benchmark != panel.benchmark {
        return Err(failure("subsetting must not rewrite the benchmark series"));
    }
    Ok(())
}

/// F28: selection evidence is causal, reproducible and gated by declared rules.
pub fn legacy_f28() -> Result<(), String> {
    let labels: Vec<f64> = (0..80).map(|index| 0.01 * index as f64).collect();
    let values: Vec<f64> = labels
        .iter()
        .enumerate()
        .map(|(index, label)| 0.5 * label + 0.001 * ((index % 3) as f64 - 1.0))
        .collect();
    let indices: Vec<i64> = (10..70).collect();
    let first = overlapping_ic(
        &values.iter().map(|value| Some(*value)).collect::<Vec<_>>(),
        &labels.iter().map(|value| Some(*value)).collect::<Vec<_>>(),
        &indices,
        5,
        200,
        7,
    );
    if first["status"].as_str() != Some("assessed")
        || first["rank_ic"].as_f64().unwrap_or(0.0) < 0.9
    {
        return Err(failure(format!(
            "a monotone factor must read as a strong IC: {first}"
        )));
    }
    let mut extended_values: Vec<Option<f64>> = values.iter().map(|value| Some(*value)).collect();
    extended_values.extend([Some(9.0); 40]);
    let mut extended_labels: Vec<Option<f64>> = labels.iter().map(|value| Some(*value)).collect();
    extended_labels.extend([Some(9.0); 40]);
    let extended = overlapping_ic(&extended_values, &extended_labels, &indices, 5, 200, 7);
    if (extended["rank_ic"].as_f64().unwrap_or(f64::NAN)
        - first["rank_ic"].as_f64().unwrap_or(f64::NAN))
    .abs()
        > 1e-12
    {
        return Err(failure(
            "appending future data must not move a fixed-index estimate",
        ));
    }
    let repeat = overlapping_ic(
        &values.iter().map(|value| Some(*value)).collect::<Vec<_>>(),
        &labels.iter().map(|value| Some(*value)).collect::<Vec<_>>(),
        &indices,
        5,
        200,
        7,
    );
    if repeat != first {
        return Err(failure(
            "the block bootstrap must be deterministic for a fixed seed",
        ));
    }
    let thin_indices: Vec<i64> = (10..10 + MINIMUM_OBSERVATIONS as i64 - 1).collect();
    let thin_values: Vec<Option<f64>> = values.iter().map(|value| Some(*value)).collect();
    let thin_labels: Vec<Option<f64>> = labels.iter().map(|value| Some(*value)).collect();
    let thin = overlapping_ic(&thin_values, &thin_labels, &thin_indices, 5, 0, 0);
    if thin["status"].as_str() != Some("insufficient_samples")
        || !thin["rank_ic"].is_null()
        || !thin["interval"].is_null()
    {
        return Err(failure(format!("thin evidence must stay unscored: {thin}")));
    }

    let passing = json!({"status": "assessed", "rank_ic": 0.15, "sign_consistency": 1.0});
    if stability_verdict(&passing, 0.02, 2.0 / 3.0)["passed"] != Value::Bool(true) {
        return Err(failure("a stable factor above the IC floor must pass"));
    }
    let weak = json!({"status": "assessed", "rank_ic": 0.01, "sign_consistency": 1.0});
    if stability_verdict(&weak, 0.02, 2.0 / 3.0)["reason"].as_str() != Some("below_minimum_rank_ic")
    {
        return Err(failure(
            "a factor under the IC floor must be rejected for that reason",
        ));
    }
    let unstable = json!({"status": "assessed", "rank_ic": 0.15, "sign_consistency": 0.34});
    if stability_verdict(&unstable, 0.02, 2.0 / 3.0)["reason"].as_str()
        != Some("direction_unstable")
    {
        return Err(failure(
            "a factor flipping sign across sub-windows must be rejected",
        ));
    }
    let unscorable =
        json!({"status": "insufficient_samples", "rank_ic": null, "sign_consistency": null});
    if stability_verdict(&unscorable, 0.02, 2.0 / 3.0)["passed"].as_bool() == Some(true) {
        return Err(failure("unscored evidence must never pass"));
    }

    let disclosure = multiple_testing_disclosure(&[
        ("a".to_string(), Some(0.01)),
        ("b".to_string(), Some(0.4)),
        ("c".to_string(), None),
    ]);
    if disclosure["used_as_selection_gate"].as_bool() != Some(false)
        || disclosure["tested_candidates"].as_i64() != Some(2)
    {
        return Err(failure(format!(
            "FDR must stay disclosure-only: {disclosure}"
        )));
    }
    Ok(())
}

/// T34: `pipeline` runs the News stage without an explicit policy file.
pub fn legacy_t34() -> Result<(), String> {
    let work = scratch_dir("t34");
    let snapshot = fixtures().join("snapshot/complete-window");
    let result = (|| {
        let manifest = read_json(&snapshot.join("manifest.json"))?;
        let request = json!({
            "ticker": manifest["ticker"].clone(),
            "market": manifest["market"].clone(),
            "horizon": "5D",
            "intent": "forecast",
            "asof": manifest["asof_timestamp"].clone(),
        });
        let request_path = work.join("request.json");
        std::fs::write(
            &request_path,
            serde_json::to_string(&request).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let mut domains = Map::new();
        for name in [
            "market",
            "fundamentals",
            "insider",
            "macro",
            "news",
            "options",
            "sec",
            "short_volume",
        ] {
            let path = snapshot.join(format!("{name}.json"));
            if path.is_file() {
                domains.insert(name.to_string(), read_json(&path)?);
            }
        }
        let responses_path = work.join("responses.json");
        std::fs::write(
            &responses_path,
            serde_json::to_string(&json!({"domains": domains}))
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let runtime = work.join("runtime");
        let output = Command::new(std::env::current_exe().map_err(|error| error.to_string())?)
            .args([
                "research",
                "pipeline",
                "--request",
                &request_path.to_string_lossy(),
                "--responses",
                &responses_path.to_string_lossy(),
                "--output-root",
                &runtime.to_string_lossy(),
                "--no-gc",
            ])
            .output()
            .map_err(|error| error.to_string())?;
        if !output.status.success() {
            return Err(failure(format!(
                "pipeline failed with exit code {:?}: {}",
                output.status.code(),
                sd_core::security::bounded_text(
                    &Value::String(String::from_utf8_lossy(&output.stderr).to_string()),
                    400
                )
            )));
        }
        let research = runtime.join("research");
        let mut analyses: Vec<PathBuf> = std::fs::read_dir(&research)
            .map_err(|error| error.to_string())?
            .filter_map(|entry| entry.ok().map(|entry| entry.path().join("analysis.json")))
            .filter(|path| path.is_file())
            .collect();
        analyses.sort();
        let Some(latest) = analyses.last() else {
            return Err(failure("pipeline produced no analysis"));
        };
        let payload = read_json(latest)?;
        let news = payload
            .get("news_result")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if news["schema_version"].as_str() != Some("1.3") {
            return Err(failure(format!(
                "the News stage did not run by default: {:?} {:?}",
                news["status"],
                news.as_object()
                    .map(|map| map.keys().cloned().collect::<Vec<_>>())
            )));
        }
        let news_stage = payload["stages"]
            .as_array()
            .and_then(|rows| rows.iter().find(|row| row["stage"] == "news"))
            .cloned()
            .unwrap_or(Value::Null);
        let status = news_stage["status"].as_str().unwrap_or("");
        if !matches!(status, "complete" | "partial" | "not_assessed") {
            return Err(failure(format!(
                "unexpected News stage status: {news_stage}"
            )));
        }
        if news.get("warning_count").is_none() {
            return Err(failure("the News result must disclose its warning count"));
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&work);
    result
}

/// T45: compact artifacts keep pointers; timing and viability have one source.
pub fn legacy_t45() -> Result<(), String> {
    let work = scratch_dir("t45");
    let snapshot = fixtures().join("snapshot/complete-window");
    let result = (|| {
        let payload = analyse(&snapshot, &work, "audit")?;
        if payload["analysis_id"] != payload["run_id"] {
            return Err(failure("analysis_id must name the run"));
        }
        if !payload["elapsed_seconds"].is_number() {
            return Err(failure(
                "elapsed_seconds must be written before the artifact is frozen",
            ));
        }
        if payload["consensus"]["probability_basis"].as_str()
            != Some("drift_shrunk_causal_base_rate")
        {
            return Err(failure(format!(
                "probability_basis: {}",
                payload["consensus"]["probability_basis"]
            )));
        }
        if !matches!(
            payload["analysis_status"].as_str(),
            Some("complete_with_limitations" | "degraded" | "abstain")
        ) {
            return Err(failure(format!(
                "analysis_status must use the documented vocabulary: {}",
                payload["analysis_status"]
            )));
        }
        let viability = &payload["synthesis"]["economic_viability"];
        if viability["source"].as_str() != Some("quant_result.json.consensus.economic_viability") {
            return Err(failure(format!("economic viability block: {viability}")));
        }
        let compact = analyse(&snapshot, &work.join("research_compact"), "compact")?;
        if compact["artifact_profile"].as_str() != Some("compact") {
            return Err(failure("compact profile was not recorded"));
        }
        let rows = &compact["researchers"]["quant"]["validation"]["oos_forecasts"];
        let omitted = rows["omitted"].as_i64().unwrap_or(0);
        if !rows.is_object() || omitted == 0 {
            return Err(failure(format!(
                "compact profile must reference, not duplicate, replay rows: {rows}"
            )));
        }
        // The full rows remain in the researcher artifact.
        let root = work.join("research_compact");
        let mut analyses: Vec<PathBuf> = std::fs::read_dir(&root)
            .map_err(|error| error.to_string())?
            .filter_map(|entry| entry.ok().map(|entry| entry.path()))
            .filter(|path| path.join("researchers/quant.json").is_file())
            .collect();
        analyses.sort();
        if analyses.is_empty() {
            return Err(failure(
                "the full replay rows must remain in the researcher artifact",
            ));
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&work);
    result
}

/// Implemented R11 checks.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T07", legacy_t07 as CheckFn),
        ("T37", legacy_t37 as CheckFn),
        ("F27", legacy_f27 as CheckFn),
        ("F28", legacy_f28 as CheckFn),
        ("T34", legacy_t34 as CheckFn),
        ("T45", legacy_t45 as CheckFn),
    ]
}
