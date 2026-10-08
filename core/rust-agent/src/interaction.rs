//! File-based interaction channel for front-ends without a terminal (apps/agent-desktop).
//!
//! A confirmation or question is written to `memory/tasks/<id>.request.json`; the runner then waits for
//! `memory/tasks/<id>.response.json` (`{"id", "approve": bool}` or `{"id", "answer": "..."}`). Unanswered
//! requests expire after `RESPONSE_TIMEOUT` and count as "declined" / "no answer". Run events are
//! forwarded to an inner interaction (console output); the UI follows the audit log for progress.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::agent::{Interaction, RunEvent};

pub const RESPONSE_TIMEOUT: Duration = Duration::from_secs(600);
const POLL_INTERVAL: Duration = Duration::from_millis(300);

pub fn request_path(tasks_dir: &Path, task_id: &str) -> PathBuf {
    tasks_dir.join(format!("{task_id}.request.json"))
}

pub fn response_path(tasks_dir: &Path, task_id: &str) -> PathBuf {
    tasks_dir.join(format!("{task_id}.response.json"))
}

pub struct FileInteraction<I: Interaction> {
    request: PathBuf,
    response: PathBuf,
    inner: I,
}

impl<I: Interaction> FileInteraction<I> {
    pub fn new(tasks_dir: &Path, task_id: &str, inner: I) -> Self {
        Self { request: request_path(tasks_dir, task_id), response: response_path(tasks_dir, task_id), inner }
    }

    fn exchange(&self, mut request: Value) -> Option<Value> {
        let id = uuid::Uuid::new_v4().simple().to_string();
        request["id"] = json!(id);
        request["created_at"] = json!(chrono::Utc::now().to_rfc3339());
        // A response left over from an earlier request must not answer this one.
        let _ = std::fs::remove_file(&self.response);
        let tmp = self.request.with_extension("json.tmp");
        if std::fs::write(&tmp, serde_json::to_vec_pretty(&request).ok()?).is_err()
            || std::fs::rename(&tmp, &self.request).is_err()
        {
            self.inner.event(RunEvent::Notice("could not write the interaction request file"));
            return None;
        }
        let deadline = Instant::now() + RESPONSE_TIMEOUT;
        let answer = loop {
            if Instant::now() >= deadline {
                break None;
            }
            if let Ok(bytes) = std::fs::read(&self.response) {
                if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
                    if value.get("id").and_then(Value::as_str) == Some(id.as_str()) {
                        break Some(value);
                    }
                }
            }
            std::thread::sleep(POLL_INTERVAL);
        };
        let _ = std::fs::remove_file(&self.request);
        let _ = std::fs::remove_file(&self.response);
        answer
    }
}

impl<I: Interaction> Interaction for FileInteraction<I> {
    fn event(&self, event: RunEvent<'_>) {
        self.inner.event(event);
    }

    fn confirm(&self, tool: &str, args: &Value, reasons: &[String]) -> bool {
        let request = json!({"kind": "confirm", "tool": tool, "args": args, "reasons": reasons});
        let approved = self
            .exchange(request)
            .and_then(|r| r.get("approve").and_then(Value::as_bool))
            .unwrap_or(false);
        self.inner.event(RunEvent::Notice(&format!("{tool} {} via the UI", if approved { "approved" } else { "declined" })));
        approved
    }

    fn ask(&self, question: &str) -> Option<String> {
        self.exchange(json!({"kind": "ask", "question": question}))
            .and_then(|r| r.get("answer").and_then(Value::as_str).map(String::from))
            .filter(|a| !a.trim().is_empty())
    }
}
