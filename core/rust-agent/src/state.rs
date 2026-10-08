//! Task lifecycle: state machine, step records and crash-safe persistence.
//!
//! The task record is written atomically after every transition (temp file + rename) and the
//! previous version is kept as `<id>.json.bak`. If both are destroyed, the record is rebuilt from the
//! audit log (`logs/tasks/<id>.jsonl`), which receives every step independently. See docs/recovery.md.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::ExecutionMode;
use crate::error::ToolErrorInfo;

pub const RECORD_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Pending,
    Planning,
    Executing,
    Observing,
    Verifying,
    Recovering,
    NeedsUser,
    Completed,
    Failed,
    Cancelled,
}

impl TaskStatus {
    pub fn is_terminal(self) -> bool {
        matches!(self, TaskStatus::Completed | TaskStatus::Failed | TaskStatus::Cancelled)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            TaskStatus::Pending => "pending",
            TaskStatus::Planning => "planning",
            TaskStatus::Executing => "executing",
            TaskStatus::Observing => "observing",
            TaskStatus::Verifying => "verifying",
            TaskStatus::Recovering => "recovering",
            TaskStatus::NeedsUser => "needs_user",
            TaskStatus::Completed => "completed",
            TaskStatus::Failed => "failed",
            TaskStatus::Cancelled => "cancelled",
        }
    }

    /// Allowed transitions of the execution loop
    /// (PLAN -> EXECUTE -> OBSERVE -> VERIFY -> CONTINUE/RECOVER -> COMPLETE).
    pub fn can_transition(self, to: TaskStatus) -> bool {
        use TaskStatus::*;
        if self == to {
            return true;
        }
        if matches!(to, Failed | Cancelled) {
            return !self.is_terminal();
        }
        matches!(
            (self, to),
            (Pending, Planning)
                | (Planning, Executing)
                | (Planning, Verifying)
                | (Planning, NeedsUser)
                | (Planning, Recovering)
                | (Executing, Observing)
                | (Executing, Recovering)
                | (Executing, Planning)
                | (Observing, Planning)
                | (Observing, Recovering)
                | (Recovering, Planning)
                | (Recovering, Executing)
                | (Verifying, Completed)
                | (Verifying, Planning)
                | (NeedsUser, Planning)
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepStatus {
    Running,
    Succeeded,
    Failed,
    Denied,
    Rejected,
    Cancelled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepRecord {
    pub step_id: String,
    pub index: u32,
    pub started_at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
    pub thought: String,
    pub tool: String,
    pub args: Value,
    pub input_hash: String,
    pub status: StepStatus,
    pub verification: Option<String>,
    pub observation: String,
    pub error: Option<ToolErrorInfo>,
    pub retry_count: u32,
    pub duration_ms: u64,
    pub backend: String,
    pub provider: String,
    pub artifacts: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskRecord {
    pub version: u32,
    pub task_id: String,
    pub goal: String,
    pub mode: ExecutionMode,
    pub status: TaskStatus,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub plan: Vec<String>,
    pub steps: Vec<StepRecord>,
    pub consecutive_failures: u32,
    pub worker_restarts: u32,
    pub llm_calls: u32,
    pub final_summary: Option<String>,
    pub failure_reason: Option<String>,
    pub artifacts: Vec<String>,
    pub escalation: Option<Value>,
    pub pending_question: Option<String>,
    pub runner_pid: Option<u32>,
}

impl TaskRecord {
    pub fn new(task_id: String, goal: String, mode: ExecutionMode) -> Self {
        let now = Utc::now();
        Self {
            version: RECORD_VERSION,
            task_id,
            goal,
            mode,
            status: TaskStatus::Pending,
            created_at: now,
            updated_at: now,
            plan: Vec::new(),
            steps: Vec::new(),
            consecutive_failures: 0,
            worker_restarts: 0,
            llm_calls: 0,
            final_summary: None,
            failure_reason: None,
            artifacts: Vec::new(),
            escalation: None,
            pending_question: None,
            runner_pid: None,
        }
    }

    pub fn transition(&mut self, to: TaskStatus) -> Result<()> {
        if !self.status.can_transition(to) {
            bail!("invalid task transition {} -> {}", self.status.as_str(), to.as_str());
        }
        self.status = to;
        self.updated_at = Utc::now();
        Ok(())
    }

    /// Put a stopped task back into the loop (`agent resume`).
    pub fn resume(&mut self) -> Result<()> {
        if self.status == TaskStatus::Completed {
            bail!("task {} is already completed", self.task_id);
        }
        self.status = TaskStatus::Planning;
        self.consecutive_failures = 0;
        self.failure_reason = None;
        self.updated_at = Utc::now();
        Ok(())
    }

    pub fn next_step_id(&self) -> (u32, String) {
        let index = self.steps.len() as u32 + 1;
        (index, format!("s{index:03}"))
    }
}

pub fn new_task_id() -> String {
    format!("{}-{}", Utc::now().format("%Y%m%d-%H%M%S"), &uuid::Uuid::new_v4().simple().to_string()[..6])
}

pub struct TaskStore {
    dir: PathBuf,
    audit_dir: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoadSource {
    Primary,
    Backup,
    AuditLog,
}

impl TaskStore {
    pub fn new(dir: PathBuf, audit_dir: PathBuf) -> Self {
        Self { dir, audit_dir }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn path(&self, task_id: &str) -> PathBuf {
        self.dir.join(format!("{task_id}.json"))
    }

    pub fn save(&self, record: &TaskRecord) -> Result<()> {
        std::fs::create_dir_all(&self.dir)?;
        let path = self.path(&record.task_id);
        let backup = path.with_extension("json.bak");
        let tmp = path.with_extension("json.tmp");
        let data = serde_json::to_vec_pretty(record)?;
        {
            use std::io::Write;
            let mut file = std::fs::File::create(&tmp).with_context(|| format!("creating {}", tmp.display()))?;
            file.write_all(&data)?;
            file.sync_all()?;
        }
        if path.is_file() && serde_json::from_slice::<TaskRecord>(&std::fs::read(&path)?).is_ok() {
            std::fs::copy(&path, &backup).with_context(|| format!("backing up {}", path.display()))?;
        }
        std::fs::rename(&tmp, &path).with_context(|| format!("replacing {}", path.display()))?;
        Ok(())
    }

    /// Load a task, falling back to the backup copy and then to reconstruction from the audit log.
    pub fn load(&self, task_id: &str) -> Result<(TaskRecord, LoadSource)> {
        let path = self.path(task_id);
        if let Ok(bytes) = std::fs::read(&path) {
            if let Ok(record) = serde_json::from_slice::<TaskRecord>(&bytes) {
                return Ok((record, LoadSource::Primary));
            }
        }
        if let Ok(bytes) = std::fs::read(path.with_extension("json.bak")) {
            if let Ok(record) = serde_json::from_slice::<TaskRecord>(&bytes) {
                return Ok((record, LoadSource::Backup));
            }
        }
        let record = crate::audit::reconstruct_task(&self.audit_dir, task_id)
            .with_context(|| format!("task {task_id}: state file and backup unreadable, audit log reconstruction failed"))?;
        Ok((record, LoadSource::AuditLog))
    }

    pub fn list(&self) -> Result<Vec<TaskRecord>> {
        let mut records = Vec::new();
        let Ok(entries) = std::fs::read_dir(&self.dir) else { return Ok(records) };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_some_and(|e| e == "json") {
                let id = path.file_stem().and_then(|s| s.to_str()).ok_or_else(|| anyhow!("bad file name"))?;
                if let Ok((record, _)) = self.load(id) {
                    records.push(record);
                }
            }
        }
        records.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
        Ok(records)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transitions_follow_the_loop() {
        let mut r = TaskRecord::new("t".into(), "g".into(), ExecutionMode::Normal);
        r.transition(TaskStatus::Planning).unwrap();
        r.transition(TaskStatus::Executing).unwrap();
        r.transition(TaskStatus::Observing).unwrap();
        r.transition(TaskStatus::Planning).unwrap();
        assert!(r.transition(TaskStatus::Completed).is_err(), "completion must go through verification");
        r.transition(TaskStatus::Verifying).unwrap();
        r.transition(TaskStatus::Completed).unwrap();
        assert!(r.transition(TaskStatus::Planning).is_err());
        assert!(r.resume().is_err());
    }

    #[test]
    fn save_load_with_backup_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let store = TaskStore::new(dir.path().join("tasks"), dir.path().join("audit"));
        let mut r = TaskRecord::new("abc".into(), "goal".into(), ExecutionMode::Normal);
        store.save(&r).unwrap();
        r.transition(TaskStatus::Planning).unwrap();
        store.save(&r).unwrap();
        std::fs::write(dir.path().join("tasks").join("abc.json"), b"{corrupt").unwrap();
        let (loaded, source) = store.load("abc").unwrap();
        assert_eq!(source, LoadSource::Backup);
        assert_eq!(loaded.task_id, "abc");
    }
}
