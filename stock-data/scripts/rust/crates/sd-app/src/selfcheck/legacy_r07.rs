//! Migrated R07 checks: three-state News, deterministic screening, timing/priced-in,
//! host AI lean validation, coverage reconciliation and presentation-warning severity.
//!
//! Each function mirrors the corresponding `selfcheck.py` check. T25/T35/T41 call the
//! real `crate::delivery::{validate_response_text, require_ai_news_coverage}` API.
//! `pipeline::finalize_warnings` is private in R07, so T42 exercises its semantics
//! through the observable `analyze_news` output instead.

use crate::delivery::{require_ai_news_coverage, validate_response_text};
use chrono::{Duration, NaiveDate};
use serde_json::{json, Map, Value};

use crate::report::{render_final_response, render_markdown, required_response_lines};
use sd_core::random::PythonRandom;
use sd_news::classify::direction_for;
use sd_news::contracts::NewsInput;
use sd_news::pipeline::{analyze_news, warning_details, warning_severity_counts};
use sd_news::status::{news_assessed, news_status_label};
use sd_news::timing::{aggregate_priced_in, event_time_bucket, priced_in_assessment};

use crate::selfcheck::registry::CheckFn;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

// ----------------------------------------------------------------- synthetic inputs

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

/// `synth_bars` from selfcheck.py: deterministic MT19937 walk, weekdays only.
fn synth_bars(count: usize, seed: u64) -> Vec<Value> {
    let mut rng = PythonRandom::new(seed);
    let mut price = 100.0f64;
    let mut day = NaiveDate::from_ymd_opt(2023, 1, 2).expect("start date");
    let mut bars: Vec<Value> = Vec::new();
    while bars.len() < count {
        if day.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            price *= rng.gauss(0.0005, 0.018).exp();
            bars.push(json!({
                "date": day.to_string(),
                "open": round4(price * 0.997),
                "high": round4(price * 1.012),
                "low": round4(price * 0.988),
                "close": round4(price),
                "volume": 1_000_000.0 + bars.len() as f64,
            }));
        }
        day += Duration::days(1);
    }
    bars
}

fn bar_dates(bars: &[Value], last: usize) -> Vec<String> {
    bars.iter()
        .rev()
        .take(last)
        .rev()
        .map(|bar| bar["date"].as_str().unwrap_or("").to_string())
        .collect()
}

/// `_f17_articles` from selfcheck.py.
fn f17_articles(bars: &[Value]) -> Vec<Value> {
    let dates = bar_dates(bars, 5);
    vec![
        json!({"article_id": "a1", "title": format!("MU raises guidance on strong demand ({})", dates[4]),
            "url": "https://example.com/a1", "source": "Reuters", "source_type": "news",
            "record_type": "news", "published_at": format!("{}T13:00:00Z", dates[4]),
            "tickers": ["MU"], "summary": null}),
        json!({"article_id": "a2", "title": format!("MU misses estimates as demand weak ({})", dates[3]),
            "url": "https://example.com/a2", "source": "Bloomberg", "source_type": "news",
            "record_type": "news", "published_at": format!("{}T13:00:00Z", dates[3]),
            "tickers": ["MU"], "summary": null}),
        json!({"article_id": "a3", "title": format!("MU wins contract ({})", dates[2]),
            "url": "https://example.com/a3", "source": "Reuters", "source_type": "news",
            "record_type": "news", "published_at": format!("{}T13:00:00Z", dates[2]),
            "tickers": ["MU"], "summary": null, "canonical_event_id": "dup"}),
        json!({"article_id": "a4", "title": format!("MU profit falls after lawsuit ({})", dates[2]),
            "url": "https://example.com/a4", "source": "Bloomberg", "source_type": "news",
            "record_type": "news", "published_at": format!("{}T15:00:00Z", dates[2]),
            "tickers": ["MU"], "summary": null, "canonical_event_id": "dup"}),
        json!({"article_id": "a5", "title": format!("Chipmaker expands capacity in Asia ({})", dates[4]),
            "url": "https://example.com/a5", "source": "Wire", "source_type": "news",
            "record_type": "news", "published_at": format!("{}T14:00:00Z", dates[4]),
            "tickers": [], "summary": null}),
    ]
}

/// `_news_input` from selfcheck.py (ticker MU, US, 5D, complete).
fn news_input(bars: &[Value], articles: Vec<Value>) -> NewsInput {
    let last = bars
        .last()
        .and_then(|bar| bar["date"].as_str())
        .unwrap_or("");
    let mut coverage = Map::new();
    coverage.insert("session_closes".into(), Value::Object(Map::new()));
    coverage.insert(
        "calendar_verification".into(),
        Value::String("source_supplied_schedule".into()),
    );
    NewsInput {
        ticker: "MU".into(),
        market: "US".into(),
        horizon: "5D".into(),
        asof_timestamp: format!("{last}T21:30:00Z"),
        snapshot_id: "snap_f17".into(),
        news_status: "complete".into(),
        source_sentiment: None,
        articles,
        bars: bars.to_vec(),
        warnings: Vec::new(),
        coverage,
    }
}

fn analysis(input: &NewsInput, ai: Option<&Value>) -> Result<Value, String> {
    analyze_news(input, None, ai)
}

// ----------------------------------------------------------------- shared fixtures

/// `_fake_delivery_result` from selfcheck.py.
fn fake_delivery_result(news_status: &str) -> Value {
    json!({
        "snapshot": {
            "ticker": "SYN", "market": "US", "horizon": "5D",
            "asof_timestamp": "2026-01-02T21:00:00Z",
            "policy": {"decision_session": "2026-01-02", "window_end_inclusive": "2026-01-02",
                       "policy_version": "fixed_52w_5d_v1"},
            "target_session": "2026-01-09",
            "snapshot_id": "SYN_TEST",
        },
        "consensus": {
            "prob_up": 0.53, "model_lean": "up", "production_probability_eligible": true,
            "directional_edge_supported": false, "computable": true,
            "current_prediction_integrity_pass": true, "oos_integrity_pass": true,
            "production": {"probability_transform": {"k": 50}},
            "evidence": {
                "replay_type": "prefix_walk_forward", "replay_valid": 230, "replay_nonoverlap": 39,
                "replay_abstained": 0, "oos_effective_samples": 46, "prospective_nonoverlap": 0,
                "directional_edge_state": "insufficient_evidence",
                "directional_edge_primary_evidence": "full_prefix_replay",
                "relative_baseline_skill": [
                    {"block_length": 6, "primary": true, "status": "estimated",
                     "mean": 0.0201, "lower": -0.002, "upper": 0.0517},
                ],
                "baseline_comparison": {"brier_model": 0.2553, "brier_causal_base_rate": 0.2754,
                                        "brier_constant_half": 0.25, "auc": 0.4886,
                                        "effective_sample_size": 46, "observations": 230},
            },
        },
        "news_result": {"status": news_status},
        "adversarial_audit": {"may_report_probability": true},
    })
}

fn line_text(lines: &Map<String, Value>, key: &str) -> String {
    lines
        .get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_default()
}

fn ai_artifact(input: &NewsInput, items: Vec<Value>) -> Value {
    json!({
        "schema_version": "1.0",
        "ticker": "MU",
        "asof_timestamp": input.asof_timestamp,
        "basis": "headline_level_ai_screen_v1",
        "model": {
            "id": "selfcheck-stub",
            "prompt_sha256": "0".repeat(64),
            "generated_at_utc": input.asof_timestamp,
        },
        "items": items,
    })
}

fn candidate_rows(result: &Value) -> Vec<Value> {
    result
        .get("ai_lean_candidates")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn assert_core_unchanged(scored: &Value, baseline: &Value, label: &str) -> Result<(), String> {
    for key in [
        "overall_direction",
        "confidence",
        "event_strength",
        "market_confirmation",
        "status",
    ] {
        if scored.get(key) != baseline.get(key) {
            return Err(failure(format!(
                "{label}: lean must not change News {key}: {:?} vs {:?}",
                scored.get(key),
                baseline.get(key)
            )));
        }
    }
    Ok(())
}

fn ids(rows: &[Value], key: &str) -> Vec<String> {
    rows.iter()
        .filter_map(|row| {
            row.get(key)
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .collect()
}

// ----------------------------------------------------------------- checks

/// T25: partial is assessed evidence and the warning count must be shown.
pub fn legacy_t25() -> Result<(), String> {
    if !news_assessed(Some("partial"))
        || !news_assessed(Some("complete"))
        || !news_assessed(Some("valid"))
    {
        return Err(failure("partial/complete/valid are assessed News states"));
    }
    if news_assessed(Some("not_assessed"))
        || news_assessed(None)
        || news_assessed(Some("corrupted"))
    {
        return Err(failure("not_assessed/None/corrupted must not be assessed"));
    }
    if news_status_label(Some("partial")) != "partial" {
        return Err(failure("partial keeps its own label"));
    }

    let mut partial = fake_delivery_result("partial");
    partial["news_result"] = json!({
        "status": "partial",
        "warning_count": 3,
        "warnings": ["a", "b", "c"],
        "overall_direction": "mixed",
        "expected_volatility_regime": {"regime": "elevated"},
    });
    let line = line_text(&required_response_lines(&partial), "news");
    for expected in ["已采集", "3 条警告", "AI逐条未评估"] {
        if !line.contains(expected) {
            return Err(failure(format!(
                "collection and AI assessment must have distinct statuses: {line} (missing {expected})"
            )));
        }
    }
    if line.contains("未提供") {
        return Err(failure(format!(
            "collected evidence must not be described as absent: {line}"
        )));
    }
    let text = render_final_response(&partial);
    if text.contains("新闻：未提供") {
        return Err(failure(
            "a collected run must not be described as absent".to_string(),
        ));
    }
    validate_response_text(&partial, &text)?;

    let absent = fake_delivery_result("not_assessed");
    let absent_line = line_text(&required_response_lines(&absent), "news");
    if !absent_line.contains("未提供") {
        return Err(failure(format!(
            "a genuinely unassessed News stage must say so: {absent_line}"
        )));
    }
    validate_response_text(&absent, &render_final_response(&absent))?;
    Ok(())
}

/// T26: a denial is a fact, not missing evidence; "No. 1" is not a negation.
pub fn legacy_t26() -> Result<(), String> {
    if direction_for("Company denies the rumour") != "neutral" {
        return Err(failure(
            "a denial of a rumour is a factual statement, not unknown".to_string(),
        ));
    }
    if direction_for("No. 1 vendor in the segment") == "unknown" {
        return Err(failure(
            "a bare 'No.' must not be read as negation or uncertainty".to_string(),
        ));
    }
    if direction_for("Company may miss estimates") != "unknown" {
        return Err(failure("a genuine hedge must stay unknown".to_string()));
    }
    if direction_for("Reports unconfirmed bid approach") != "unknown" {
        return Err(failure(
            "an unconfirmed rumour must stay unknown".to_string(),
        ));
    }
    if direction_for("MU misses estimates as demand weak") != "negative" {
        return Err(failure(
            "confirmed negative news must stay negative".to_string(),
        ));
    }
    Ok(())
}

/// T27: timing bucket, non-directional volatility state, priced-in state.
pub fn legacy_t27() -> Result<(), String> {
    let mut closes_map = Map::new();
    closes_map.insert(
        "2026-01-05".into(),
        Value::String("2026-01-05T20:00:00Z".into()),
    );
    let bucket = |published: Option<&str>| -> Result<String, String> {
        let published_value = published.map(|value| Value::String(value.to_string()));
        let result = event_time_bucket(
            published_value.as_ref(),
            Some(&Value::String("2026-01-05".into())),
            Some(&closes_map),
        );
        result
            .get("bucket")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .ok_or_else(|| failure(format!("bucket missing: {result}")))
    };
    if bucket(Some("2026-01-05T10:00:00Z"))? != "pre_market" {
        return Err(failure("10:00 should be pre_market".to_string()));
    }
    if bucket(Some("2026-01-05T15:00:00Z"))? != "intraday" {
        return Err(failure("15:00 should be intraday".to_string()));
    }
    if bucket(Some("2026-01-05T22:00:00Z"))? != "after_close" {
        return Err(failure("22:00 should be after_close".to_string()));
    }
    if bucket(None)? != "unknown" {
        return Err(failure("missing publication time is unknown".to_string()));
    }

    // 25 quiet sessions, a +2% event session, then a flat continuation window.
    let mut closes: Vec<f64> = vec![100.0];
    for index in 0..24 {
        let last = *closes.last().expect("non-empty");
        closes.push(last * (1.0 + if index % 2 == 0 { -0.001 } else { 0.001 }));
    }
    let event_close = *closes.last().expect("non-empty") * 1.02;
    closes.push(event_close);
    for _ in 0..6 {
        closes.push(event_close);
    }
    let reaction = json!({"status": "confirmed_positive", "daily_close_return": 0.02});
    let priced_in = priced_in_assessment(&reaction, &closes, Some(25), "positive", 5, 1.0);
    if priced_in.get("state").and_then(Value::as_str) != Some("likely_priced_in") {
        return Err(failure(format!("expected likely_priced_in: {priced_in}")));
    }
    if priced_in.get("probability_effect").and_then(Value::as_str) != Some("none") {
        return Err(failure(
            "priced-in must have no probability effect".to_string(),
        ));
    }
    let early = priced_in_assessment(&reaction, &closes[..28], Some(25), "positive", 5, 1.0);
    if early.get("state").and_then(Value::as_str) != Some("not_assessable") {
        return Err(failure(format!(
            "an unmatured continuation window must be not_assessable: {early}"
        )));
    }
    let cards = vec![
        json!({"already_priced_in": priced_in.clone()}),
        json!({"already_priced_in": priced_in.clone()}),
        json!({"already_priced_in": priced_in}),
    ];
    let aggregate = aggregate_priced_in(&cards);
    if aggregate.get("state").and_then(Value::as_str) != Some("likely_priced_in") {
        return Err(failure(format!(
            "three priced-in events aggregate to likely_priced_in: {aggregate}"
        )));
    }
    Ok(())
}

/// T28: a supplied headline/source/url must match the analysed record.
pub fn legacy_t28() -> Result<(), String> {
    let bars = synth_bars(60, 7);
    let input = news_input(&bars, f17_articles(&bars));
    let baseline = analysis(&input, None)?;
    let candidates = candidate_rows(&baseline);
    let candidate = candidates
        .first()
        .ok_or_else(|| failure("baseline must offer AI lean candidates".to_string()))?;
    let news_id = candidate
        .get("news_id")
        .and_then(Value::as_str)
        .ok_or_else(|| failure("candidate news_id missing".to_string()))?;
    let headline = candidate
        .get("headline")
        .and_then(Value::as_str)
        .ok_or_else(|| failure("candidate headline missing".to_string()))?;
    let forged = ai_artifact(
        &input,
        vec![json!({
            "news_id": news_id,
            "lean": "bullish",
            "reason": "标题上调",
            "headline": "Forged headline that was never published",
        })],
    );
    let rejected = analysis(&input, Some(&forged))?;
    if rejected["ai_lean"]["status"].as_str() != Some("invalid") {
        return Err(failure(format!(
            "a forged headline must reject the artifact: {}",
            rejected["ai_lean"]
        )));
    }
    let good = ai_artifact(
        &input,
        vec![json!({
            "news_id": news_id,
            "lean": "bullish",
            "reason": "标题上调",
            "headline": headline,
        })],
    );
    let accepted = analysis(&input, Some(&good))?;
    if accepted["ai_lean"]["status"].as_str() != Some("partial") {
        return Err(failure(format!(
            "a copied headline is accepted and partial: {}",
            accepted["ai_lean"]
        )));
    }
    Ok(())
}

/// T35: every canonical headline is offered to AI; direct counts and major links are
/// auditable.
pub fn legacy_t35() -> Result<(), String> {
    let bars = synth_bars(60, 7);
    let mut articles = f17_articles(&bars);
    let last = bars
        .last()
        .and_then(|bar| bar["date"].as_str())
        .unwrap_or("");
    articles.push(json!({
        "article_id": "macro-1", "title": "Fed rate decision this week",
        "url": "https://example.com/macro", "source": "Wire", "source_type": "news",
        "record_type": "unknown", "published_at": format!("{last}T14:00:00Z"),
        "tickers": [], "summary": null,
    }));
    articles.push(json!({
        "article_id": "roundup-1", "title": "Weekly market roundup",
        "url": "https://example.com/roundup", "source": "Wire", "source_type": "news",
        "record_type": "unknown", "published_at": format!("{last}T15:00:00Z"),
        "tickers": [], "summary": null,
    }));
    let input = news_input(&bars, articles);
    let baseline = analysis(&input, None)?;
    let candidates = candidate_rows(&baseline);
    if !candidates
        .iter()
        .any(|item| item.get("record_kind").and_then(Value::as_str) == Some("unclassified"))
    {
        return Err(failure(
            "unclassified headlines must still be offered to the AI lean stage".to_string(),
        ));
    }
    let mut items: Vec<Value> = Vec::new();
    for candidate in &candidates {
        let headline = candidate
            .get("headline")
            .and_then(Value::as_str)
            .unwrap_or("");
        let (lean, relevance, importance) = if headline.contains("raises guidance") {
            ("bullish", "direct", "major")
        } else if headline.contains("misses estimates") {
            ("bearish", "direct", "major")
        } else if headline.contains("Fed rate") {
            ("bullish", "indirect", "major")
        } else {
            ("neutral", "unclear", "routine")
        };
        items.push(json!({
            "news_id": candidate.get("news_id").cloned().unwrap_or(Value::Null),
            "lean": lean,
            "reason": "标题直接陈述该事项",
            "target_relevance": relevance,
            "importance": importance,
            "evidence_grade": "headline_only",
        }));
    }
    let artifact = ai_artifact(&input, items);
    let assessed = analysis(&input, Some(&artifact))?;
    let ai = &assessed["ai_lean"];
    if ai["status"].as_str() != Some("assessed") {
        return Err(failure(format!("every candidate assessed: {ai}")));
    }
    if ai["coverage"]["assessed_count"].as_i64() != Some(candidates.len() as i64) {
        return Err(failure(format!(
            "assessed count must cover every candidate: {ai}"
        )));
    }
    let counts = &ai["coverage"]["direct_label_counts"];
    if counts["bullish"].as_i64() != Some(1) || counts["bearish"].as_i64() != Some(1) {
        return Err(failure(format!("direct label counts: {counts}")));
    }
    if ai["major_direct_items"].as_array().map(Vec::len) != Some(2) {
        return Err(failure(format!(
            "two major direct items expected: {}",
            ai["major_direct_items"]
        )));
    }
    require_ai_news_coverage(&assessed)?;
    if require_ai_news_coverage(&baseline).is_ok() {
        return Err(failure(
            "delivery must reject collected headlines without AI assessment".to_string(),
        ));
    }
    let mut result = fake_delivery_result("complete");
    result["news_result"] = assessed;
    let line = line_text(&required_response_lines(&result), "news");
    if !(line.contains("利多1条") && line.contains("利空1条") && line.contains("AI逐条已完成"))
    {
        return Err(failure(format!(
            "the news line must disclose AI counts: {line}"
        )));
    }
    let final_response = render_final_response(&result);
    let report = render_markdown(&result);
    if !final_response.contains("https://example.com/a1")
        || !final_response.contains("https://example.com/a2")
    {
        return Err(failure(
            "major direct links must appear in the final response".to_string(),
        ));
    }
    if final_response.contains("https://example.com/macro") {
        return Err(failure(
            "indirect items must not appear in the final response".to_string(),
        ));
    }
    if !report.contains("https://example.com/macro") {
        return Err(failure(
            "the markdown report must list every assessed item".to_string(),
        ));
    }
    Ok(())
}

/// T41: partial coverage is deliverable only with reconciling counts and a warning.
pub fn legacy_t41() -> Result<(), String> {
    let candidates = vec![json!({"news_id": "n1"}), json!({"news_id": "n2"})];
    let news = |status: &str, assessed: usize, items: usize, warnings: Vec<&str>| -> Value {
        json!({
            "ai_lean_candidates": candidates,
            "warnings": warnings,
            "ai_lean": {
                "status": status,
                "coverage": {"candidate_count": 2, "assessed_count": assessed},
                "items": (0..items).map(|index| json!({"news_id": format!("n{}", index + 1)})).collect::<Vec<_>>(),
            },
        })
    };
    require_ai_news_coverage(&news("assessed", 2, 2, vec![]))?;
    require_ai_news_coverage(&news("partial", 1, 1, vec!["1 candidate not assessed"]))?;
    for (case, label) in [
        (
            news("partial", 1, 1, vec![]),
            "partial without a disclosed reason",
        ),
        (
            news("assessed", 1, 1, vec![]),
            "assessed with a missing item",
        ),
        (news("not_assessed", 0, 0, vec![]), "unassessed artifact"),
        (
            news("partial", 3, 3, vec!["x"]),
            "assessed count above the candidate count",
        ),
    ] {
        if require_ai_news_coverage(&case).is_ok() {
            return Err(failure(format!("{label} was accepted")));
        }
    }
    Ok(())
}

/// T42: truncation is disclosed without pretending the evidence set shrank.
pub fn legacy_t42() -> Result<(), String> {
    let warnings = vec![
        "12 news headline(s) were truncated to 400 characters".to_string(),
        "3 news record(s) were dropped before normalization".to_string(),
    ];
    let details = warning_details(&warnings);
    let details = details.as_array().cloned().unwrap_or_default();
    if details.len() < 2
        || details[0].get("severity").and_then(Value::as_str) != Some("presentation")
        || details[1].get("severity").and_then(Value::as_str) != Some("evidence_affecting")
    {
        return Err(failure(format!("warning severities: {details:?}")));
    }
    let counts = warning_severity_counts(&warnings);
    if counts.get("evidence_affecting").and_then(Value::as_i64) != Some(1)
        || counts.get("presentation").and_then(Value::as_i64) != Some(1)
    {
        return Err(failure(format!("warning severity counts: {counts:?}")));
    }
    let truncation_only = warning_severity_counts(&[
        "3 news headline(s) were truncated to 400 characters".to_string(),
    ]);
    if truncation_only
        .get("evidence_affecting")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        != 0
    {
        return Err(failure(
            "a truncation-only warning must not degrade the status".to_string(),
        ));
    }

    // Observable behaviour of the private `finalize_warnings`: presentation-only
    // warnings keep the assessed status complete.
    let bars = synth_bars(60, 7);
    let last = bars
        .last()
        .and_then(|bar| bar["date"].as_str())
        .unwrap_or("");
    let long_title = format!("MU raises guidance on strong demand {}", "x".repeat(420));
    let articles = vec![json!({
        "article_id": "long-1", "title": long_title, "url": "https://example.com/long",
        "source": "Wire", "source_type": "news", "record_type": "news",
        "published_at": format!("{last}T13:00:00Z"), "tickers": ["MU"], "summary": null,
    })];
    let presentation = analysis(&news_input(&bars, articles), None)?;
    if presentation["status"].as_str() != Some("complete") {
        return Err(failure(format!(
            "presentation-only warnings must keep status complete: {}",
            presentation["status"]
        )));
    }
    if !presentation["warnings"]
        .as_array()
        .map(|warnings| {
            warnings.iter().any(|warning| {
                warning
                    .as_str()
                    .map(|text| text.contains("truncated"))
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false)
    {
        return Err(failure(
            "the truncation must be disclosed as a warning".to_string(),
        ));
    }

    // An unassessed canonical headline is evidence-affecting and degrades the status.
    let f17 = analysis(&news_input(&bars.clone(), f17_articles(&bars)), None)?;
    let first_candidate = candidate_rows(&f17)
        .first()
        .cloned()
        .ok_or_else(|| failure("candidates expected".to_string()))?;
    let partial_artifact = ai_artifact(
        &news_input(&bars, f17_articles(&bars)),
        vec![json!({
            "news_id": first_candidate["news_id"],
            "lean": "neutral",
            "reason": "x",
        })],
    );
    let degraded = analysis(
        &news_input(&bars, f17_articles(&bars)),
        Some(&partial_artifact),
    )?;
    if degraded["status"].as_str() != Some("partial") {
        return Err(failure(format!(
            "unassessed canonical headlines must degrade the assessed status: {}",
            degraded["status"]
        )));
    }
    Ok(())
}

/// F17: the AI lean is display-only and can never change a deterministic News field.
pub fn legacy_f17() -> Result<(), String> {
    let bars = synth_bars(60, 7);
    let input = news_input(&bars, f17_articles(&bars));
    let baseline = analysis(&input, None)?;
    let risk_ids = ids(
        baseline
            .get("risks")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]),
        "event_id",
    );
    let mixed_ids = ids(
        baseline
            .get("mixed_events")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]),
        "event_id",
    );
    if risk_ids.iter().any(|id| mixed_ids.contains(id)) {
        return Err(failure(
            "mixed-direction events must not be reported as bearish risks".to_string(),
        ));
    }
    if mixed_ids.is_empty() {
        return Err(failure(
            "the conflicting-headline cluster should be reported as mixed".to_string(),
        ));
    }
    if baseline["method"]["news_probability"].as_str() != Some("not_emitted") {
        return Err(failure("News must not emit a probability".to_string()));
    }
    if baseline["method"]["calibrated"].as_bool() != Some(false) {
        return Err(failure("News must not be calibrated".to_string()));
    }

    let candidates = candidate_rows(&baseline);
    let candidate_ids: Vec<String> = ids(&candidates, "news_id");
    let mut unique_ids = candidate_ids.clone();
    unique_ids.sort();
    unique_ids.dedup();
    if unique_ids.len() != candidates.len() || candidates.len() < 3 {
        return Err(failure(format!(
            "canonical candidate ids must be unique and >=3: {}",
            candidates.len()
        )));
    }
    let pregated: Vec<&Value> = candidates
        .iter()
        .filter(|row| row.get("eligible_evidence") == Some(&Value::Bool(false)))
        .collect();
    if pregated.is_empty() {
        return Err(failure(
            "a record failing the relevance gate must still be offered to the lean stage"
                .to_string(),
        ));
    }
    let bullish_id = candidates
        .iter()
        .find(|row| row.get("direction").and_then(Value::as_str) == Some("positive"))
        .and_then(|row| row.get("news_id").and_then(Value::as_str))
        .ok_or_else(|| failure("a positive candidate must exist".to_string()))?
        .to_string();
    let bearish_id = candidates
        .iter()
        .find(|row| row.get("direction").and_then(Value::as_str) == Some("negative"))
        .and_then(|row| row.get("news_id").and_then(Value::as_str))
        .ok_or_else(|| failure("a negative candidate must exist".to_string()))?
        .to_string();
    let pregated_id = pregated[0]
        .get("news_id")
        .and_then(Value::as_str)
        .ok_or_else(|| failure("pregated candidate id missing".to_string()))?
        .to_string();

    let good = ai_artifact(
        &input,
        vec![
            json!({"news_id": bullish_id, "lean": "bullish", "reason": "标题称指引上调",
                "evidence_grade": "reported_event"}),
            json!({"news_id": bearish_id, "lean": "bearish", "reason": "标题称不及预期",
                "evidence_grade": "reported_event"}),
            json!({"news_id": pregated_id, "lean": "unclear", "reason": "标题未点明标的",
                "evidence_grade": "headline_only", "relevance_basis": "not_explicitly_matched"}),
        ],
    );
    let scored = analysis(&input, Some(&good))?;
    let lean = &scored["ai_lean"];
    if lean["status"].as_str() != Some("partial") {
        return Err(failure(format!("partial lean expected: {lean}")));
    }
    if ids(
        lean["bullish"].as_array().map(Vec::as_slice).unwrap_or(&[]),
        "news_id",
    ) != vec![bullish_id.clone()]
    {
        return Err(failure(
            "bullish bucket must contain the judged id".to_string(),
        ));
    }
    if ids(
        lean["bearish"].as_array().map(Vec::as_slice).unwrap_or(&[]),
        "news_id",
    ) != vec![bearish_id.clone()]
    {
        return Err(failure(
            "bearish bucket must contain the judged id".to_string(),
        ));
    }
    if lean["bullish"][0]["reason"].as_str() != Some("标题称指引上调") {
        return Err(failure("the lean reason must be preserved".to_string()));
    }
    if lean["display_only"].as_bool() != Some(true) {
        return Err(failure("the lean must be display-only".to_string()));
    }
    if lean["coverage"]["candidate_count"].as_i64() != Some(candidates.len() as i64)
        || lean["coverage"]["assessed_count"].as_i64() != Some(3)
    {
        return Err(failure(format!("lean coverage: {}", lean["coverage"])));
    }
    assert_core_unchanged(&scored, &baseline, "lean")?;
    let scored_directions: Vec<Value> = scored["major_events"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|row| row.get("direction").cloned().unwrap_or(Value::Null))
        .collect();
    let baseline_directions: Vec<Value> = baseline["major_events"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|row| row.get("direction").cloned().unwrap_or(Value::Null))
        .collect();
    if scored_directions != baseline_directions {
        return Err(failure(
            "the lean must not change major-event directions".to_string(),
        ));
    }
    let scored_risk_ids = ids(
        scored["risks"].as_array().map(Vec::as_slice).unwrap_or(&[]),
        "event_id",
    );
    if scored_risk_ids != risk_ids {
        return Err(failure("the lean must not change the risk set".to_string()));
    }
    let attached = scored["major_events"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .find(|row| row.get("event_id").and_then(Value::as_str) == Some(&bullish_id))
        .ok_or_else(|| failure("the bullish event must stay in major_events".to_string()))?;
    if attached.get("ai_lean").and_then(Value::as_str) != Some("bullish")
        || attached.get("direction").and_then(Value::as_str) != Some("positive")
    {
        return Err(failure(format!(
            "attachment must preserve the deterministic direction: {attached}"
        )));
    }

    let rejections = [
        (
            "unknown news id",
            ai_artifact(
                &input,
                vec![json!({"news_id": "event_deadbeef", "lean": "bullish", "reason": "x"})],
            ),
        ),
        (
            "quant vocabulary",
            ai_artifact(
                &input,
                vec![
                    json!({"news_id": bullish_id, "lean": "bullish", "reason": "prob_up should rise"}),
                ],
            ),
        ),
        ("identity mismatch", {
            let mut artifact = ai_artifact(
                &input,
                vec![json!({"news_id": bullish_id, "lean": "bullish", "reason": "x"})],
            );
            artifact["ticker"] = Value::String("NVDA".into());
            artifact
        }),
        (
            "unsupported field",
            ai_artifact(
                &input,
                vec![
                    json!({"news_id": bullish_id, "lean": "bullish", "reason": "x", "probability": 0.9}),
                ],
            ),
        ),
        (
            "bad label",
            ai_artifact(
                &input,
                vec![json!({"news_id": bullish_id, "lean": "very bullish", "reason": "x"})],
            ),
        ),
    ];
    for (name, bad) in rejections {
        let rejected = analysis(&input, Some(&bad))?;
        if rejected["ai_lean"]["status"].as_str() != Some("invalid") {
            return Err(failure(format!("{name} should be rejected")));
        }
        assert_core_unchanged(&rejected, &baseline, name)?;
        let has_rejection_warning = rejected["warnings"]
            .as_array()
            .map(|warnings| {
                warnings.iter().any(|warning| {
                    warning
                        .as_str()
                        .map(|text| text.contains("lean artifact rejected"))
                        .unwrap_or(false)
                })
            })
            .unwrap_or(false);
        if !has_rejection_warning {
            return Err(failure(format!(
                "{name} must leave an explicit rejection warning"
            )));
        }
    }

    let absent = analysis(&input, None)?;
    if absent["ai_lean"]["status"].as_str() != Some("not_assessed") {
        return Err(failure("absent AI artifact is not_assessed".to_string()));
    }
    if absent["ai_lean"]["bullish"].as_array().map(Vec::len) != Some(0)
        || absent["ai_lean"]["bearish"].as_array().map(Vec::len) != Some(0)
    {
        return Err(failure("absent lean has empty buckets".to_string()));
    }
    if !absent["ai_lean"]["probability_effect"]
        .as_str()
        .map(|value| value.starts_with("none"))
        .unwrap_or(false)
    {
        return Err(failure("absent lean has no probability effect".to_string()));
    }
    if absent["status"] != baseline["status"] {
        return Err(failure(
            "absent lean must not change the News status".to_string(),
        ));
    }

    let empty_input = news_input(&bars, Vec::new());
    let empty_result = analysis(
        &empty_input,
        Some(&ai_artifact(
            &empty_input,
            vec![json!({"news_id": "event_x", "lean": "bullish", "reason": "x"})],
        )),
    )?;
    if empty_result["ai_lean"]["status"].as_str() != Some("invalid") {
        return Err(failure(
            "an artifact against an empty candidate set must be rejected".to_string(),
        ));
    }
    Ok(())
}

/// Implemented R07 checks (T25/T35/T41 use the temporary delivery mirrors; see module docs).
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T25", legacy_t25 as CheckFn),
        ("T26", legacy_t26 as CheckFn),
        ("T27", legacy_t27 as CheckFn),
        ("T28", legacy_t28 as CheckFn),
        ("T35", legacy_t35 as CheckFn),
        ("T41", legacy_t41 as CheckFn),
        ("T42", legacy_t42 as CheckFn),
        ("F17", legacy_f17 as CheckFn),
    ]
}
