//! Snapshot compatibility: Rust must read Python-frozen snapshots unchanged.
//!
//! The fixtures under `tests/fixtures/snapshot/` were written by the unmodified
//! Python runtime with a frozen clock. Loading them here proves the digest chain,
//! identity binding and market extraction agree across implementations; a mismatch
//! would make every historical artifact unusable, so nothing is normalized away.

use std::path::{Path, PathBuf};

use sd_storage::snapshot::{load_snapshot, DOMAINS};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/snapshot")
}

#[test]
fn python_frozen_snapshot_loads_with_its_digest_chain_intact() {
    let loaded =
        load_snapshot(&fixtures().join("complete-window")).expect("frozen snapshot must load");
    assert_eq!(loaded.schema_version, "1.3");
    assert!(loaded.production_schema);
    assert_eq!(loaded.ticker(), "SYN");
    assert_eq!(loaded.identity.market, "US");
    assert_eq!(loaded.identity.horizon, "5D");
    assert_eq!(loaded.identity.asof_timestamp, "2026-10-23T21:00:00Z");
    // Identity fields captured by the Python fixture generation.
    assert_eq!(
        loaded.identity.snapshot_content_sha256,
        "00d9769e96e8b45deff4406b0d090a0d264b8ce387baa106d06eb6100540bc3c"
    );
    assert_eq!(loaded.snapshot_id(), "SYN_20261024T000000Z_5D_00d9769e96");
    assert_eq!(
        loaded.manifest["manifest_sha256"],
        "5ae811cd8fc19b956e0f16b84c27b37d585d36d2a7f58a6dc1d8937f52ef8a13"
    );
    // The source payload digest is recorded for provenance and must be a 64-hex digest.
    let source_digest = loaded.manifest["source_payload_sha256"]
        .as_str()
        .expect("source digest");
    assert_eq!(source_digest.len(), 64);
    assert!(source_digest
        .chars()
        .all(|character| character.is_ascii_hexdigit()));
    // The gate is re-derived at load time from the frozen domains; the frozen values
    // are replaced, exactly like `_derived_metadata` in the legacy loader.
    assert!(loaded.validation_recomputed);
    assert_eq!(loaded.frozen_validation["forecast_eligible"], true);
    assert_eq!(loaded.frozen_validation["reporting_status"], "degraded");
    assert_eq!(
        loaded.manifest["policy"]["policy_version"],
        "fixed_52w_5d_v1"
    );
    assert_eq!(loaded.manifest["policy"]["decision_session"], "2026-10-23");
    assert_eq!(loaded.manifest["policy"]["horizon_sessions"], 5);
}

#[test]
fn all_domains_and_bars_are_exposed_after_verification() {
    let loaded = load_snapshot(&fixtures().join("complete-window")).expect("load");
    assert_eq!(loaded.domains.len(), DOMAINS.len());
    for name in DOMAINS {
        assert!(loaded.domains.contains_key(name), "missing domain {name}");
    }
    assert_eq!(loaded.domains["market"]["status"], "complete");
    assert_eq!(loaded.market.bars.len(), 260);
    assert_eq!(
        loaded.market.bars.len(),
        loaded.manifest["market_bar_count"].as_u64().unwrap() as usize
    );
    let first = &loaded.market.bars[0].date;
    let last = &loaded.market.bars.last().unwrap().date;
    assert_eq!(last, "2026-10-23");
    assert!(
        first.as_str() >= "2025-10-20" && first.as_str() <= "2025-10-27",
        "unexpected first bar {first}"
    );
    // Bars are strictly increasing.
    assert!(loaded
        .market
        .bars
        .windows(2)
        .all(|pair| pair[0].date < pair[1].date));
    assert_eq!(loaded.market.price_basis.as_deref(), Some("split_adjusted"));
    assert_eq!(loaded.market.adjustment.as_deref(), Some("provider"));
    assert_eq!(loaded.market.currency.as_deref(), Some("USD"));
    // The frozen schedule is loaded from the snapshot, not regenerated.
    let calendar = loaded.market.calendar.expect("frozen session calendar");
    assert_eq!(calendar.source, "selfcheck:synthetic_exchange");
    assert_eq!(calendar.timezone, "America/New_York");
    assert!(calendar.covers("2025-10-23", "2026-10-23"));
    assert_eq!(
        calendar.decision_session(
            chrono::DateTime::parse_from_rfc3339("2026-10-23T21:00:00Z")
                .unwrap()
                .with_timezone(&chrono::Utc)
        ),
        chrono::NaiveDate::from_ymd_opt(2026, 10, 23)
    );
    // Target session is the fifth session after the decision, resolved from the frozen calendar.
    let target = calendar
        .target_session(chrono::NaiveDate::from_ymd_opt(2026, 10, 23).unwrap(), 5)
        .expect("frozen schedule extends past the target");
    assert!(target > chrono::NaiveDate::from_ymd_opt(2026, 10, 23).unwrap());
}

#[test]
fn missing_calendar_snapshot_still_loads_and_reports_the_frozen_veto() {
    let loaded = load_snapshot(&fixtures().join("missing-calendar")).expect("load");
    assert!(
        loaded.market.calendar.is_none(),
        "no frozen schedule means no verified calendar"
    );
    assert_eq!(loaded.frozen_validation["forecast_eligible"], false);
    assert_eq!(loaded.frozen_validation["reporting_status"], "veto");
    let codes = loaded.frozen_validation["reason_codes"]
        .as_array()
        .expect("reason codes");
    assert!(
        codes.iter().any(|code| code == "CALENDAR_UNVERIFIED"),
        "the frozen veto must disclose the unverified calendar: {codes:?}"
    );
}

#[test]
fn load_recomputed_manifest_matches_python() {
    let load_root = fixtures().parent().expect("fixtures root").join("load");
    for case in ["complete-window", "missing-calendar"] {
        let loaded = load_snapshot(&fixtures().join(case)).expect("frozen snapshot must load");
        let expected_path = load_root.join(case).join("expected-manifest.json");
        let text = std::fs::read_to_string(&expected_path)
            .unwrap_or_else(|error| panic!("cannot read {}: {error}", expected_path.display()));
        let expected: serde_json::Value = serde_json::from_str(&text).expect("expected manifest");
        assert_eq!(
            loaded.manifest, expected,
            "load-time recomputed manifest differs for {case}"
        );
        let market_expected_path = load_root.join(case).join("expected-market.json");
        let market_expected: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(&market_expected_path).expect("expected market"),
        )
        .expect("expected market json");
        assert_eq!(
            loaded.market.bars.len(),
            market_expected["market_bar_count"].as_u64().unwrap() as usize
        );
        assert_eq!(
            loaded.frozen_validation,
            market_expected["data_validation"].clone(),
            "loaded market validation differs for {case}"
        );
    }
}

#[test]
fn tampered_domain_is_rejected_before_any_research_runs() {
    let source = fixtures().join("complete-window");
    let scratch = std::env::temp_dir().join(format!("sd-snapshot-tamper-{}", std::process::id()));
    if scratch.exists() {
        std::fs::remove_dir_all(&scratch).expect("clean scratch");
    }
    copy_tree(&source, &scratch);
    // Edit one close price: the canonical digest must catch it.
    let market_path = scratch.join("market.json");
    let text = std::fs::read_to_string(&market_path).expect("read");
    let tampered = text.replacen("\"close\": ", "\"close\": 9", 1);
    std::fs::write(&market_path, tampered).expect("write");
    let error = load_snapshot(&scratch).expect_err("tampered domain must be rejected");
    assert!(
        error
            .message()
            .contains("snapshot domain digest mismatch: market"),
        "unexpected error: {}",
        error.message()
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn tampered_manifest_is_rejected() {
    let source = fixtures().join("complete-window");
    let scratch = std::env::temp_dir().join(format!(
        "sd-snapshot-manifest-tamper-{}",
        std::process::id()
    ));
    if scratch.exists() {
        std::fs::remove_dir_all(&scratch).expect("clean scratch");
    }
    copy_tree(&source, &scratch);
    let manifest_path = scratch.join("manifest.json");
    let text = std::fs::read_to_string(&manifest_path).expect("read");
    // Flip the frozen eligibility claim without re-hashing the manifest.
    let tampered = text.replacen(
        "\"forecast_eligible\": true",
        "\"forecast_eligible\": false",
        1,
    );
    assert_ne!(
        tampered, text,
        "the fixture must contain an eligibility claim to flip"
    );
    std::fs::write(&manifest_path, tampered).expect("write");
    let error = load_snapshot(&scratch).expect_err("tampered manifest must be rejected");
    assert!(
        error
            .message()
            .contains("snapshot manifest digest mismatch"),
        "unexpected error: {}",
        error.message()
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn foreign_snapshot_id_is_rejected() {
    let source = fixtures().join("complete-window");
    let scratch =
        std::env::temp_dir().join(format!("sd-snapshot-id-tamper-{}", std::process::id()));
    if scratch.exists() {
        std::fs::remove_dir_all(&scratch).expect("clean scratch");
    }
    copy_tree(&source, &scratch);
    let manifest_path = scratch.join("manifest.json");
    let text = std::fs::read_to_string(&manifest_path).expect("read");
    let tampered = text.replacen(
        "SYN_20261024T000000Z_5D_00d9769e96",
        "SYN_20261024T000000Z_5D_deadbeef00",
        1,
    );
    std::fs::write(&manifest_path, tampered).expect("write");
    let error = load_snapshot(&scratch).expect_err("a foreign snapshot id must be rejected");
    assert!(
        error.message().contains("content identity mismatch")
            || error.message().contains("manifest digest mismatch"),
        "unexpected error: {}",
        error.message()
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

fn copy_tree(source: &Path, destination: &Path) {
    std::fs::create_dir_all(destination).expect("scratch dir");
    for entry in std::fs::read_dir(source).expect("list fixture") {
        let entry = entry.expect("entry");
        let target = destination.join(entry.file_name());
        if entry.path().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), &target).expect("copy");
        }
    }
}
