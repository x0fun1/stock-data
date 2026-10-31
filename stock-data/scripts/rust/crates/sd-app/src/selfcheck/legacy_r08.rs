//! Migrated R08 checks: delivery/stage contract, non-stale reporting status and
//! headline newline injection.
//!
//! T23 runs the real delivery gate on a synthetic frozen analysis bundle (the R11
//! orchestrator will re-run the same gate on its own bundle; the contract asserted here is
//! identical). T45 (compact profile) stays pending until R11 because it inspects
//! orchestrator artifacts.

use serde_json::{json, Map, Value};

use crate::delivery::validate_final_response;
use crate::report::{render_markdown, reporting_status_for, required_response_lines};
use crate::selfcheck::registry::CheckFn;
use sd_core::policy::{POLICY_VERSION, REQUIRED_STAGES};
use sd_news::normalize::normalize_articles;
use sd_news::pipeline::freeze_receipt;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

fn synthetic_result() -> Value {
    let news = json!({
        "ticker": "SYN", "market": "US", "horizon": "5D",
        "snapshot_id": "snap-1", "asof_timestamp": "2026-01-05T21:00:00Z",
        "status": "complete", "warnings": [], "warning_count": 0,
        "ai_lean": {
            "status": "assessed",
            "coverage": {"candidate_count": 1, "assessed_count": 1,
                         "direct_label_counts": {"bullish": 1, "bearish": 0, "neutral": 0,
                                                 "mixed": 0, "unclear": 0}},
            "items": [{"news_id": "event-1", "lean": "bullish", "reason": "beat",
                       "published_at": "2026-01-05T12:00:00Z",
                       "target_relevance": "direct", "importance": "major",
                       "url": "https://example.com/a", "headline": "Company beats estimates"}],
            "major_direct_items": [{"news_id": "event-1", "lean": "bullish", "reason": "beat",
                                    "published_at": "2026-01-05T12:00:00Z",
                                    "target_relevance": "direct", "importance": "major",
                                    "url": "https://example.com/a",
                                    "headline": "Company beats estimates"}]
        },
        "ai_lean_candidates": [{"news_id": "event-1"}],
        "expected_volatility_regime": {"regime": "normal"},
        "event_time_bucket_counts": {"pre_market": 1},
        "opinion_source_agreement": {"state": "mixed"},
        "event_source_agreement": {"state": "consistent"},
        "market_confirmation": "not_assessed",
    });
    let stages: Vec<Value> = REQUIRED_STAGES
        .iter()
        .map(|stage| {
            json!({"stage": stage, "status": "complete", "run_id": "run-1",
                   "snapshot_id": "snap-1", "policy_version": POLICY_VERSION})
        })
        .collect();
    json!({
        "run_id": "run-1", "snapshot_id": "snap-1", "policy_version": POLICY_VERSION,
        "analysis_id": "run-1", "analysis_status": "complete_with_limitations",
        "stages": stages,
        "snapshot": {
            "ticker": "SYN", "market": "US", "horizon": "5D", "snapshot_id": "snap-1",
            "asof_timestamp": "2026-01-05T21:00:00Z", "target_session": "2026-01-12",
            "last_bar_date": "2026-01-05", "data_validation": {"blockers": []}, "warnings": [],
            "policy": {"decision_session": "2026-01-05", "window_start_exclusive": "2025-10-06",
                       "window_end_inclusive": "2026-01-05", "coverage_status": "complete",
                       "coverage_recomputed": true, "calendar_verified": true,
                       "calendar_source": "XNYS", "expected_sessions": 63,
                       "observed_sessions": 63, "missing_sessions": 0,
                       "price_basis": "split_adjusted", "policy_version": POLICY_VERSION},
        },
        "consensus": {
            "prob_up": 0.57, "raw_probability": 0.61, "direction": "bullish",
            "model_lean": "up", "production_probability_eligible": true,
            "directional_edge_supported": false, "reason_codes": [], "computable": true,
            "current_prediction_integrity_pass": true, "oos_integrity_pass": true,
            "economic_viability": {"status": "thin"},
            "production": {"prob_up": 0.57, "matching_observations": 42,
                           "causal_base_rate": 0.52,
                           "probability_transform": {"k": 50, "n_eff": 12.5}},
            "evidence": {"directional_edge_state": "insufficient_evidence",
                         "replay_valid": 120, "replay_nonoverlap": 20, "replay_total": 130,
                         "replay_abstained": 10, "replay_coverage_ratio": 0.6667,
                         "oos_effective_samples": 24, "prospective_nonoverlap": 12,
                         "directional_edge_independent_supported": false,
                         "baseline_comparison": {"observations": 120, "effective_sample_size": 24,
                                                 "brier_model": 0.24,
                                                 "brier_causal_base_rate": 0.26,
                                                 "brier_constant_half": 0.25, "skill": 0.04,
                                                 "auc": 0.55, "verdict": "inconclusive"},
                         "relative_baseline_skill": [],
                         "oos_sampling_status": "anchored", "oos_sampling_role": "primary",
                         "directional_edge_primary_evidence": "in_window_replay",
                         "replay_type": "prefix_walk_forward"},
            "shadow_predictions": [], "diagnostics": {},
            "trading_signal_eligible": false, "aggregation_method": "single_path",
            "production_path_count": 1,
        },
        "adversarial_audit": {"status": "pass", "may_report_probability": true,
                              "may_report_direction": true, "vetoes": [], "warnings": []},
        "news_result": news,
    })
}

fn scratch_dir(label: &str) -> std::path::PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-selfcheck-r08-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch dir");
    directory
}

/// Write the analysis bundle, its frozen News result and the required response text.
fn write_bundle(
    directory: &std::path::Path,
    result: &Value,
    response_text: &str,
) -> std::path::PathBuf {
    let news = result["news_result"].clone();
    let receipt = freeze_receipt("news_result.json", &news, "2026-01-05T21:00:00Z");
    std::fs::write(
        directory.join("analysis.json"),
        serde_json::to_vec(result).expect("analysis json"),
    )
    .expect("write analysis");
    std::fs::write(
        directory.join("news_result.json"),
        serde_json::to_vec(&news).expect("news json"),
    )
    .expect("write news");
    std::fs::write(
        directory.join("news_result.freeze.json"),
        serde_json::to_vec(&receipt).expect("freeze json"),
    )
    .expect("write freeze");
    let response = directory.join("final_response.md");
    std::fs::write(&response, response_text).expect("write response");
    response
}

/// T23: the frozen bundle and its stage receipts must satisfy the delivery contract.
pub fn legacy_t23() -> Result<(), String> {
    let result = synthetic_result();
    let lines = required_response_lines(&result);
    let response_text = lines
        .values()
        .map(|value| match value {
            Value::String(text) => text.clone(),
            other => sd_core::contracts::python_str(other),
        })
        .collect::<Vec<_>>()
        .join("\n");
    let directory = scratch_dir("t23");
    let response = write_bundle(&directory, &result, &response_text);
    let validated = validate_final_response(&directory, &response)?;
    if validated["delivery_status"] != "validated" {
        return Err(failure("the delivered bundle must validate"));
    }
    let stages: Vec<String> = validated["stages"]["order"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(ToString::to_string)
                .collect()
        })
        .unwrap_or_default();
    if stages != REQUIRED_STAGES {
        return Err(failure(format!(
            "stage order {stages:?} != required sequence"
        )));
    }
    // A missing stage must be rejected by the contract.
    let mut broken = result.clone();
    if let Some(rows) = broken["stages"].as_array_mut() {
        rows.pop();
    }
    std::fs::write(
        directory.join("analysis.json"),
        serde_json::to_vec(&broken).expect("broken json"),
    )
    .expect("write broken");
    if validate_final_response(&directory, &response).is_ok() {
        let _ = std::fs::remove_dir_all(&directory);
        return Err(failure("a missing stage must be rejected"));
    }
    // A duplicated stage must be rejected too.
    let mut duplicated = result.clone();
    if let Some(rows) = duplicated["stages"].as_array_mut() {
        let first = rows[0].clone();
        rows.insert(1, first);
    }
    std::fs::write(
        directory.join("analysis.json"),
        serde_json::to_vec(&duplicated).expect("duplicated json"),
    )
    .expect("write duplicated");
    let outcome = validate_final_response(&directory, &response);
    let _ = std::fs::remove_dir_all(&directory);
    if outcome.is_ok() {
        return Err(failure("a duplicated stage must be rejected"));
    }
    Ok(())
}

/// T31: a veto must not leave the report saying "reportable".
pub fn legacy_t31() -> Result<(), String> {
    let stale = json!({
        "consensus": {"prob_up": null, "production_probability_eligible": true},
        "adversarial_audit": {"may_report_probability": false},
    });
    if reporting_status_for(&stale) != "unavailable" {
        return Err(failure(
            "a vetoed run with no probability must not be reportable",
        ));
    }
    let live = json!({
        "consensus": {"prob_up": 0.53, "production_probability_eligible": true},
        "adversarial_audit": {"may_report_probability": true},
    });
    if reporting_status_for(&live) != "reportable" {
        return Err(failure(
            "a live probability with audit clearance is reportable",
        ));
    }
    Ok(())
}

/// T32: provider text stays one line and cannot inject Markdown lines into the report.
pub fn legacy_t32() -> Result<(), String> {
    let normalized = normalize_articles(
        &json!({"articles": [{
            "title": "MU raises guidance\n## Injected heading\n- injected item",
            "published_at": "2026-01-05T13:00:00Z", "source": "Reuters",
            "url": "https://example.com/a", "tickers": ["MU"],
        }]}),
        "2026-01-09T00:00:00Z",
    );
    let Some(article) = normalized.articles.first() else {
        return Err(failure("the fixture article must survive normalization"));
    };
    let title = article["title"].as_str().unwrap_or("");
    if title.contains('\n') || title.contains('\r') {
        return Err(failure(format!(
            "the normalized headline still contains a newline: {title}"
        )));
    }
    // A headline with newlines must not create a Markdown heading in the report.
    let mut result = synthetic_result();
    if let Some(news) = result.get_mut("news_result").and_then(Value::as_object_mut) {
        news.insert(
            "major_events".into(),
            json!([{"headline": "MU raises guidance\n## Injected heading",
                    "direction": "positive", "eligible_evidence": true, "age_sessions": 1}]),
        );
        news.insert(
            "ai_lean".into(),
            json!({
                "status": "partial",
                "coverage": {"candidate_count": 1, "assessed_count": 1,
                             "direct_label_counts": {"bullish": 1, "bearish": 0, "neutral": 0,
                                                     "mixed": 0, "unclear": 0}},
                "items": [{"news_id": "event-1", "lean": "bullish", "reason": "beat",
                           "published_at": "2026-01-05T12:00:00Z",
                           "target_relevance": "direct", "importance": "major",
                           "url": "https://example.com/a",
                           "headline": "MU raises guidance\n## Injected heading"}],
                "major_direct_items": [{"news_id": "event-1", "lean": "bullish",
                                        "reason": "beat",
                                        "published_at": "2026-01-05T12:00:00Z",
                                        "target_relevance": "direct", "importance": "major",
                                        "url": "https://example.com/a",
                                        "headline": "MU raises guidance\n## Injected heading"}]
            }),
        );
    }
    let rendered = render_markdown(&result);
    if rendered
        .lines()
        .any(|line| line.trim_start().starts_with("## Injected heading"))
    {
        return Err(failure(
            "a headline must not be able to create a new Markdown heading",
        ));
    }
    let _ = Map::new();
    Ok(())
}

/// Implemented R08 checks.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T23", legacy_t23 as CheckFn),
        ("T31", legacy_t31 as CheckFn),
        ("T32", legacy_t32 as CheckFn),
    ]
}
