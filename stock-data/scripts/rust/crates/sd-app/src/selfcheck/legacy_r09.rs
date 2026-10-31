//! Migrated R09 checks: forward ledger, prospective/retrospective classification,
//! prompt recording and version isolation.
//!
//! All wall clock values are injected: the ledger module never reads the clock itself, so
//! these checks are deterministic. Every check uses a temporary directory and deletes it.

use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc};
use serde_json::{json, Map, Value};

use sd_core::json_text::canonical_text;
use sd_storage::ledger::{
    append_event, audit, prediction_id, prospective_records, read_events, record_prediction,
    settle_pending, RecordOptions, PREDICTION, SCHEMA_VERSION,
};

use crate::selfcheck::registry::CheckFn;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

fn scratch_dir(label: &str) -> std::path::PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-selfcheck-r09-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch dir");
    directory
}

fn payload(decision: &str, asof: &str, ticker: &str) -> Value {
    json!({
        "snapshot": {
            "ticker": ticker,
            "market": "US",
            "horizon": "5D",
            "asof_timestamp": asof,
            "snapshot_id": format!("S-{decision}"),
            "snapshot_content_sha256": format!("{}{}", decision, "a".repeat(20)).chars().take(64).collect::<String>(),
        },
        "consensus": {
            "prob_up": 0.6,
            "raw_probability": 0.6,
            "decision_sampled": true,
            "sampling": {"status": "anchored"},
            "calibrated_probability": null,
            "probability_basis": "drift_shrunk_causal_base_rate",
            "direction": "bullish",
            "confidence": "low",
            "confidence_score": 50,
            "predictive_evidence_grade": "insufficient_oos_evidence",
            "eligibility": {"probability_report": true, "direction_report": true,
                            "trading_signal": false, "trading_signal_reasons": []},
            "timing": {"decision_session": decision, "decision_at": format!("{decision}T20:00:00Z"),
                       "label_basis": "close_to_close", "entry_basis": "not_modelled"},
        },
        "engine": {"version": "test"},
    })
}

fn weekday_bars(count: usize, start: NaiveDate) -> Vec<Value> {
    let mut bars: Vec<Value> = Vec::new();
    let mut cursor = start;
    while bars.len() < count {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            bars.push(json!({
                "date": cursor.to_string(),
                "close": 100.0 + bars.len() as f64 * 2.0,
            }));
        }
        cursor += Duration::days(1);
    }
    bars
}

fn options<'a>(bars: Option<&'a [Value]>, import_reason: Option<&'a str>) -> RecordOptions<'a> {
    RecordOptions {
        import_reason,
        bars,
        sessions: None,
        snapshot_dir: None,
    }
}

/// F39: the forward ledger records once, settles once and never mixes instruments.
pub fn legacy_f39() -> Result<(), String> {
    let work = scratch_dir("f39");
    let path = work.join("ledger.jsonl");
    let result = payload("2026-01-02", "2026-01-02T21:00:00Z", "SYN");
    let recorded_at = Utc::now();
    let first = record_prediction(&result, &path, recorded_at, options(None, None)).expect("first");
    let second =
        record_prediction(&result, &path, recorded_at, options(None, None)).expect("second");
    if first["status"] != "recorded" || second["status"] != "duplicate" {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a retried analysis must not double-record the forecast",
        ));
    }
    let events = read_events(&path).expect("events");
    if events.len() != 1 || events[0]["status"] != "pending" {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "the ledger must hold one immutable pending prediction",
        ));
    }
    let bars = weekday_bars(10, NaiveDate::from_ymd_opt(2026, 1, 2).expect("date"));
    let settled_at = Utc::now();
    let (settlements, skipped) =
        settle_pending(&events, &bars, "SYN", "US", None, None, None, settled_at).expect("settle");
    if !skipped.is_empty() || settlements.len() != 1 {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "one prediction should settle cleanly: {skipped:?}"
        )));
    }
    let settlement = &settlements[0];
    let hit = settlement["hit"].as_i64();
    let brier = settlement["brier"].as_f64().unwrap_or(f64::NAN);
    if hit != Some(1) || (brier - 0.16).abs() > 1e-12 {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("settlement must use probability minus outcome"));
    }
    for event in &settlements {
        append_event(&path, event).expect("append settlement");
    }
    let (again, _) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("settle again");
    if !again.is_empty() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("settlement must be idempotent"));
    }
    let report = audit(&path, NaiveDate::from_ymd_opt(2026, 2, 1).expect("today")).expect("audit");
    if report["settled_predictions"].as_i64() != Some(1)
        || report["pending_predictions"].as_i64() != Some(0)
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("audit accounting is wrong: {report}")));
    }
    let retrospective_brier = report["retrospective"]["brier"]
        .as_f64()
        .unwrap_or(f64::NAN);
    if (retrospective_brier - 0.16).abs() > 1e-9 {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "audit must recompute Brier from the recorded probability",
        ));
    }
    if report["prospective"]["samples"].as_i64() != Some(0) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a snapshot recorded after its label matured is not prospective",
        ));
    }
    let mut abstained = result.clone();
    abstained["consensus"]["prob_up"] = Value::Null;
    abstained["snapshot"]["asof_timestamp"] = Value::String("2026-01-03T21:00:00Z".into());
    abstained["consensus"]["timing"]["decision_session"] = Value::String("2026-01-05".into());
    let recorded = record_prediction(&abstained, &path, recorded_at, options(None, None))
        .expect("abstained record");
    if recorded["status"] != "recorded" {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("an abstention must still enter the ledger"));
    }
    let latest = read_events(&path).expect("events");
    let latest = latest.last().cloned().unwrap_or(Value::Null);
    if latest["status"] != "abstained" {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "an abstention must be labelled, not silently dropped",
        ));
    }
    let (_, skipped) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("settle abstained");
    let skipped_reasons: Vec<String> = skipped
        .iter()
        .filter_map(|item| {
            item.get("reason")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .collect();
    if !skipped_reasons
        .iter()
        .any(|reason| reason == "prediction_abstained")
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("an abstention has no outcome to settle"));
    }
    let mut other = result.clone();
    other["snapshot"]["ticker"] = Value::String("OTHER".into());
    other["snapshot"]["snapshot_content_sha256"] = Value::String("b".repeat(64));
    other["snapshot"]["asof_timestamp"] = Value::String("2026-01-04T21:00:00Z".into());
    if record_prediction(&other, &path, recorded_at, options(None, None)).expect("other")["status"]
        != "recorded"
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("the second instrument must enter the same ledger"));
    }
    let (_, mixed) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("settle mixed");
    if !mixed
        .iter()
        .any(|item| item["reason"].as_str() == Some("ticker_mismatch"))
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "bars for SYN must never settle an OTHER prediction",
        ));
    }
    let _ = std::fs::remove_dir_all(&work);
    Ok(())
}

/// F40: retrospective metrics must use the realised base rate and a causal baseline.
pub fn legacy_f40() -> Result<(), String> {
    let work = scratch_dir("f40");
    let path = work.join("ledger.jsonl");
    let start = NaiveDate::from_ymd_opt(2026, 1, 1).expect("start");
    let mut bars: Vec<Value> = Vec::new();
    for offset in 1..60 {
        let day = start + Duration::days(offset);
        if day.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            bars.push(json!({"date": day.to_string(), "close": 100.0 + offset as f64}));
        }
    }
    if bars.len() < 30 {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("not enough weekday bars"));
    }
    let recorded_at = Utc::now();
    for offset in 0..4usize {
        let decision = bars[20 + offset]["date"].as_str().unwrap_or("").to_string();
        let snapshot_sha = format!("{offset:064}");
        let mut result = payload(&decision, &format!("{decision}T21:00:00Z"), "SYN");
        result["consensus"]["prob_up"] = Value::from(0.1);
        result["snapshot"]["snapshot_id"] = Value::String(format!("S{offset}"));
        result["snapshot"]["snapshot_content_sha256"] = Value::String(snapshot_sha);
        let fitter = RecordOptions {
            import_reason: None,
            bars: Some(&bars),
            sessions: None,
            snapshot_dir: None,
        };
        if record_prediction(&result, &path, recorded_at, fitter).expect("record")["status"]
            != "recorded"
        {
            let _ = std::fs::remove_dir_all(&work);
            return Err(failure(format!("record {offset} was not recorded")));
        }
    }
    let settled_at = Utc::now();
    let (appended, _) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("settle");
    for event in &appended {
        append_event(&path, event).expect("append");
    }
    let report = audit(&path, NaiveDate::from_ymd_opt(2026, 2, 1).expect("today")).expect("audit");
    if report["prospective"]["samples"].as_i64() != Some(0)
        || report["retrospective"]["samples"].as_i64() != Some(4)
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("a past snapshot must stay retrospective"));
    }
    let metrics = &report["retrospective"];
    if metrics["observed_up_rate"].as_f64() != Some(1.0) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "observed_up_rate must be the realised label base rate",
        ));
    }
    if metrics["direction_accuracy"].as_f64() != Some(0.0) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "four confident down calls on four up labels are zero accuracy",
        ));
    }
    if metrics.get("hit_rate").is_some() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("hit_rate must not be used for a base-rate field"));
    }
    if metrics["causal_baseline_samples"].as_i64() != Some(4) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("every settled row needs a causal baseline"));
    }
    for record in &appended {
        if record.get("baseline_probability").and_then(Value::as_f64) != Some(1.0) {
            let _ = std::fs::remove_dir_all(&work);
            return Err(failure(
                "the causal baseline may only use labels matured by the decision",
            ));
        }
        if record.get("baseline_frozen_at").and_then(Value::as_str) != Some("prediction") {
            let _ = std::fs::remove_dir_all(&work);
            return Err(failure(
                "a baseline computed at recording time must be frozen there",
            ));
        }
    }
    let _ = std::fs::remove_dir_all(&work);
    Ok(())
}

/// T15: the same decision is recorded exactly once.
pub fn legacy_t15() -> Result<(), String> {
    let work = scratch_dir("t15");
    let path = work.join("ledger.jsonl");
    let result = payload("2026-01-02", "2026-01-02T21:00:00Z", "SYN");
    let recorded_at = Utc::now();
    let first = record_prediction(&result, &path, recorded_at, options(None, None)).expect("first");
    let second =
        record_prediction(&result, &path, recorded_at, options(None, None)).expect("second");
    let _ = std::fs::remove_dir_all(&work);
    if first["status"] != "recorded" || second["status"] != "duplicate" {
        return Err(failure(format!("first={first} second={second}")));
    }
    Ok(())
}

/// T22: prospective rows are promptly recorded; backfilled snapshots stay retrospective.
pub fn legacy_t22() -> Result<(), String> {
    let work = scratch_dir("t22");
    let path = work.join("forward.jsonl");
    let result = payload("2026-01-02", "2026-01-02T21:00:00Z", "SYN");
    let decision_at = DateTime::parse_from_rfc3339("2026-01-02T20:00:00Z")
        .expect("decision")
        .with_timezone(&Utc);
    let forward = record_prediction(
        &result,
        &path,
        decision_at + Duration::hours(1),
        options(None, None),
    )
    .expect("forward");
    if forward["prospective_eligible"].as_bool() != Some(true) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "a record inside the prompt window must be prospective: {forward}"
        )));
    }
    let old = payload("2023-01-03", "2023-01-03T20:00:00Z", "SYN");
    let backfilled = record_prediction(&old, &path, Utc::now(), options(None, Some("historical")))
        .expect("backfill");
    if backfilled["prospective_eligible"].as_bool() != Some(false) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a historical import must not enter the forward score",
        ));
    }
    let prospective = prospective_records(&path, None, None).expect("prospective");
    if prospective
        .iter()
        .any(|row| row["prediction_id"] == prediction_id(&old))
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "the backfilled snapshot leaked into forward evidence",
        ));
    }
    let _ = std::fs::remove_dir_all(&work);
    Ok(())
}

/// F44: prospective means recorded promptly and before the label matured.
pub fn legacy_f44() -> Result<(), String> {
    let work = scratch_dir("f44");
    let path = work.join("ledger.jsonl");
    let today = Utc::now().date_naive();
    let mut session = today - Duration::days(1);
    while session.weekday().num_days_from_monday() >= 5 {
        session -= Duration::days(1);
    }
    let decision_at = session.and_time(chrono::NaiveTime::from_hms_opt(20, 0, 0).expect("time"));
    let decision_at = DateTime::<Utc>::from_naive_utc_and_offset(decision_at, Utc);
    let live = payload(&session.to_string(), &format!("{session}T20:00:00Z"), "SYN");
    let mut live = live;
    live["snapshot"]["snapshot_id"] = Value::String("LIVE".into());
    live["snapshot"]["snapshot_content_sha256"] = Value::String("a".repeat(64));
    let recorded = record_prediction(
        &live,
        &path,
        decision_at + Duration::hours(1),
        options(None, None),
    )
    .expect("prompt record");
    if recorded["prospective_eligible"].as_bool() != Some(true) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a prompt record made before the label matures must be prospective",
        ));
    }
    let mut late_session = session - Duration::days(1);
    while late_session.weekday().num_days_from_monday() >= 5 {
        late_session -= Duration::days(1);
    }
    let late_decision_at = DateTime::<Utc>::from_naive_utc_and_offset(
        late_session.and_time(chrono::NaiveTime::from_hms_opt(20, 0, 0).expect("time")),
        Utc,
    );
    let mut late = live.clone();
    late["snapshot"]["snapshot_content_sha256"] = Value::String("d".repeat(64));
    late["snapshot"]["asof_timestamp"] = Value::String(format!("{late_session}T20:00:00Z"));
    late["consensus"]["timing"] = json!({
        "decision_session": late_session.to_string(),
        "decision_at": format!("{late_session}T20:00:00Z"),
    });
    let late_record = record_prediction(
        &late,
        &path,
        late_decision_at + Duration::hours(13),
        options(None, None),
    )
    .expect("late record");
    if late_record["prospective_eligible"].as_bool() != Some(false) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a record written after the prompt window must not be prospective",
        ));
    }
    let late_id = prediction_id(&late);
    let events = read_events(&path).expect("events");
    let late_event = events
        .iter()
        .find(|event| event["event"] == PREDICTION && event["prediction_id"] == late_id);
    let Some(late_event) = late_event else {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("the late record must be in the ledger"));
    };
    if late_event["forward_eligibility"]["reason"].as_str() != Some("recorded_after_prompt_window")
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "late eligibility reason: {}",
            late_event["forward_eligibility"]
        )));
    }
    let mut old = live.clone();
    old["snapshot"]["snapshot_content_sha256"] = Value::String("c".repeat(64));
    old["snapshot"]["asof_timestamp"] = Value::String("2023-01-03T20:00:00Z".into());
    old["consensus"]["timing"] = json!({
        "decision_session": "2023-01-03",
        "decision_at": "2023-01-03T20:00:00Z",
    });
    let historical = record_prediction(&old, &path, Utc::now(), options(None, Some("backfill")))
        .expect("historical");
    if historical["prospective_eligible"].as_bool() != Some(false) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a historical import must not enter the forward score",
        ));
    }
    let old_id = prediction_id(&old);
    let events = read_events(&path).expect("events");
    let stored = events
        .iter()
        .find(|event| event["prediction_id"] == old_id)
        .cloned()
        .unwrap_or(Value::Null);
    if stored["import_reason"].as_str() != Some("backfill") {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("the import reason must be recorded"));
    }
    let _ = std::fs::remove_dir_all(&work);
    Ok(())
}

/// T39: late records and other model versions cannot become forward evidence.
pub fn legacy_t39() -> Result<(), String> {
    let work = scratch_dir("t39");
    let path = work.join("ledger.jsonl");
    let early = payload("2026-01-02", "2026-01-02T20:00:00Z", "SYN");
    let decision_at = DateTime::parse_from_rfc3339("2026-01-02T20:00:00Z")
        .expect("decision")
        .with_timezone(&Utc);
    let prompt = record_prediction(
        &early,
        &path,
        decision_at + Duration::hours(1),
        options(None, None),
    )
    .expect("prompt");
    if prompt["prospective_eligible"].as_bool() != Some(true) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a record inside the prompt window must be prospective",
        ));
    }
    let late = payload("2026-01-05", "2026-01-05T20:00:00Z", "SYN");
    let late = record_prediction(
        &late,
        &path,
        DateTime::parse_from_rfc3339("2026-01-06T09:00:00Z")
            .expect("late")
            .with_timezone(&Utc),
        options(None, None),
    )
    .expect("late");
    if late["prospective_eligible"].as_bool() != Some(false) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(
            "a record written after the prompt window must not be prospective",
        ));
    }
    let late_payload = payload("2026-01-05", "2026-01-05T20:00:00Z", "SYN");
    let late_id = prediction_id(&late_payload);
    let events = read_events(&path).expect("events");
    let late_event = events
        .iter()
        .find(|event| event["prediction_id"] == late_id)
        .cloned()
        .unwrap_or(Value::Null);
    if late_event["forward_eligibility"]["reason"].as_str() != Some("recorded_after_prompt_window")
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "late eligibility reason: {}",
            late_event["forward_eligibility"]
        )));
    }
    if late_event["import_reason"].as_str()
        != Some("recorded_after_prompt_window_not_forward_evidence")
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "late import reason: {}",
            late_event["import_reason"]
        )));
    }
    let days = [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ];
    let bars: Vec<Value> = days
        .iter()
        .enumerate()
        .map(|(index, day)| json!({"date": day, "close": 100.0 + index as f64 * 2.0}))
        .collect();
    let (settlements, _) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        Utc::now(),
    )
    .expect("settle");
    for event in &settlements {
        append_event(&path, event).expect("append");
    }
    // A row from another model version must not inherit this model's evidence.
    let source = read_events(&path)
        .expect("events")
        .into_iter()
        .find(|event| event["event"] == PREDICTION)
        .expect("source prediction");
    let mut forged = source.as_object().cloned().unwrap_or_else(Map::new);
    forged.remove("event_hash");
    forged.remove("prev_hash");
    forged.insert("prediction_id".into(), Value::String("other-model".into()));
    forged.insert(
        "model_version".into(),
        Value::String("quant_previous_model".into()),
    );
    append_event(&path, &Value::Object(forged)).expect("forged append");
    append_event(
        &path,
        &json!({"schema_version": SCHEMA_VERSION, "event": "settlement",
                "prediction_id": "other-model", "hit": 1,
                "recorded_at": "2026-01-10T00:00:00Z"}),
    )
    .expect("forged settlement");
    let rows = prospective_records(&path, None, None).expect("prospective");
    if rows.len() != 1 {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "only the prompt, same-version record may count: {rows:?}"
        )));
    }
    if rows[0]["evidence_scope"].as_str() != Some("prospective_prompt_recording_anchored") {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("unexpected evidence scope: {}", rows[0])));
    }
    if rows
        .iter()
        .any(|row| row["decision_session"].as_str() == Some("2026-01-05"))
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("the late record leaked into forward evidence"));
    }
    // Tampering with a chained row must fail the hash check.
    let text = std::fs::read_to_string(&path).expect("read ledger");
    let mut lines: Vec<String> = text.lines().map(ToString::to_string).collect();
    let mut modified: Value = serde_json::from_str(&lines[0]).expect("parse line");
    modified["prob_up"] = Value::from(0.99);
    lines[0] = canonical_text(&modified);
    let tampered = work.join("tampered.jsonl");
    std::fs::write(&tampered, format!("{}\n", lines.join("\n"))).expect("write tampered");
    let outcome = read_events(&tampered);
    let _ = std::fs::remove_dir_all(&work);
    match outcome {
        Err(error) if error.to_string().contains("hash") => Ok(()),
        Err(error) => Err(failure(format!("unexpected tamper error: {error}"))),
        Ok(_) => Err(failure(
            "a modified ledger row must fail the hash chain check",
        )),
    }
}

/// Implemented R09 checks.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T15", legacy_t15 as CheckFn),
        ("T22", legacy_t22 as CheckFn),
        ("F44", legacy_f44 as CheckFn),
        ("T39", legacy_t39 as CheckFn),
        ("F39", legacy_f39 as CheckFn),
        ("F40", legacy_f40 as CheckFn),
    ]
}
