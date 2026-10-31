//! Error classes and reason codes.
//!
//! The legacy runtime distinguishes two channels and the Rust CLI must keep both:
//!
//! * `global_stock_data.py` writes a JSON envelope to **stdout** and exits **1**;
//! * `quant_research.py` writes the JSON error to **stderr** and exits **2**.
//!
//! `error_type` therefore mirrors the Python exception class name, and `code`
//! mirrors an attached machine-readable code (`PolicyError.code`), so a host that
//! already branches on those strings keeps working.

use std::fmt;

/// Canonical error kind. The variant carries the Python-compatible class name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ErrorKind {
    /// `ValueError` - input/contract violation.
    Validation,
    /// `PolicyError` - validation failure with a stable policy code.
    Policy,
    /// A provider-specific exception (`ConfigurationError`, `ProviderError`, ...).
    Provider(String),
    /// `OSError`/`FileNotFoundError`/`PermissionError` family.
    Io(String),
    /// The command exists but this build does not implement it yet.
    NotImplemented(String),
}

/// Error value shared by every crate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SdError {
    kind: ErrorKind,
    code: Option<String>,
    message: String,
}

impl SdError {
    /// `ValueError` equivalent.
    pub fn validation(message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Validation,
            code: None,
            message: message.into(),
        }
    }

    /// `PolicyError` equivalent carrying a stable code.
    pub fn policy(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Policy,
            code: Some(code.into()),
            message: message.into(),
        }
    }

    /// Provider exception with its original class name.
    pub fn provider(class: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Provider(class.into()),
            code: None,
            message: message.into(),
        }
    }

    /// Filesystem/OS error with the Python exception class name.
    pub fn io(class: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Io(class.into()),
            code: None,
            message: message.into(),
        }
    }

    /// A command that exists but is not implemented by this build. Never reported
    /// as `success`; the CLI maps it to a non-zero exit code.
    pub fn not_implemented(command: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::NotImplemented(command.into()),
            code: None,
            message: String::new(),
        }
    }

    /// Python-compatible exception class name.
    pub fn error_type(&self) -> &str {
        match &self.kind {
            ErrorKind::Validation => "ValueError",
            ErrorKind::Policy => "PolicyError",
            ErrorKind::Provider(class) => class,
            ErrorKind::Io(class) => class,
            ErrorKind::NotImplemented(_) => "NotImplementedError",
        }
    }

    /// Attached machine-readable code, when the failure has one.
    pub fn code(&self) -> Option<&str> {
        self.code.as_deref()
    }

    /// Human-readable message.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// True when this is a policy rejection (a host may branch on `code`).
    pub fn is_policy(&self) -> bool {
        matches!(self.kind, ErrorKind::Policy)
    }

    /// True when this build simply has not implemented the command.
    pub fn is_not_implemented(&self) -> bool {
        matches!(self.kind, ErrorKind::NotImplemented(_))
    }

    /// Attach a code when the underlying error does not already carry one.
    pub fn with_code(self, code: impl Into<String>) -> Self {
        match self.code {
            Some(_) => self,
            None => Self {
                code: Some(code.into()),
                ..self
            },
        }
    }
}

impl fmt::Display for SdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.kind {
            ErrorKind::NotImplemented(command) => {
                write!(
                    formatter,
                    "command is not implemented by this build: {command}"
                )
            }
            _ => formatter.write_str(&self.message),
        }
    }
}

impl std::error::Error for SdError {}

impl From<std::io::Error> for SdError {
    fn from(error: std::io::Error) -> Self {
        let class = match error.kind() {
            std::io::ErrorKind::NotFound => "FileNotFoundError",
            std::io::ErrorKind::PermissionDenied => "PermissionError",
            std::io::ErrorKind::AlreadyExists => "FileExistsError",
            _ => "OSError",
        };
        Self::io(class, error.to_string())
    }
}

impl From<serde_json::Error> for SdError {
    fn from(error: serde_json::Error) -> Self {
        Self::validation(error.to_string())
    }
}

/// Convenient result alias.
pub type SdResult<T> = Result<T, SdError>;
