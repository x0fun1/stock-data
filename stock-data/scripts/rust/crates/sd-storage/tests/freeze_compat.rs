//! Freeze compatibility: the Rust snapshot must equal the Python snapshot byte for
//! byte, including every digest and the `snapshot_id`.
//!
//! The fixtures contain the receipt (input), the full Python-frozen directory
//! (reference) and the identity chain. A mismatch in any file means historical
//! artifacts produced by either implementation would diverge. The recorded Python
//! files use Windows text-mode CRLF; Unix selects the corresponding LF text-mode
//! reference. JSON content, canonical digests and identity remain exact.

use std::path::{Path, PathBuf};

use sd_core::clock::FixedClock;
use sd_storage::freeze::{freeze_snapshot, FreezeOptions};
use serde_json::Value;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/freeze")
}

fn read(path: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(path).expect("read")).expect("json")
}

fn scratch(case: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!("sd-freeze-tests-{}", std::process::id()));
    std::fs::create_dir_all(&root).expect("scratch root");
    let directory = root.join(case);
    if directory.exists() {
        std::fs::remove_dir_all(&directory).expect("clean scratch");
    }
    std::fs::create_dir_all(&directory).expect("create scratch");
    directory
}

fn files(root: &Path) -> Vec<(String, Vec<u8>)> {
    let mut entries = Vec::new();
    fn walk(root: &Path, current: &Path, entries: &mut Vec<(String, Vec<u8>)>) {
        for entry in std::fs::read_dir(current).expect("list") {
            let entry = entry.expect("entry");
            let path = entry.path();
            if path.is_dir() {
                walk(root, &path, entries);
            } else {
                let relative = path
                    .strip_prefix(root)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                entries.push((relative, std::fs::read(&path).expect("read file")));
            }
        }
    }
    walk(root, root, &mut entries);
    entries.sort();
    entries
}

#[test]
fn rust_freeze_reproduces_the_python_snapshot_byte_for_byte() {
    for case in ["supplied-calendar", "missing-calendar"] {
        let directory = fixtures().join(case);
        let payload = read(&directory.join("input.json"));
        let expected_outcome = read(&directory.join("expected-outcome.json"));
        let output = scratch(case);
        let clock =
            FixedClock::from_timestamp(expected_outcome["snapshot_created_at"].as_str().unwrap())
                .expect("freeze clock");
        let options = FreezeOptions {
            output_root: &output,
            library_calendar: None,
            allow_library_calendar: false,
            on_snapshot_created: None,
        };
        let created = freeze_snapshot(&payload, &options, &clock).expect("freeze");
        assert_eq!(
            created.file_name().unwrap().to_string_lossy(),
            expected_outcome["snapshot_id"].as_str().unwrap(),
            "snapshot_id differs for {case}"
        );

        let expected_root = directory.join("expected");
        let expected_files = files(&expected_root);
        let actual_files = files(&created);
        assert_eq!(
            actual_files
                .iter()
                .map(|(name, _)| name.clone())
                .collect::<Vec<_>>(),
            expected_files
                .iter()
                .map(|(name, _)| name.clone())
                .collect::<Vec<_>>(),
            "file set differs for {case}"
        );
        for ((name, actual), (_, expected)) in actual_files.iter().zip(expected_files.iter()) {
            let expected = if cfg!(windows) {
                expected.clone()
            } else {
                // Python Path.write_text uses os.linesep. Only physical CRLF is
                // changed; escaped JSON string values and all other bytes remain exact.
                String::from_utf8(expected.clone())
                    .expect("UTF-8 reference")
                    .replace("\r\n", "\n")
                    .into_bytes()
            };
            if *actual != expected {
                panic!(
                    "frozen file differs for {case}/{name}\n--- actual ---\n{}\n--- expected ---\n{}",
                    String::from_utf8_lossy(actual),
                    String::from_utf8_lossy(&expected)
                );
            }
        }
        let _ = std::fs::remove_dir_all(&output);
    }
}

#[test]
fn freeze_identity_and_digest_chain_matches_python() {
    let directory = fixtures().join("supplied-calendar");
    let payload = read(&directory.join("input.json"));
    let expected = read(&directory.join("expected-outcome.json"));
    let output = scratch("identity");
    let clock = FixedClock::from_timestamp(expected["snapshot_created_at"].as_str().unwrap())
        .expect("clock");
    let options = FreezeOptions {
        output_root: &output,
        library_calendar: None,
        allow_library_calendar: false,
        on_snapshot_created: None,
    };
    let created = freeze_snapshot(&payload, &options, &clock).expect("freeze");
    let manifest = read(&created.join("manifest.json"));
    for key in [
        "snapshot_id",
        "snapshot_content_sha256",
        "manifest_sha256",
        "source_payload_sha256",
        "snapshot_created_at",
        "market_bar_count",
    ] {
        assert_eq!(manifest[key], expected[key], "manifest {key} differs");
    }
    assert_eq!(
        manifest["data_validation"]["forecast_eligible"],
        expected["forecast_eligible"]
    );
    assert_eq!(
        manifest["data_validation"]["reporting_status"],
        expected["reporting_status"]
    );
    // Reloading the freshly frozen snapshot yields the same identity (Rust -> Rust).
    let loaded = sd_storage::snapshot::load_snapshot(&created).expect("reload");
    assert_eq!(
        loaded.identity.snapshot_id,
        manifest["snapshot_id"].as_str().unwrap()
    );
    assert_eq!(
        loaded.identity.snapshot_content_sha256,
        manifest["snapshot_content_sha256"].as_str().unwrap()
    );
    assert_eq!(
        loaded.market.bars.len(),
        manifest["market_bar_count"].as_u64().unwrap() as usize
    );
    let _ = std::fs::remove_dir_all(&output);
}

#[test]
fn freezing_refuses_to_overwrite_an_existing_snapshot() {
    let directory = fixtures().join("supplied-calendar");
    let payload = read(&directory.join("input.json"));
    let expected = read(&directory.join("expected-outcome.json"));
    let output = scratch("overwrite");
    let clock = FixedClock::from_timestamp(expected["snapshot_created_at"].as_str().unwrap())
        .expect("clock");
    let options = FreezeOptions {
        output_root: &output,
        library_calendar: None,
        allow_library_calendar: false,
        on_snapshot_created: None,
    };
    freeze_snapshot(&payload, &options, &clock).expect("first freeze");
    let error = freeze_snapshot(&payload, &options, &clock).expect_err("second freeze must refuse");
    assert_eq!(error.error_type(), "FileExistsError");
    assert!(error
        .message()
        .contains("refusing to overwrite frozen snapshot"));
    let _ = std::fs::remove_dir_all(&output);
}
