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

pub const RESPONSE_TIMEOUT: Duration = Duration::from_secs(1800);
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
    timeout: Duration,
}

impl<I: Interaction> FileInteraction<I> {
    pub fn new(tasks_dir: &Path, task_id: &str, inner: I) -> Self {
        Self { request: request_path(tasks_dir, task_id), response: response_path(tasks_dir, task_id), inner, timeout: RESPONSE_TIMEOUT }
    }

    /// How long a request waits for the person (tests use a short one).
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
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
        let deadline = Instant::now() + self.timeout;
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

#[cfg(test)]
mod tests {
    use super::*;

    struct Quiet;
    impl Interaction for Quiet {
        fn event(&self, _event: RunEvent<'_>) {}
        fn confirm(&self, _tool: &str, _args: &Value, _reasons: &[String]) -> bool {
            true // an inner console that would say yes: the file channel must not fall back to it
        }
        fn ask(&self, _question: &str) -> Option<String> {
            None
        }
    }

    fn ui(dir: &Path) -> FileInteraction<Quiet> {
        FileInteraction::new(dir, "t1", Quiet).with_timeout(Duration::from_millis(400))
    }

    #[test]
    fn no_answer_in_time_declines_and_leaves_no_request_behind() {
        let dir = tempfile::tempdir().unwrap();
        let ui = ui(dir.path());
        assert!(!ui.confirm_always("shell.exec", &json!({"command": "shutdown /s"}), &["x".into()]));
        assert!(!ui.confirm("shell.exec", &json!({}), &[]));
        assert!(!request_path(dir.path(), "t1").exists());
    }

    #[test]
    fn a_choice_question_carries_its_options_to_the_ui_and_returns_the_answer() {
        let dir = tempfile::tempdir().unwrap();
        let tasks = dir.path().to_path_buf();
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Value::Null));
        let answerer = {
            let (tasks, seen) = (tasks.clone(), seen.clone());
            std::thread::spawn(move || {
                let req = request_path(&tasks, "t1");
                for _ in 0..300 {
                    if let Ok(bytes) = std::fs::read(&req) {
                        let v = serde_json::from_slice::<Value>(&bytes).unwrap();
                        *seen.lock().unwrap() = v.clone();
                        std::fs::write(response_path(&tasks, "t1"), json!({"id": v["id"], "answer": "- Word
- PDF"}).to_string()).unwrap();
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
            })
        };
        let ui = FileInteraction::new(&tasks, "t1", Quiet).with_timeout(Duration::from_secs(5));
        let answer = ui.ask_choice("Định dạng nào?", &["Word".into(), "PDF".into()], true);
        answerer.join().unwrap();
        assert_eq!(answer.as_deref(), Some("- Word
- PDF"));
        let v = seen.lock().unwrap().clone();
        assert_eq!(v["kind"], "ask");
        assert_eq!(v["options"], json!(["Word", "PDF"]));
        assert_eq!(v["multi"], true);
    }

    #[test]
    fn only_an_answer_to_this_request_counts() {
        let dir = tempfile::tempdir().unwrap();
        let tasks = dir.path().to_path_buf();
        // a stale or forged response (wrong id) is ignored; an explicit refusal is a refusal
        let answerer = {
            let tasks = tasks.clone();
            std::thread::spawn(move || {
                let req = request_path(&tasks, "t1");
                for _ in 0..100 {
                    if let Ok(bytes) = std::fs::read(&req) {
                        let id = serde_json::from_slice::<Value>(&bytes).unwrap()["id"].as_str().unwrap().to_string();
                        std::fs::write(response_path(&tasks, "t1"), json!({"id": "not-this-request", "approve": true}).to_string()).unwrap();
                        std::thread::sleep(Duration::from_millis(120));
                        std::fs::write(response_path(&tasks, "t1"), json!({"id": id, "approve": false}).to_string()).unwrap();
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
            })
        };
        let ui = FileInteraction::new(&tasks, "t1", Quiet).with_timeout(Duration::from_secs(5));
        assert!(!ui.confirm_always("shell.exec", &json!({}), &[]));
        answerer.join().unwrap();
    }

    #[test]
    fn an_explicit_yes_to_this_request_is_the_only_approval() {
        let dir = tempfile::tempdir().unwrap();
        let tasks = dir.path().to_path_buf();
        let answerer = {
            let tasks = tasks.clone();
            std::thread::spawn(move || {
                let req = request_path(&tasks, "t1");
                for _ in 0..100 {
                    if let Ok(bytes) = std::fs::read(&req) {
                        let v = serde_json::from_slice::<Value>(&bytes).unwrap();
                        assert_eq!(v["always"], true, "the UI is told this is an always-confirm request");
                        std::fs::write(response_path(&tasks, "t1"), json!({"id": v["id"], "approve": true}).to_string()).unwrap();
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
            })
        };
        let ui = FileInteraction::new(&tasks, "t1", Quiet).with_timeout(Duration::from_secs(5));
        assert!(ui.confirm_always("shell.exec", &json!({}), &[]));
        answerer.join().unwrap();
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

    fn confirm_always(&self, tool: &str, args: &Value, reasons: &[String]) -> bool {
        // Same channel (a person pressing the button in the chat window), flagged so the UI can say what is at stake.
        let request = json!({"kind": "confirm", "always": true, "tool": tool, "args": args, "reasons": reasons});
        let approved = self
            .exchange(request)
            .and_then(|r| r.get("approve").and_then(Value::as_bool))
            .unwrap_or(false);
        self.inner.event(RunEvent::Notice(&format!("{tool} (always-confirm) {} via the UI", if approved { "approved" } else { "declined" })));
        approved
    }

    fn ask(&self, question: &str) -> Option<String> {
        self.exchange(json!({"kind": "ask", "question": question}))
            .and_then(|r| r.get("answer").and_then(Value::as_str).map(String::from))
            .filter(|a| !a.trim().is_empty())
    }

    fn ask_choice(&self, question: &str, options: &[String], multi: bool) -> Option<String> {
        self.exchange(json!({"kind": "ask", "question": question, "options": options, "multi": multi}))
            .and_then(|r| r.get("answer").and_then(Value::as_str).map(String::from))
            .filter(|a| !a.trim().is_empty())
    }
}
