//! Path guard and atomic write lifecycle tests.
//!
//! They run entirely inside `target/` of the repository: destructive behaviour is
//! only ever injected into a scratch test root, never into user runtime data.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use sd_storage::atomic_write::{
    atomic_write_text, is_orphan_temp_dir, is_orphan_temp_file, scratch_path,
};
use sd_storage::jsonio::{read_json_file, read_text_file, write_json};
use sd_storage::path_guard::{
    path_contains, real_directory, require_absent, safe_output_destination, validate_path_component,
};
use serde_json::json;

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn scratch_root(label: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!("sd-storage-tests-{}", std::process::id()));
    std::fs::create_dir_all(&root).expect("scratch root");
    let unique = format!(
        "{label}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let directory = root.join(unique);
    std::fs::create_dir_all(&directory).expect("scratch directory");
    directory
}

#[test]
fn component_validation_rejects_every_escape_shape() {
    for valid in ["artifact.json", ".hidden", "a.b_c-d", "019c25ac"] {
        assert!(
            validate_path_component(valid, "name").is_ok(),
            "{valid} should be accepted"
        );
    }
    for invalid in [
        "", ".", "..", "../x", "a/b", "a\\b", "C:", "a b", "é", "a\0b",
    ] {
        assert!(
            validate_path_component(invalid, "name").is_err(),
            "{invalid:?} must be rejected"
        );
    }
    let error = validate_path_component("..", "output name").expect_err("dot-dot rejected");
    assert_eq!(error.error_type(), "ValueError");
    assert!(error.message().contains("safe filename component"));
}

#[test]
fn output_destination_stays_inside_the_root() {
    let root = scratch_root("guard");
    let destination = safe_output_destination(&root, "analysis.json").expect("safe name");
    assert_eq!(
        destination,
        root.canonicalize().unwrap().join("analysis.json")
    );
    assert!(safe_output_destination(&root, "../escape").is_err());
    assert!(safe_output_destination(&root, "a/b").is_err());
    assert!(path_contains(&root, &destination));

    let outside = scratch_root("outside");
    assert!(!path_contains(&root, &outside.join("x")));
    require_absent(&destination).expect("absent file is fine");
    std::fs::write(&destination, "x").expect("write");
    let error = require_absent(&destination).expect_err("existing file must not be overwritten");
    assert_eq!(error.error_type(), "FileExistsError");
    let _ = std::fs::remove_dir_all(&root);
    let _ = std::fs::remove_dir_all(&outside);
}

#[test]
fn real_directory_rejects_missing_paths() {
    let root = scratch_root("real");
    real_directory(&root, "run dir").expect("existing directory is fine");
    let missing = root.join("missing");
    let error =
        real_directory(&missing, "run dir").expect_err("missing directory must be rejected");
    assert!(error.message().contains("must be an existing directory"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn atomic_write_replaces_without_leaving_scratch_files() {
    let root = scratch_root("atomic");
    let target = root.join("analysis.json");
    atomic_write_text(&target, "first\n").expect("first write");
    assert_eq!(read_text_file(&target).unwrap(), "first\n");
    atomic_write_text(&target, "second\n").expect("second write");
    assert_eq!(read_text_file(&target).unwrap(), "second\n");

    let leftovers: Vec<String> = std::fs::read_dir(&root)
        .expect("list")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name != "analysis.json")
        .collect();
    assert!(
        leftovers.is_empty(),
        "scratch files left behind: {leftovers:?}"
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn scratch_names_match_the_cleanup_ownership_patterns() {
    let target = Path::new("runtime/refactor/x/analysis.json");
    let scratch = scratch_path(target);
    let name = scratch.file_name().unwrap().to_string_lossy().into_owned();
    assert!(
        name.starts_with(".analysis.json."),
        "unexpected scratch name: {name}"
    );
    assert!(
        is_orphan_temp_file(&name),
        "scratch file must match the orphan sweep pattern: {name}"
    );
    assert_eq!(scratch.parent(), target.parent());

    assert!(is_orphan_temp_file(".analysis.json.0123456789ab.tmp"));
    assert!(
        !is_orphan_temp_file(".analysis.json.0123456789AB.tmp"),
        "uppercase hex is not the pattern"
    );
    assert!(!is_orphan_temp_file(".analysis.json.tmp"));
    assert!(
        !is_orphan_temp_file("analysis.json.0123456789ab.tmp"),
        "missing leading dot"
    );
    assert!(
        !is_orphan_temp_file(".pipeline.lock"),
        "lock markers are not scratch files"
    );
    assert!(is_orphan_temp_dir(
        ".SYN_20261024T000000Z_5D_00d9769e96.p7d1wq3a"
    ));
    // The legacy directory pattern is `[a-z0-9_]{8}`, so the 8-character suffix of a
    // killed snapshot *file* scratch name also matches; R10 keeps the two patterns
    // separate by checking file vs directory before sweeping.
    assert!(is_orphan_temp_dir(
        ".SYN_20261024T000000Z_5D_00d9769e96.r1601rue"
    ));
    assert!(!is_orphan_temp_dir(
        ".SYN_20261024T000000Z_5D_00d9769e96.p7d1wq3"
    ));
}

#[test]
fn json_io_is_bom_tolerant_and_exclusive_writes_refuse_overwrites() {
    let root = scratch_root("jsonio");
    let path = root.join("receipt.json");
    std::fs::write(&path, b"\xef\xbb\xbf{\"a\":1}\n").expect("bom write");
    let value = read_json_file(&path).expect("BOM-prefixed JSON must parse");
    assert_eq!(value["a"], 1);

    let target = root.join("out.json");
    write_json(&target, &json!({"ticker": "MU", "p": 0.5}), false).expect("write");
    assert_eq!(
        read_text_file(&target).unwrap(),
        "{\n  \"ticker\": \"MU\",\n  \"p\": 0.5\n}\n"
    );
    let error = write_json(&target, &json!({"x": 1}), true).expect_err("exclusive write must fail");
    assert_eq!(error.error_type(), "FileExistsError");
    let _ = std::fs::remove_dir_all(&root);
}
