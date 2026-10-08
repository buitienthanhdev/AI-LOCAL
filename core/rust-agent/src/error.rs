//! Deterministic error model shared with the Python worker (`lga_worker/errors.py`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorCode {
    InvalidArgument,
    UnknownTool,
    NotFound,
    WindowNotFound,
    ElementNotFound,
    StaleElement,
    Timeout,
    Cancelled,
    PolicyDenied,
    ElevationRequired,
    BackendUnavailable,
    Busy,
    VerificationFailed,
    ExecutionFailed,
    Unsupported,
    Conflict,
    Internal,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::InvalidArgument => "INVALID_ARGUMENT",
            ErrorCode::UnknownTool => "UNKNOWN_TOOL",
            ErrorCode::NotFound => "NOT_FOUND",
            ErrorCode::WindowNotFound => "WINDOW_NOT_FOUND",
            ErrorCode::ElementNotFound => "ELEMENT_NOT_FOUND",
            ErrorCode::StaleElement => "STALE_ELEMENT",
            ErrorCode::Timeout => "TIMEOUT",
            ErrorCode::Cancelled => "CANCELLED",
            ErrorCode::PolicyDenied => "POLICY_DENIED",
            ErrorCode::ElevationRequired => "ELEVATION_REQUIRED",
            ErrorCode::BackendUnavailable => "BACKEND_UNAVAILABLE",
            ErrorCode::Busy => "BUSY",
            ErrorCode::VerificationFailed => "VERIFICATION_FAILED",
            ErrorCode::ExecutionFailed => "EXECUTION_FAILED",
            ErrorCode::Unsupported => "UNSUPPORTED",
            ErrorCode::Conflict => "CONFLICT",
            ErrorCode::Internal => "INTERNAL",
        }
    }

    /// Recovery guidance given to the planner after this error (docs/recovery.md).
    pub fn recovery_hint(self) -> &'static str {
        match self {
            ErrorCode::InvalidArgument => "Fix the arguments so they match the tool's input schema.",
            ErrorCode::UnknownTool => "Use only tools from the tool list.",
            ErrorCode::NotFound => "The target does not exist. Re-check the path/name or create it first.",
            ErrorCode::WindowNotFound => {
                "The window is gone. Call desktop.list_windows, then refocus or relaunch the application."
            }
            ErrorCode::ElementNotFound => {
                "Re-inspect the window (desktop.inspect_ui / desktop.find_element). If UI Automation cannot \
                 see it, take a screenshot and use vision.locate, then click the returned coordinates."
            }
            ErrorCode::StaleElement => {
                "The UI changed. Call desktop.inspect_ui again and use the fresh element_id."
            }
            ErrorCode::Timeout => "Observe the current state before retrying; the action may have partly happened.",
            ErrorCode::Cancelled => "The task was cancelled.",
            ErrorCode::PolicyDenied => "This action is not allowed in the current mode. Choose another approach or ask the user.",
            ErrorCode::ElevationRequired => {
                "Administrator rights are required. Do not try to bypass UAC; report the escalation request to the user."
            }
            ErrorCode::BackendUnavailable => "The backend is restarting. Retry once; if it fails again, use another approach.",
            ErrorCode::Busy => "The application is busy. Wait briefly and retry.",
            ErrorCode::VerificationFailed => {
                "The action ran but its effect could not be confirmed. Observe the state, then fix or retry differently."
            }
            ErrorCode::ExecutionFailed => "Observe the state (e.g. a dialog may be blocking) and re-plan.",
            ErrorCode::Unsupported => "Use a different tool for this element or application.",
            ErrorCode::Conflict => "The target already exists or is in a conflicting state; decide explicitly (e.g. overwrite).",
            ErrorCode::Internal => "An internal error occurred; try a different approach.",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolErrorInfo {
    pub code: ErrorCode,
    pub message: String,
    pub retryable: bool,
    #[serde(default)]
    pub details: Value,
}

impl ToolErrorInfo {
    pub fn new(code: ErrorCode, message: impl Into<String>, retryable: bool) -> Self {
        Self { code, message: message.into(), retryable, details: Value::Null }
    }

    pub fn with_details(mut self, details: Value) -> Self {
        self.details = details;
        self
    }
}

impl std::fmt::Display for ToolErrorInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}
