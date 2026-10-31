//! Calendar policy: decision session, target session, coverage recomputation and
//! the three-state sampling phase.
//!
//! The synthetic schedules below encode the facts that matter and that a time-zone
//! conversion alone gets wrong: US daylight-saving changes (20:00Z vs 21:00Z close),
//! a half session, an exchange holiday, an HK closure cluster, and the A-CAL-01
//! completion boundary (continuous close vs HK closing-auction latest end vs
//! confirmable complete daily bar).

use chrono::{TimeZone, Utc};
use sd_core::calendar::{
    recompute_window_coverage, resolve_bar_axis, CalendarResource, CalendarSession,
    CompletionProvenance, CompletionSemantics,
};
use serde_json::json;

/// Minimal valid schema-2.0 provenance shared by the synthetic HK schedule.
fn hk_completion_provenance() -> CompletionProvenance {
    CompletionProvenance::from_json(&json!({
        "rule_version": "cas_latest_end_v1",
        "markets": [
            {"market": "HK", "calendar": "XHKG", "scheme": "CAS",
             "applies_from": "2016-07-25", "applies_to": null}
        ],
        "sources": [
            {"id": "hkex-trading-hours-securities",
             "url": "https://www.hkex.com.hk/Services/Trading-hours-and-Severe-Weather-Arrangements/Trading-Hours/Securities-Market?sc_lang=en",
             "retrieved_at_utc": "2026-10-31T16:51:17Z",
             "sha256": "92ecb6390bd1e767382f69631ed7443fcd2cda92f0069c298cd4ad32c678bc92"}
        ],
        "unverified_scope": [],
        "verified_session_count": 4,
        "unverified_session_count": 0
    }))
    .expect("test provenance")
}

fn session(date: &str, close_at: &str) -> CalendarSession {
    CalendarSession {
        date: date.to_string(),
        close_at: close_at.to_string(),
        auction_end_at: Some(close_at.to_string()),
        complete_at: Some(close_at.to_string()),
    }
}

/// A session whose auction end / confirmable-complete boundary differ from the close.
fn hk_session(
    date: &str,
    close_at: &str,
    auction_end_at: Option<&str>,
    complete_at: Option<&str>,
) -> CalendarSession {
    CalendarSession {
        date: date.to_string(),
        close_at: close_at.to_string(),
        auction_end_at: auction_end_at.map(str::to_string),
        complete_at: complete_at.map(str::to_string),
    }
}

/// Synthetic US schedule: 2026-10-30 .. 2026-11-06 around the DST fall-back and a
/// half session on 2026-11-27 (18:00Z close).
fn us_schedule() -> CalendarResource {
    let mut sessions = vec![
        session("2026-10-27", "2026-10-27T20:00:00Z"),
        session("2026-10-28", "2026-10-28T20:00:00Z"),
        session("2026-10-29", "2026-10-29T20:00:00Z"),
        session("2026-10-30", "2026-10-30T20:00:00Z"),
        // DST ends 2026-11-01: the regular close moves from 20:00Z to 21:00Z.
        session("2026-11-02", "2026-11-02T21:00:00Z"),
        session("2026-11-03", "2026-11-03T21:00:00Z"),
        session("2026-11-04", "2026-11-04T21:00:00Z"),
        session("2026-11-05", "2026-11-05T21:00:00Z"),
        session("2026-11-06", "2026-11-06T21:00:00Z"),
    ];
    // Half session: Thanksgiving Friday closes at 13:00 America/New_York = 18:00Z.
    sessions.push(session("2026-11-27", "2026-11-27T18:00:00Z"));
    CalendarResource {
        market: "US".to_string(),
        calendar: "XNYS".to_string(),
        timezone: "America/New_York".to_string(),
        source: "test:XNYS".to_string(),
        coverage_start: "2026-10-27".to_string(),
        coverage_end: "2026-11-27".to_string(),
        sessions,
        completion: CompletionSemantics::NoAuctionExtensionRule,
    }
}

/// Synthetic HK schedule across a normal day and a half day under the CAS regime.
///
/// Continuous close: 16:00 HKT normal / 12:00 HKT half day. The official closing
/// auction can end as late as 16:10 / 12:10 HKT, which is the completion boundary.
fn hk_cas_schedule() -> CalendarResource {
    let sessions = vec![
        hk_session(
            "2026-12-23",
            "2026-12-23T04:00:00Z",
            Some("2026-12-23T04:10:00Z"),
            Some("2026-12-23T04:10:00Z"),
        ),
        hk_session(
            "2026-12-24",
            "2026-12-24T04:00:00Z",
            Some("2026-12-24T04:10:00Z"),
            Some("2026-12-24T04:10:00Z"),
        ),
        hk_session(
            "2026-12-28",
            "2026-12-28T08:00:00Z",
            Some("2026-12-28T08:10:00Z"),
            Some("2026-12-28T08:10:00Z"),
        ),
        hk_session(
            "2026-12-29",
            "2026-12-29T08:00:00Z",
            Some("2026-12-29T08:10:00Z"),
            Some("2026-12-29T08:10:00Z"),
        ),
    ];
    CalendarResource {
        market: "HK".to_string(),
        calendar: "XHKG".to_string(),
        timezone: "Asia/Hong_Kong".to_string(),
        source: "test:XHKG".to_string(),
        coverage_start: "2026-12-23".to_string(),
        coverage_end: "2026-12-29".to_string(),
        sessions,
        completion: CompletionSemantics::Declared(hk_completion_provenance()),
    }
}

#[test]
fn decision_session_uses_the_last_completed_close() {
    let schedule = us_schedule();
    // Intraday on a trading day: the previous completed close is the decision.
    let intraday = Utc.with_ymd_and_hms(2026, 11, 3, 15, 0, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(intraday)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-11-02")
    );
    // Exactly at the close instant counts as completed.
    let at_close = Utc.with_ymd_and_hms(2026, 11, 3, 21, 0, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(at_close)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-11-03")
    );
    // One second before the close is still the previous session.
    let before_close = Utc.with_ymd_and_hms(2026, 11, 3, 20, 59, 59).unwrap();
    assert_eq!(
        schedule
            .decision_session(before_close)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-11-02")
    );
    // The half session uses its own (earlier) close instant.
    let half = Utc.with_ymd_and_hms(2026, 11, 27, 18, 0, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(half)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-11-27")
    );
    // Before the first session there is no decision.
    let before_any = Utc.with_ymd_and_hms(2026, 1, 2, 12, 0, 0).unwrap();
    assert_eq!(schedule.decision_session(before_any), None);
}

/// The US half session must resolve at its own early-close instant and not before.
#[test]
fn us_half_day_early_close_boundary_is_respected() {
    let schedule = us_schedule();
    let one_second_before = Utc.with_ymd_and_hms(2026, 11, 27, 17, 59, 59).unwrap();
    assert_eq!(
        schedule
            .decision_session(one_second_before)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-11-06"),
        "the early close must not be selected one second early"
    );
    let at_early_close = Utc.with_ymd_and_hms(2026, 11, 27, 18, 0, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(at_early_close)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-11-27")
    );
}

/// A-CAL-01 boundary regression: HK normal day, 16:00/16:05/16:09:59 -> previous
/// complete session; 16:10 -> the current session may be selected.
#[test]
fn hk_cas_normal_day_completion_boundary() {
    let schedule = hk_cas_schedule();
    // 2026-12-29 is a normal HK session: continuous close 08:00Z, CAS latest end 08:10Z.
    for (label, hour, minute, second) in [
        ("16:00:00", 8, 0, 0),
        ("16:00:01", 8, 0, 1),
        ("16:05:00", 8, 5, 0),
        ("16:09:59", 8, 9, 59),
    ] {
        let asof = Utc
            .with_ymd_and_hms(2026, 12, 29, hour, minute, second)
            .unwrap();
        assert_eq!(
            schedule
                .decision_session(asof)
                .map(|date| date.to_string())
                .as_deref(),
            Some("2026-12-28"),
            "{label} HKT must select the previous complete session, not the current day"
        );
        assert_eq!(
            schedule
                .closed_sessions(asof)
                .last()
                .map(|row| row.date.as_str()),
            Some("2026-12-28"),
            "{label} HKT must not report the current session as closed"
        );
    }
    let at_completion = Utc.with_ymd_and_hms(2026, 12, 29, 8, 10, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(at_completion)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-12-29"),
        "at/after the CAS latest end the current session may be selected"
    );
}

/// A-CAL-01 boundary regression: HK half day, 12:00/12:05/12:09:59 -> previous
/// complete session; 12:10 -> the current half-day session may be selected.
#[test]
fn hk_cas_half_day_completion_boundary() {
    let schedule = hk_cas_schedule();
    // 2026-12-24 is a half day: continuous close 04:00Z, CAS latest end 04:10Z.
    for (label, hour, minute, second) in [
        ("12:00:00", 4, 0, 0),
        ("12:00:01", 4, 0, 1),
        ("12:05:00", 4, 5, 0),
        ("12:09:59", 4, 9, 59),
    ] {
        let asof = Utc
            .with_ymd_and_hms(2026, 12, 24, hour, minute, second)
            .unwrap();
        assert_eq!(
            schedule
                .decision_session(asof)
                .map(|date| date.to_string())
                .as_deref(),
            Some("2026-12-23"),
            "{label} HKT must select the previous complete session on a half day"
        );
    }
    let at_completion = Utc.with_ymd_and_hms(2026, 12, 24, 4, 10, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(at_completion)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2026-12-24")
    );
}

/// A session whose completion boundary is unknown is skipped, not offset: the
/// resolver falls back to an earlier confirmed session and never assumes +10 minutes.
#[test]
fn unverified_completion_sessions_are_skipped() {
    let schedule = CalendarResource {
        market: "HK".to_string(),
        calendar: "XHKG".to_string(),
        timezone: "Asia/Hong_Kong".to_string(),
        source: "test:XHKG".to_string(),
        coverage_start: "2008-05-23".to_string(),
        coverage_end: "2008-05-27".to_string(),
        sessions: vec![
            hk_session(
                "2008-05-23",
                "2008-05-23T08:00:00Z",
                None,
                Some("2008-05-23T08:00:00Z"),
            ),
            // Historical closing-auction window: boundary unknown -> complete_at null.
            hk_session("2008-05-26", "2008-05-26T08:00:00Z", None, None),
            hk_session("2008-05-27", "2008-05-27T08:00:00Z", None, None),
        ],
        completion: CompletionSemantics::Unverified {
            reason: "historical closing-auction boundary unverified".to_string(),
        },
    };
    // After 2008-05-26 continuous close but before 2008-05-27: the unverified session
    // is skipped and the earlier confirmed session is selected.
    let after_unverified_close = Utc.with_ymd_and_hms(2008, 5, 26, 9, 0, 0).unwrap();
    assert_eq!(
        schedule
            .decision_session(after_unverified_close)
            .map(|date| date.to_string())
            .as_deref(),
        Some("2008-05-23"),
        "an unverified completion boundary must not be treated as the continuous close"
    );
    assert!(!schedule.completion_is_verified());
    assert_eq!(schedule.sessions_without_completion().len(), 2);
    let verdict = resolve_bar_axis(
        &schedule,
        after_unverified_close,
        &["2008-05-23".to_string(), "2008-05-26".to_string()],
    )
    .expect("axis");
    assert!(!verdict.completion_verified);
    assert_eq!(verdict.unverified_completion_sessions, 2);
    assert!(verdict.latest_observation_unresolved.is_some());
}

#[test]
fn target_session_is_the_fifth_exchange_session() {
    let schedule = us_schedule();
    let decision = chrono::NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
    let target = schedule
        .target_session(decision, 5)
        .expect("five sessions follow 2026-10-30");
    assert_eq!(target.to_string(), "2026-11-06");
    // A calendar that stops before the fifth session yields an unresolved target.
    let short = CalendarResource {
        sessions: schedule.sessions[..4].to_vec(),
        coverage_end: "2026-11-03".into(),
        ..schedule.clone()
    };
    assert_eq!(short.target_session(decision, 5), None);
    // The decision itself must be a session on the same calendar.
    let missing = chrono::NaiveDate::from_ymd_opt(2026, 10, 31).unwrap();
    assert_eq!(schedule.target_session(missing, 5), None);
}

#[test]
fn bar_axis_must_match_the_resolved_sessions() {
    let schedule = us_schedule();
    let asof = Utc.with_ymd_and_hms(2026, 11, 6, 22, 0, 0).unwrap();
    let matching: Vec<String> = [
        "2026-10-29",
        "2026-10-30",
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
    ]
    .iter()
    .map(|day| (*day).to_string())
    .collect();
    let verdict = resolve_bar_axis(&schedule, asof, &matching).expect("axis");
    assert!(verdict.latest_observation_unresolved.is_none());
    assert!(verdict.missing_sessions.is_none());

    // A missing session in the middle is a factual gap, not a recorded holiday.
    let gapped: Vec<String> = vec![
        "2026-10-29",
        "2026-10-30",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
    ]
    .into_iter()
    .map(str::to_string)
    .collect();
    let verdict = resolve_bar_axis(&schedule, asof, &gapped).expect("axis");
    assert!(
        verdict.missing_sessions.is_some(),
        "a missing session must be reported"
    );

    // A stale last bar (the as-of implies a later closed session) is unresolved.
    let stale: Vec<String> = vec!["2026-10-29", "2026-10-30", "2026-11-02"]
        .into_iter()
        .map(str::to_string)
        .collect();
    let verdict = resolve_bar_axis(&schedule, asof, &stale).expect("axis");
    assert!(verdict.latest_observation_unresolved.is_some());
}

#[test]
fn coverage_is_recomputed_not_declared() {
    let schedule = us_schedule();
    let bars = ["2026-10-29", "2026-10-30", "2026-11-02"]
        .map(str::to_string)
        .to_vec();
    // A producer may claim `complete`; the recomputation decides.
    let declared = json!({"expected_sessions": 7, "observed_sessions": 3, "missing_session_count": 0, "coverage_status": "complete"});
    let coverage =
        recompute_window_coverage(&schedule, &bars, "2026-10-28", "2026-10-30", &declared);
    assert_eq!(coverage.expected_sessions, 2, "window is (start, end]");
    assert_eq!(coverage.observed_sessions, 3);
    assert!(
        !coverage.complete,
        "an unexpected bar means the declaration was false"
    );
    assert_eq!(coverage.unexpected_sessions, vec!["2026-11-02".to_string()]);
    assert!(
        !coverage.declaration_mismatches.is_empty(),
        "declared counts must be compared"
    );

    // Exact axis and window coverage is complete.
    let exact_bars = ["2026-10-28", "2026-10-29", "2026-10-30"]
        .map(str::to_string)
        .to_vec();
    let declared_ok =
        json!({"expected_sessions": 3, "observed_sessions": 3, "missing_session_count": 0});
    let exact = recompute_window_coverage(
        &schedule,
        &exact_bars,
        "2026-10-27",
        "2026-10-30",
        &declared_ok,
    );
    assert!(exact.complete, "{exact:?}");
    assert!(exact.declaration_mismatches.is_empty());

    // Duplicates and unordered bars both block completeness.
    let duplicated = ["2026-10-29", "2026-10-29", "2026-10-30"]
        .map(str::to_string)
        .to_vec();
    let coverage = recompute_window_coverage(
        &schedule,
        &duplicated,
        "2026-10-28",
        "2026-10-30",
        &json!({}),
    );
    assert_eq!(coverage.duplicate_sessions, vec!["2026-10-29".to_string()]);
    assert!(!coverage.complete);
    let unordered = ["2026-10-30", "2026-10-29"].map(str::to_string).to_vec();
    let coverage = recompute_window_coverage(
        &schedule,
        &unordered,
        "2026-10-28",
        "2026-10-30",
        &json!({}),
    );
    assert!(!coverage.axis_matches_calendar);
    assert!(!coverage.complete);

    // A schedule that does not span the window cannot claim completeness.
    let mut narrow = schedule.clone();
    narrow.coverage_start = "2026-10-29".to_string();
    let coverage =
        recompute_window_coverage(&narrow, &bars[..2], "2026-10-01", "2026-10-30", &json!({}));
    assert!(!coverage.calendar_covers_window);
    let value = sd_core::calendar::coverage_value(&coverage);
    assert_eq!(value["complete"], false);
}

#[test]
fn sampling_phase_is_three_state() {
    let mut sessions = Vec::new();
    // Anchor plus nine sessions; ordinal(anchor) = 0 and the modulus is 6.
    sessions.push(session("2000-01-03", "2000-01-03T21:00:00Z"));
    for day in [
        "2000-01-04",
        "2000-01-05",
        "2000-01-06",
        "2000-01-07",
        "2000-01-10",
        "2000-01-11",
        "2000-01-12",
        "2000-01-13",
        "2000-01-14",
    ] {
        sessions.push(session(day, &format!("{day}T21:00:00Z")));
    }
    let anchored = CalendarResource {
        market: "US".into(),
        calendar: "XNYS".into(),
        timezone: "America/New_York".into(),
        source: "test:XNYS".into(),
        coverage_start: "2000-01-03".into(),
        coverage_end: "2000-01-14".into(),
        sessions,
        completion: CompletionSemantics::NoAuctionExtensionRule,
    };
    assert!(anchored.anchors_phase());
    let anchor = chrono::NaiveDate::from_ymd_opt(2000, 1, 3).unwrap();
    assert_eq!(anchored.session_ordinal(anchor), Some(0));
    assert_eq!(
        anchored.is_sampled(anchor),
        Some(true),
        "ordinal 0 is sampled"
    );
    let sixth = chrono::NaiveDate::from_ymd_opt(2000, 1, 11).unwrap();
    assert_eq!(anchored.session_ordinal(sixth), Some(6));
    assert_eq!(anchored.is_sampled(sixth), Some(true));
    let fifth = chrono::NaiveDate::from_ymd_opt(2000, 1, 10).unwrap();
    assert_eq!(anchored.is_sampled(fifth), Some(false));

    // Without the anchor the phase is *unanchored* (`None`), not "not sampled".
    let mut unanchored = anchored.clone();
    unanchored.sessions.remove(0);
    unanchored.coverage_start = "2000-01-04".to_string();
    assert!(!unanchored.anchors_phase());
    assert_eq!(
        unanchored.session_ordinal(chrono::NaiveDate::from_ymd_opt(2000, 1, 11).unwrap()),
        None
    );
    assert_eq!(
        unanchored.is_sampled(chrono::NaiveDate::from_ymd_opt(2000, 1, 11).unwrap()),
        None
    );
}

#[test]
fn resource_parsing_rejects_malformed_schedules() {
    let completion = json!({
        "rule_version": "cas_latest_end_v1",
        "markets": [
            {"market": "US", "calendar": "XNYS", "scheme": "none", "applies_from": null, "applies_to": null}
        ],
        "sources": [
            {"id": "nyse-trade-hours-calendars", "url": "https://www.nyse.com/trade/hours-calendars",
             "retrieved_at_utc": "2026-10-31T16:51:32Z",
             "sha256": "9ede5dd8af5a2ffef372d90e1fcac65ce3e9cb74ebab6d67edbce2d4bceae93c"}
        ],
        "unverified_scope": [],
        "verified_session_count": 2,
        "unverified_session_count": 0
    });
    let good = json!({
        "resource_schema": "2.0",
        "market": "US",
        "calendar": "XNYS",
        "timezone": "America/New_York",
        "source": "exchange_calendars 4.13.2",
        "coverage_start": "2026-10-29",
        "coverage_end": "2026-10-30",
        "completion": completion,
        "sessions": [
            {"date": "2026-10-29", "close_at": "2026-10-29T20:00:00Z",
             "auction_end_at": "2026-10-29T20:00:00Z", "complete_at": "2026-10-29T20:00:00Z"},
            {"date": "2026-10-30", "close_at": "2026-10-30T20:00:00Z",
             "auction_end_at": "2026-10-30T20:00:00Z", "complete_at": "2026-10-30T20:00:00Z"}
        ]
    });
    let resource = CalendarResource::from_json(&good).expect("valid schema-2.0 resource");
    assert_eq!(resource.session_dates().len(), 2);
    assert!(resource.completion_is_verified());
    assert_eq!(resource.session_dates().len(), 2);

    // Schema 1.0 stays readable for diagnostics but is never verified completion.
    let mut legacy = good.clone();
    legacy["resource_schema"] = json!("1.0");
    legacy.as_object_mut().unwrap().remove("completion");
    for row in legacy["sessions"].as_array_mut().unwrap() {
        row.as_object_mut().unwrap().remove("auction_end_at");
        row.as_object_mut().unwrap().remove("complete_at");
    }
    let legacy_resource = CalendarResource::from_json(&legacy).expect("schema 1.0 is readable");
    assert!(!legacy_resource.completion_is_verified());
    assert!(legacy_resource.completion_unverified_reason().is_some());

    let mut wrong_schema = good.clone();
    wrong_schema["resource_schema"] = json!("3.0");
    assert!(CalendarResource::from_json(&wrong_schema).is_err());

    // Schema 2.0 without the completion provenance block is rejected (fail closed).
    let mut missing_completion = good.clone();
    missing_completion
        .as_object_mut()
        .unwrap()
        .remove("completion");
    assert!(
        CalendarResource::from_json(&missing_completion).is_err(),
        "schema 2.0 without completion provenance must fail closed"
    );

    // A schema-2.0 row missing an explicit complete_at is malformed.
    let mut missing_complete = good.clone();
    missing_complete["sessions"][0]
        .as_object_mut()
        .unwrap()
        .remove("complete_at");
    assert!(CalendarResource::from_json(&missing_complete).is_err());

    // completion before the continuous close is impossible.
    let mut inverted = good.clone();
    inverted["sessions"][0]["complete_at"] = json!("2026-10-29T19:59:59Z");
    assert!(CalendarResource::from_json(&inverted).is_err());

    // A declared count that disagrees with the rows is a tampered resource.
    let mut bad_counts = good.clone();
    bad_counts["completion"]["verified_session_count"] = json!(1);
    assert!(CalendarResource::from_json(&bad_counts).is_err());

    let mut unsorted = good.clone();
    unsorted["sessions"] = json!([
        {"date": "2026-10-30", "close_at": "2026-10-30T20:00:00Z",
         "auction_end_at": null, "complete_at": "2026-10-30T20:00:00Z"},
        {"date": "2026-10-29", "close_at": "2026-10-29T20:00:00Z",
         "auction_end_at": null, "complete_at": "2026-10-29T20:00:00Z"}
    ]);
    assert!(
        CalendarResource::from_json(&unsorted).is_err(),
        "sessions must be strictly increasing"
    );

    let mut local_close = good.clone();
    local_close["sessions"] = json!([{"date": "2026-10-29", "close_at": "2026-10-29T16:00:00-04:00",
                "auction_end_at": null, "complete_at": "2026-10-29T20:00:00Z"}]);
    local_close["coverage_end"] = json!("2026-10-29");
    local_close["completion"]["verified_session_count"] = json!(1);
    assert!(
        CalendarResource::from_json(&local_close).is_err(),
        "close_at must be a UTC Z instant"
    );

    let mut coverage_mismatch = good;
    coverage_mismatch["coverage_end"] = json!("2026-11-30");
    assert!(CalendarResource::from_json(&coverage_mismatch).is_err());
}

/// A snapshot calendar without completion metadata is only usable as verified evidence
/// for a market where no auction extension exists (US); an HK snapshot without it is
/// explicitly unverified and its sessions are skipped, not offset.
#[test]
fn snapshot_calendar_completion_semantics_follow_the_market_rule() {
    let us_block = json!({
        "source": "exchange_calendars 4.13.2",
        "timezone": "America/New_York",
        "coverage_start": "2026-10-29",
        "coverage_end": "2026-10-30",
        "sessions": [
            {"date": "2026-10-29", "close_at": "2026-10-29T20:00:00+00:00"},
            {"date": "2026-10-30", "close_at": "2026-10-30T20:00:00+00:00"}
        ]
    });
    let us = CalendarResource::from_snapshot_calendar(&us_block, "US").expect("legacy US block");
    assert!(us.completion_is_verified(), "US has no auction extension");
    assert_eq!(
        us.sessions[0].complete_at.as_deref(),
        Some("2026-10-29T20:00:00+00:00")
    );
    let at_close = Utc.with_ymd_and_hms(2026, 10, 30, 20, 0, 0).unwrap();
    assert_eq!(
        us.decision_session(at_close).map(|date| date.to_string()),
        Some("2026-10-30".to_string())
    );

    let hk_block = json!({
        "source": "exchange_platform:legacy",
        "timezone": "Asia/Hong_Kong",
        "coverage_start": "2026-12-23",
        "coverage_end": "2026-12-24",
        "sessions": [
            {"date": "2026-12-23", "close_at": "2026-12-23T08:00:00Z"},
            {"date": "2026-12-24", "close_at": "2026-12-24T04:00:00Z"}
        ]
    });
    let hk = CalendarResource::from_snapshot_calendar(&hk_block, "HK").expect("legacy HK block");
    assert!(!hk.completion_is_verified());
    assert!(hk.completion_unverified_reason().is_some());
    assert!(hk.sessions.iter().all(|row| row.complete_at.is_none()));
    let after_close = Utc.with_ymd_and_hms(2026, 12, 23, 8, 5, 0).unwrap();
    assert_eq!(
        hk.decision_session(after_close),
        None,
        "an HK snapshot without completion metadata must not assume the completion boundary"
    );
    assert_eq!(hk.sessions_without_completion().len(), 2);
}
