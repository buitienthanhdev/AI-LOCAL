//! Backend error classification. Decides retry vs. failover vs. returning the error to the caller.

use std::fmt;

use serde::Serialize;

use crate::types::Attempt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// The backend process/upstream is unreachable or broken: count against the breaker, fail over.
    BackendDown,
    /// Throttled: back off and retry the same backend, then fail over.
    RateLimited,
    /// A glitch worth retrying on the same backend (empty reply, transient upstream refusal).
    Transient,
    /// This backend cannot serve this request (no account for images, model not offered): fail over
    /// without penalising the breaker.
    Capability,
    /// Misconfiguration of the backend (bad local API key): fail over and count against the breaker.
    Config,
    /// The request itself is invalid (too long, malformed). Failing over would not help.
    ClientError,
    /// A browser adapter may already have sent the prompt (or is still generating the answer). Never
    /// retried on the same backend and not held against its breaker; other brains may still answer.
    SubmissionUncertain,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::BackendDown => "backend_down",
            ErrorKind::RateLimited => "rate_limited",
            ErrorKind::Transient => "transient",
            ErrorKind::Capability => "capability",
            ErrorKind::Config => "config",
            ErrorKind::ClientError => "client_error",
            ErrorKind::SubmissionUncertain => "submission_uncertain",
        }
    }

    pub fn penalises_breaker(self) -> bool {
        matches!(self, ErrorKind::BackendDown | ErrorKind::RateLimited | ErrorKind::Transient | ErrorKind::Config)
    }

    pub fn retry_same_backend(self) -> bool {
        matches!(self, ErrorKind::RateLimited | ErrorKind::Transient)
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone)]
pub struct BackendError {
    pub kind: ErrorKind,
    pub message: String,
    pub status: Option<u16>,
    /// A browser adapter's last observation of its page, when the exchange ended without an answer.
    pub last_observed_state: Option<String>,
}

impl BackendError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into(), status: None, last_observed_state: None }
    }

    pub fn with_observed(mut self, state: Option<String>) -> Self {
        self.last_observed_state = state;
        self
    }

    pub fn with_status(kind: ErrorKind, message: impl Into<String>, status: u16) -> Self {
        Self { kind, message: message.into(), status: Some(status), last_observed_state: None }
    }
}

/// Returned to the gateway's caller when no backend could serve the request.
#[derive(Debug, Clone)]
pub struct GatewayError {
    pub kind: ErrorKind,
    pub message: String,
    pub attempts: Vec<Attempt>,
}

impl GatewayError {
    pub fn new(kind: ErrorKind, message: impl Into<String>, attempts: Vec<Attempt>) -> Self {
        Self { kind, message: message.into(), attempts }
    }
}
