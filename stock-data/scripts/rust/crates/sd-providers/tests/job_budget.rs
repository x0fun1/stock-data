//! S04: one job budget/deadline is shared by the native route, redirects, session
//! bootstrap and the compatibility fallback; exhausted jobs send no extra request.

use std::path::PathBuf;

use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::{JobContext, JobState, MAX_RESPONSE_BYTES};
use sd_providers::yahoo_gateway::call_with_job;
use sd_providers::yahoo_provider::reset_job_circuit;
use serde_json::{json, Map, Value};

/// The Yahoo job circuit is process-global; serialize the budget tests.
static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn params() -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("symbol".into(), json!("AAPL"));
    map.insert("interval".into(), json!("1d"));
    map.insert("range".into(), json!("1mo"));
    map
}

fn cache_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("sd-job-budget-{}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("cache dir");
    dir
}

fn failing_transport(responses: usize) -> MockTransport {
    let scripted = (0..responses)
        .map(|_| HttpResponse::new(500, "Server Error", b"{\"error\":true}".to_vec()))
        .collect();
    MockTransport::new(scripted)
}

#[test]
fn exhausted_budget_sends_no_fallback_request() {
    let _serial = SERIAL.lock().expect("serial");
    reset_job_circuit();
    let transport = failing_transport(4);
    let job = JobState::new(JobContext::new(15.0, 120.0, 1, MAX_RESPONSE_BYTES));
    let error = call_with_job(
        "yahoo_history",
        &params(),
        Some(cache_dir()),
        &transport,
        &job,
    )
    .expect_err("the job must fail once its one request is spent");
    assert_eq!(
        error.0, "BudgetExceeded",
        "fallback must be refused by the shared budget: {error:?}"
    );
    assert_eq!(
        transport.requests().len(),
        1,
        "no request may be sent after the shared budget is exhausted"
    );
    let requests: usize = job.with_borrow(|context| context.requests);
    assert_eq!(requests, 1);
}

#[test]
fn shared_job_budget_spans_multiple_capability_calls() {
    let _serial = SERIAL.lock().expect("serial");
    reset_job_circuit();
    let job = JobState::new(JobContext::new(15.0, 120.0, 1, MAX_RESPONSE_BYTES));
    let first_transport = failing_transport(2);
    let _ = call_with_job(
        "yahoo_history",
        &params(),
        Some(cache_dir()),
        &first_transport,
        &job,
    )
    .expect_err("first capability spends the only request");
    assert_eq!(first_transport.requests().len(), 1);

    // A second capability on the same job must fail before any wire request.
    let second_transport = failing_transport(2);
    let error = call_with_job(
        "yahoo_quote_summary",
        &params(),
        Some(cache_dir()),
        &second_transport,
        &job,
    )
    .expect_err("the shared budget is already spent");
    assert_eq!(error.0, "BudgetExceeded", "{error:?}");
    assert_eq!(
        second_transport.requests().len(),
        0,
        "the fallback/native layer must not touch the wire without budget"
    );
}

#[test]
fn expired_job_deadline_sends_no_request() {
    let _serial = SERIAL.lock().expect("serial");
    reset_job_circuit();
    let transport = failing_transport(2);
    let job = JobState::new(JobContext::new(15.0, 0.001, 30, MAX_RESPONSE_BYTES));
    std::thread::sleep(std::time::Duration::from_millis(20));
    let error = call_with_job(
        "yahoo_history",
        &params(),
        Some(cache_dir()),
        &transport,
        &job,
    )
    .expect_err("deadline must terminate the job");
    assert_eq!(error.0, "DeadlineExceeded", "{error:?}");
    assert_eq!(transport.requests().len(), 0);
}

#[test]
fn request_timeout_is_clamped_by_job_timeout() {
    let _serial = SERIAL.lock().expect("serial");
    reset_job_circuit();
    let job = JobState::new(JobContext::new(0.1, 120.0, 30, MAX_RESPONSE_BYTES));
    let clamped = job.effective_timeout(15.0).expect("timeout");
    assert!(
        (clamped - 0.1).abs() < 1e-9,
        "request timeout must be clamped to 0.1s, got {clamped}"
    );
    let remaining_limited = job.effective_timeout(0.01).expect("timeout");
    assert!(remaining_limited <= 0.01, "{remaining_limited}");
}
