//! S01/S08 black-box launcher tests: the Skill launcher preserves cwd/argv, uses the
//! pinned toolchain, and recompiles after a source edit.
//!
//! The launcher is a POSIX shell script, so these tests are skipped when no `bash` is
//! available (the Debian CI/acceptance job always runs them). They never touch the
//! network: `scripts/setup.sh` has already populated the locked cargo cache.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn skill_root() -> PathBuf {
    // crates/sd-cli -> crates -> rust -> scripts -> stock-data
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../..")
}

fn bash() -> Option<PathBuf> {
    [
        PathBuf::from("bash"),
        PathBuf::from("/bin/bash"),
        PathBuf::from("/usr/bin/bash"),
        PathBuf::from(r"C:\Program Files\Git\bin\bash.exe"),
        PathBuf::from(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ]
    .into_iter()
    .find(|candidate| {
        Command::new(candidate)
            .arg("--version")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
    })
}

fn cargo_bin_dir() -> Option<PathBuf> {
    if let Ok(home) = std::env::var("CARGO_HOME") {
        let candidate = PathBuf::from(home).join("bin");
        if candidate.is_dir() {
            return Some(candidate);
        }
    }
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|entry| entry.join("cargo"))
            .find(|candidate| candidate.is_file())
            .and_then(|candidate| candidate.parent().map(Path::to_path_buf))
    })
}

fn launcher_env(bash: &Path, target_dir: &Path) -> Command {
    let mut command = Command::new(bash);
    let mut paths: Vec<PathBuf> = Vec::new();
    if let Some(dir) = cargo_bin_dir() {
        paths.push(dir);
    }
    if let Ok(home) = std::env::var("RUSTUP_HOME") {
        paths.push(PathBuf::from(home).join("bin"));
    }
    #[cfg(windows)]
    paths.extend([
        PathBuf::from(r"C:\Program Files\Git\bin"),
        PathBuf::from(r"C:\Program Files\Git\usr\bin"),
    ]);
    paths.extend([
        PathBuf::from("/usr/local/bin"),
        PathBuf::from("/usr/bin"),
        PathBuf::from("/bin"),
    ]);
    // User-owned compiler/linker installations must remain discoverable as well.
    // Windows drive-letter paths above are never valid Unix PATH components.
    if let Some(current) = std::env::var_os("PATH") {
        paths.extend(std::env::split_paths(&current));
    }
    let path = std::env::join_paths(paths).expect("PATH");
    command.env("PATH", path);
    command.env("STOCK_DATA_TARGET_DIR", target_dir);
    if let Ok(home) = std::env::var("CARGO_HOME") {
        command.env("CARGO_HOME", home);
    }
    if let Ok(home) = std::env::var("RUSTUP_HOME") {
        command.env("RUSTUP_HOME", home);
    }
    command.env_remove("PYTHONHOME");
    command.env_remove("PYTHONPATH");
    command.env_remove("STOCK_DATA_SKILL_ROOT");
    command
}

fn run_launcher(bash: &Path, skill: &Path, target_dir: &Path, cwd: &Path, args: &[&str]) -> Output {
    let script = skill.join("scripts").join("run.sh");
    let mut command = launcher_env(bash, target_dir);
    command
        .arg(&script)
        .args(args)
        .current_dir(cwd)
        .output()
        .unwrap_or_else(|error| panic!("cannot run launcher: {error}"))
}

fn combined(output: &Output) -> String {
    format!(
        "stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn scratch(label: &str) -> PathBuf {
    let directory = std::env::temp_dir().join(format!(
        "sd-launcher-{label}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&directory).expect("scratch");
    directory
}

fn launcher_target(label: &str) -> PathBuf {
    // Optional reuse for memory-limited Debian acceptance. The runner must execute
    // tests serially when sharing this external Cargo cache. All output/source-edit
    // assertions remain active and therefore also exercise cache freshness.
    if let Some(path) = std::env::var_os("STOCK_DATA_LAUNCHER_TEST_TARGET") {
        let path = PathBuf::from(path);
        std::fs::create_dir_all(&path).expect("external launcher target");
        path
    } else {
        scratch(label)
    }
}

fn copy_tree(source: &Path, destination: &Path) {
    std::fs::create_dir_all(destination).expect("dir");
    for entry in std::fs::read_dir(source).expect("read dir") {
        let entry = entry.expect("entry");
        let name = entry.file_name();
        if name == "target" || name == ".cargo" {
            continue;
        }
        let from = entry.path();
        let to = destination.join(&name);
        if from.is_dir() {
            copy_tree(&from, &to);
        } else {
            std::fs::copy(&from, &to).expect("copy file");
        }
    }
}

#[test]
fn launcher_preserves_cwd_argv_and_error_channels() {
    let Some(bash) = bash() else {
        eprintln!("skipping launcher test: no bash available");
        return;
    };
    let skill = scratch("cwd-skill");
    let target = launcher_target("cwd-target");
    let workdir = scratch("cwd-work");
    std::fs::write(workdir.join("request.json"), "{}\n").expect("marker");

    let version = run_launcher(&bash, &skill_root(), &target, &workdir, &["--version"]);
    assert!(version.status.success(), "{}", combined(&version));
    assert!(
        String::from_utf8_lossy(&version.stdout).contains("stock-data"),
        "{}",
        combined(&version)
    );

    // Business JSON stays on stdout; a research business error keeps exit code 2 and
    // writes its JSON error to stderr, i.e. argv/cwd pass-through reaches the real CLI.
    let missing_input = workdir.join("missing-collection.json");
    let invalid = run_launcher(
        &bash,
        &skill_root(),
        &target,
        &workdir,
        &[
            "research",
            "freeze",
            "--input",
            missing_input.to_str().expect("path"),
            "--output-root",
            workdir.join("out").to_str().expect("path"),
        ],
    );
    assert_eq!(invalid.status.code(), Some(2), "{}", combined(&invalid));
    assert!(
        String::from_utf8_lossy(&invalid.stderr).contains("\"error\""),
        "{}",
        combined(&invalid)
    );
    assert!(
        workdir.join("request.json").exists(),
        "the caller cwd must be preserved"
    );
    assert_eq!(
        std::fs::read_dir(&workdir).expect("workdir").count(),
        1,
        "the launcher must not write into the caller cwd"
    );
    let _ = skill;
    let _ = std::fs::remove_dir_all(&workdir);
    if std::env::var_os("STOCK_DATA_LAUNCHER_TEST_TARGET").is_none() {
        let _ = std::fs::remove_dir_all(&target);
    }
}

#[test]
fn launcher_recompiles_after_a_source_edit_and_reuses_the_cache() {
    let Some(bash) = bash() else {
        eprintln!("skipping source-edit launcher test: no bash available");
        return;
    };
    let root = scratch("edit");
    let skill = root.join("stock-data");
    std::fs::create_dir_all(skill.join("scripts")).expect("skill");
    copy_tree(
        &skill_root().join("scripts").join("rust"),
        &skill.join("scripts").join("rust"),
    );
    std::fs::copy(
        skill_root().join("scripts").join("run.sh"),
        skill.join("scripts").join("run.sh"),
    )
    .expect("run.sh");
    std::fs::copy(skill_root().join("SKILL.md"), skill.join("SKILL.md")).expect("SKILL.md");
    let target = std::env::var_os("STOCK_DATA_LAUNCHER_TEST_TARGET")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("target"));
    let workdir = root.join("work");
    std::fs::create_dir_all(&workdir).expect("work");

    let first = run_launcher(&bash, &skill, &target, &workdir, &["--version"]);
    assert!(first.status.success(), "{}", combined(&first));
    assert!(
        String::from_utf8_lossy(&first.stdout).contains("0.1.0"),
        "{}",
        combined(&first)
    );

    // Edit a real source constant: the launcher must rebuild (not reuse the stale
    // executable) and the behavior must change. Manifest version edits would also
    // legitimately require a Cargo.lock update, which `--locked` forbids.
    let main_rs = skill
        .join("scripts")
        .join("rust")
        .join("crates")
        .join("sd-cli")
        .join("src")
        .join("main.rs");
    let source = std::fs::read_to_string(&main_rs).expect("main.rs");
    let marker = "name = \"stock-data\",";
    if !source.contains(marker) {
        panic!("unexpected sd-cli main.rs: missing clap name");
    }
    std::fs::write(
        &main_rs,
        source.replacen(marker, "name = \"stock-data-edited\",", 1),
    )
    .expect("edit main.rs");
    let second = run_launcher(&bash, &skill, &target, &workdir, &["--version"]);
    assert!(second.status.success(), "{}", combined(&second));
    assert!(
        String::from_utf8_lossy(&second.stdout).contains("stock-data-edited"),
        "source edit did not take effect: {}",
        combined(&second)
    );

    // Restoring the source must rebuild back to the original name.
    std::fs::write(&main_rs, source).expect("restore main.rs");
    let third = run_launcher(&bash, &skill, &target, &workdir, &["--version"]);
    assert!(third.status.success(), "{}", combined(&third));
    let third_stdout = String::from_utf8_lossy(&third.stdout);
    assert!(
        third_stdout.contains("stock-data 0.1.0"),
        "{}",
        combined(&third)
    );

    let _ = std::fs::remove_dir_all(&root);
}
