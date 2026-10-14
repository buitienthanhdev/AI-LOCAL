//! Transaction contract between the gateway and an adapter that drives a *web page* (Chrome Gemini).
//!
//! Plain HTTP backends are request/response: a failed call can be repeated. A browser adapter is not:
//! once the prompt may have been typed into a page, repeating it posts a second question. So adapters
//! report a transaction status, and this module is the only place that turns it into the gateway's
//! error classes. It knows nothing about Chrome, selectors or Gemini's markup.
//!
//! `Completed` means the exchange finished technically. It does not say the answer is right.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::errors::{BackendError, ErrorKind};
use crate::types::Message;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TransactionStatus {
    /// Nothing reached the page. Safe to retry or to fail over.
    NotSubmitted,
    /// The prompt may have been sent. Never sent again.
    SubmissionUncertain,
    /// Sent; the answer was not proven complete.
    Generating,
    Completed,
    /// Sent, and the page reported an error.
    Failed,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WireError {
    pub class: String,
    #[serde(default)]
    pub message: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Transaction {
    pub transaction_id: String,
    pub status: TransactionStatus,
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub error: Option<WireError>,
    #[serde(default)]
    pub metadata: Value,
}

/// An answer the adapter vouches for, with the transaction facts that belong to it.
#[derive(Debug, Clone, PartialEq)]
pub struct Completed {
    pub content: String,
    /// The model label the page showed, or None when it could not be read (never filled from configuration).
    pub model_verified: Option<String>,
    pub transaction: Value,
}

fn summary(tx: &Transaction) -> Value {
    json!({
        "transaction_id": tx.transaction_id,
        "status": format!("{:?}", tx.status),
        "provider": tx.metadata.get("provider").cloned().unwrap_or(Value::Null),
        "model_verified": tx.metadata.get("model_verified").cloned().unwrap_or(Value::Null),
        "signals": tx.metadata.get("signals").cloned().unwrap_or(Value::Null),
    })
}

/// Turn an adapter's transaction into an answer or a classified error.
///
/// Only `NotSubmitted` errors are ones another attempt may follow; everything after the point of no
/// return becomes `SubmissionUncertain`, which the gateway neither retries nor counts against the breaker.
pub fn interpret(provider: &str, tx: &Transaction) -> Result<Completed, BackendError> {
    let class = tx.error.as_ref().map_or("", |e| e.class.as_str());
    let message = tx.error.as_ref().map_or("", |e| e.message.as_str());
    match tx.status {
        TransactionStatus::Completed if !tx.content.trim().is_empty() => Ok(Completed {
            content: tx.content.clone(),
            model_verified: tx.metadata.get("model_verified").and_then(Value::as_str).map(str::to_string),
            transaction: summary(tx),
        }),
        TransactionStatus::Completed => Err(BackendError::new(
            ErrorKind::SubmissionUncertain,
            format!("{provider}: transaction {} completed with an empty answer", tx.transaction_id),
        )),
        TransactionStatus::NotSubmitted => {
            let kind = match class {
                "busy" => ErrorKind::RateLimited,
                // shutting_down: the adapter is stopping and nothing was sent; the gateway starts it again or fails over.
                "browser_unavailable" | "connection_lost" | "shutting_down" => ErrorKind::BackendDown,
                "not_logged_in" | "selectors_unverified" | "model_mismatch" => ErrorKind::Config,
                "unsupported" => ErrorKind::Capability,
                "duplicate" => ErrorKind::ClientError,
                _ => ErrorKind::Transient,
            };
            Err(BackendError::new(kind, format!("{provider}: not submitted ({class}): {message}")))
        }
        status => {
            // What the page last showed, from the adapter's own signals (absent for older adapters).
            let observed = tx.metadata.pointer("/signals/last_observed_state").and_then(Value::as_str).map(str::to_string);
            let seen = observed.as_deref().map(|s| format!("; last_observed_state={s}")).unwrap_or_default();
            Err(BackendError::new(
                ErrorKind::SubmissionUncertain,
                format!("{provider}: transaction {} is {:?} ({class}): {message}{seen}; the prompt is not sent again", tx.transaction_id, status),
            )
            .with_observed(observed))
        }
    }
}

/// One prompt for a page that holds a single new conversation per transaction.
///
/// Images are refused (`Capability`): dropping them silently would answer a different question.
pub fn flatten_prompt(provider: &str, messages: &[Message]) -> Result<String, BackendError> {
    if messages.iter().any(Message::has_images) {
        return Err(BackendError::new(ErrorKind::Capability, format!("{provider}: images are not supported by this provider")));
    }
    let mut out = String::new();
    for message in messages {
        let text = message.text();
        if text.trim().is_empty() {
            continue;
        }
        if messages.len() > 1 {
            out.push_str(&format!("[{}]\n", message.role));
        }
        out.push_str(&text);
        out.push_str("\n\n");
    }
    let prompt = out.trim().to_string();
    if prompt.is_empty() {
        return Err(BackendError::new(ErrorKind::ClientError, format!("{provider}: empty prompt")));
    }
    Ok(prompt)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Part;

    fn tx(value: Value) -> Transaction {
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn wire_shape_of_the_python_adapter_parses() {
        let t = tx(json!({
            "transaction_id": "t1", "status": "COMPLETED", "content": "xin chào", "error": null,
            "metadata": {"provider": "chrome_gemini", "model_verified": "Pro", "started_at": 1.0, "finished_at": 2.0, "signals": {"turns": 1}}
        }));
        let done = interpret("chrome_gemini", &t).unwrap();
        assert_eq!(done.content, "xin chào");
        assert_eq!(done.model_verified.as_deref(), Some("Pro"));
        assert_eq!(done.transaction["status"], "Completed");
    }

    #[test]
    fn unknown_model_is_not_invented() {
        let t = tx(json!({"transaction_id": "t", "status": "COMPLETED", "content": "a", "metadata": {"model_verified": null}}));
        assert_eq!(interpret("p", &t).unwrap().model_verified, None);
    }

    #[test]
    fn not_submitted_classes_map_to_failover_safe_kinds() {
        for (class, kind) in [
            ("busy", ErrorKind::RateLimited),
            ("browser_unavailable", ErrorKind::BackendDown),
            ("shutting_down", ErrorKind::BackendDown),
            ("not_logged_in", ErrorKind::Config),
            ("selectors_unverified", ErrorKind::Config),
            ("model_mismatch", ErrorKind::Config),
            ("unsupported", ErrorKind::Capability),
            ("conversation_not_clean", ErrorKind::Transient),
            ("duplicate", ErrorKind::ClientError),
        ] {
            let t = tx(json!({"transaction_id": "t", "status": "NOT_SUBMITTED", "error": {"class": class, "message": "m"}}));
            assert_eq!(interpret("p", &t).unwrap_err().kind, kind, "{class}");
        }
    }

    #[test]
    fn nothing_after_the_point_of_no_return_is_retryable() {
        for (status, class) in [
            ("SUBMISSION_UNCERTAIN", "incomplete"),
            ("GENERATING", "incomplete"),
            ("FAILED", "incomplete"),
            ("GENERATING", "shutdown_interrupted"),
            ("SUBMISSION_UNCERTAIN", "shutdown_interrupted"),
        ] {
            let t = tx(json!({"transaction_id": "t", "status": status, "error": {"class": class, "message": "slow"}}));
            let error = interpret("p", &t).unwrap_err();
            assert_eq!(error.kind, ErrorKind::SubmissionUncertain, "{status}");
            assert!(!error.kind.retry_same_backend());
            assert!(!error.kind.penalises_breaker());
        }
    }

    #[test]
    fn last_observed_state_travels_with_the_uncertain_error_without_changing_its_kind() {
        let t = tx(json!({"transaction_id": "t", "status": "GENERATING", "error": {"class": "incomplete", "message": "slow"},
                          "metadata": {"signals": {"last_observed_state": "GENERATING", "last_observed_at": 1.0}}}));
        let error = interpret("p", &t).unwrap_err();
        assert_eq!(error.kind, ErrorKind::SubmissionUncertain);
        assert_eq!(error.last_observed_state.as_deref(), Some("GENERATING"));
        assert!(error.message.contains("last_observed_state=GENERATING"));
        let old = tx(json!({"transaction_id": "t", "status": "SUBMISSION_UNCERTAIN", "error": {"class": "incomplete", "message": "x"}}));
        assert_eq!(interpret("p", &old).unwrap_err().last_observed_state, None);
    }

    #[test]
    fn empty_completed_answer_is_not_an_answer() {
        let t = tx(json!({"transaction_id": "t", "status": "COMPLETED", "content": "  "}));
        assert_eq!(interpret("p", &t).unwrap_err().kind, ErrorKind::SubmissionUncertain);
    }

    #[test]
    fn images_are_refused_and_text_is_flattened() {
        let with_image = [Message { role: "user".into(), parts: vec![Part::Text("what is this".into()), Part::Image("a1".into())] }];
        assert_eq!(flatten_prompt("p", &with_image).unwrap_err().kind, ErrorKind::Capability);
        let one = [Message { role: "user".into(), parts: vec![Part::Text("hi".into())] }];
        assert_eq!(flatten_prompt("p", &one).unwrap(), "hi");
        let two = [
            Message { role: "system".into(), parts: vec![Part::Text("rules".into())] },
            Message { role: "user".into(), parts: vec![Part::Text("hi".into())] },
        ];
        assert_eq!(flatten_prompt("p", &two).unwrap(), "[system]\nrules\n\n[user]\nhi");
        assert_eq!(flatten_prompt("p", &[]).unwrap_err().kind, ErrorKind::ClientError);
    }
}
