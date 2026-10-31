//! R17 checks: registry completeness (T43) and documented versions (T44).

use crate::selfcheck::registry::{implementation, registry, CheckStatus};
use crate::selfcheck_generated::{
    ACTIVE_CHECKS, ACTIVE_CHECK_COUNT, RETIRED_CHECKS, RETIRED_CHECK_COUNT,
};
use std::collections::BTreeSet;

/// T43: every active check is registered exactly once, retirements have reasons and the
/// registry status always matches the actual implementation lookup.
pub fn legacy_t43() -> Result<(), String> {
    if ACTIVE_CHECKS.len() != ACTIVE_CHECK_COUNT {
        return Err(format!(
            "ACTIVE_CHECK_COUNT {ACTIVE_CHECK_COUNT} != {} entries",
            ACTIVE_CHECKS.len()
        ));
    }
    let mut ids: BTreeSet<&str> = BTreeSet::new();
    for spec in ACTIVE_CHECKS {
        if !ids.insert(spec.id) {
            return Err(format!("check {} is registered more than once", spec.id));
        }
    }
    if RETIRED_CHECKS.len() != RETIRED_CHECK_COUNT {
        return Err(format!(
            "RETIRED_CHECK_COUNT {RETIRED_CHECK_COUNT} != {} entries",
            RETIRED_CHECKS.len()
        ));
    }
    let mut retired: BTreeSet<&str> = BTreeSet::new();
    for (name, reason) in RETIRED_CHECKS {
        if !retired.insert(name) {
            return Err(format!("retired check {name} is listed more than once"));
        }
        if reason.trim().is_empty() {
            return Err(format!("retired check {name} has no reason"));
        }
    }
    for id in &ids {
        if retired.contains(id) {
            return Err(format!("check {id} is both registered and retired"));
        }
    }
    let rows = registry();
    if rows.len() != ACTIVE_CHECKS.len() {
        return Err("registry row count does not match the active inventory".to_string());
    }
    let mut implemented = 0usize;
    for row in &rows {
        let lookup = implementation(row.spec.id);
        let matches = match row.status {
            CheckStatus::Implemented => lookup.is_some(),
            CheckStatus::NotImplemented => lookup.is_none(),
        };
        if !matches {
            return Err(format!(
                "registry status for {} disagrees with implementation()",
                row.spec.id
            ));
        }
        if row.status == CheckStatus::Implemented {
            implemented += 1;
        }
    }
    if implemented != ACTIVE_CHECKS.len() {
        return Err(format!(
            "{implemented}/{} active checks are implemented",
            ACTIVE_CHECKS.len()
        ));
    }
    Ok(())
}

/// Mounted Skill root: explicit env override first, then the checkout layout.
fn skill_root() -> std::path::PathBuf {
    std::env::var("STOCK_DATA_SKILL_ROOT")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.."))
}

fn metadata_field<'a>(metadata: &'a str, key: &str) -> Option<&'a str> {
    let marker = format!("{key}:");
    let start = metadata.find(&marker)? + marker.len();
    let rest = metadata[start..].trim_start();
    let rest = rest.strip_prefix('"')?;
    let end = rest.find('"')?;
    Some(&rest[..end])
}

/// T44: SKILL.md metadata and (when present) the repository README advertise the same
/// versions as the Rust runtime constants.
pub fn legacy_t44() -> Result<(), String> {
    let skill_root = skill_root();
    let skill_path = skill_root.join("SKILL.md");
    let skill = std::fs::read_to_string(&skill_path)
        .map_err(|error| format!("cannot read {}: {error}", skill_path.display()))?;
    let metadata_start = skill
        .find("metadata:")
        .ok_or("SKILL.md has no metadata block")?;
    let metadata_end = match skill[metadata_start..].find('}') {
        Some(offset) => metadata_start + offset,
        None => skill.len(),
    };
    let metadata = &skill[metadata_start..metadata_end];
    let skill_version =
        metadata_field(metadata, "version").ok_or("SKILL.md has no metadata version")?;
    let runtime_binding = metadata_field(metadata, "runtime")
        .ok_or("SKILL.md does not declare the runtime version it ships")?;
    if skill_version != sd_core::versions::SKILL_VERSION {
        return Err(format!(
            "SKILL version {skill_version} != runtime constant {}",
            sd_core::versions::SKILL_VERSION
        ));
    }
    if runtime_binding != sd_core::versions::RUNTIME_VERSION {
        return Err(format!(
            "SKILL runtime {runtime_binding} != package {}",
            sd_core::versions::RUNTIME_VERSION
        ));
    }
    let readme_path = skill_root.join("..").join("README.md");
    if !readme_path.is_file() {
        return Ok(());
    }
    let readme = std::fs::read_to_string(&readme_path)
        .map_err(|error| format!("cannot read {}: {error}", readme_path.display()))?;
    let needle = "当前 Skill `";
    let start = readme.find(needle).ok_or("README.md has no version line")? + needle.len();
    let documented_skill = readme[start..]
        .split('`')
        .next()
        .ok_or("README.md has no Skill version")?;
    let runtime_needle = "research runtime `";
    let runtime_start = readme
        .find(runtime_needle)
        .ok_or("README.md has no runtime version")?;
    let documented_runtime = readme[runtime_start + runtime_needle.len()..]
        .split('`')
        .next()
        .ok_or("README.md has no runtime version")?;
    if documented_skill != skill_version {
        return Err(format!(
            "README Skill {documented_skill} != SKILL {skill_version}"
        ));
    }
    if documented_runtime != sd_core::versions::RUNTIME_VERSION {
        return Err(format!(
            "README runtime {documented_runtime} != package {}",
            sd_core::versions::RUNTIME_VERSION
        ));
    }
    Ok(())
}

/// Registry entries for this package.
pub type CheckFn = fn() -> Result<(), String>;

/// Registry entries for this package.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![("T43", legacy_t43), ("T44", legacy_t44)]
}
