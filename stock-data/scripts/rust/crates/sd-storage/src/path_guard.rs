//! Path containment and link checks.
//!
//! Every generated artifact name is one portable filename component; an output
//! destination must resolve inside its declared root. Cleanup later needs stricter
//! identity checks (R10), but the rules below already prevent `..`, absolute paths,
//! separators and pre-existing links that escape the root.

use std::path::{Path, PathBuf};

use sd_core::error::{SdError, SdResult};

/// Accept one portable filename component (letters, digits, `.`, `_`, `-`).
///
/// Mirrors `paths.validate_path_component`: rejects `""`, `"."`, `".."` and
/// anything containing a separator, drive letter or NUL.
pub fn validate_path_component(value: &str, label: &str) -> SdResult<String> {
    let valid = !value.is_empty()
        && value != "."
        && value != ".."
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        });
    if valid {
        Ok(value.to_string())
    } else {
        Err(SdError::validation(format!(
            "{label} must be a non-empty safe filename component (letters, digits, '.', '_', '-')"
        )))
    }
}

/// True when the entry itself is a symlink, junction or other reparse point.
pub fn is_link(path: &Path) -> bool {
    let Ok(metadata) = std::fs::symlink_metadata(path) else {
        return false;
    };
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return true;
        }
    }
    false
}

/// Resolve a directory that must exist and must not be a link.
pub fn real_directory(path: &Path, label: &str) -> SdResult<PathBuf> {
    if is_link(path) || !path.is_dir() {
        return Err(SdError::validation(format!(
            "{label} must be an existing directory, not a link"
        )));
    }
    path.canonicalize().map_err(SdError::from)
}

/// Lexically resolve `.`/`..` without touching the filesystem (non-strict resolve).
fn lexical_resolve(path: &Path) -> PathBuf {
    let mut resolved = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                resolved.pop();
            }
            other => resolved.push(other.as_os_str()),
        }
    }
    resolved
}

/// Reject an unsafe name and return the confined absolute destination.
///
/// The output root may itself be a user-selected symlink; its resolved directory is
/// the boundary. An existing destination that resolves outside the root fails.
pub fn safe_output_destination(output_root: &Path, name: &str) -> SdResult<PathBuf> {
    validate_path_component(name, "output name")?;
    let root = match output_root.canonicalize() {
        Ok(resolved) => resolved,
        Err(_) => lexical_resolve(output_root),
    };
    let destination = root.join(name);
    if destination.exists() {
        if let Ok(resolved) = destination.canonicalize() {
            if resolved.parent() != Some(root.as_path()) {
                return Err(SdError::validation(
                    "output destination resolves outside output_root",
                ));
            }
        }
    }
    Ok(destination)
}

/// True when `path` is the directory `root` or inside it (lexical, after resolving).
pub fn path_contains(root: &Path, path: &Path) -> bool {
    let Ok(root) = root.canonicalize() else {
        return false;
    };
    let candidate = match path.canonicalize() {
        Ok(resolved) => resolved,
        Err(_) => lexical_resolve(path),
    };
    candidate.starts_with(&root)
}

/// Refuse to overwrite an existing artifact (Python `open("x")` semantics).
pub fn require_absent(path: &Path) -> SdResult<()> {
    if path.exists() {
        return Err(SdError::io(
            "FileExistsError",
            format!("file already exists: {}", path.display()),
        ));
    }
    Ok(())
}
