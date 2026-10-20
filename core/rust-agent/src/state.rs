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
    Paused,
    Completed,
    Failed,
    Cancelled,
}

impl TaskStatus {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            TaskStatus::Completed | TaskStatus::Failed | TaskStatus::Cancelled
        )
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
            TaskStatus::Paused => "paused",
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
                | (Planning, Paused)
                | (Paused, Planning)
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

/// A file the user attached to the request (saved under `artifacts/<task_id>/attachments/`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UserAttachment {
    pub name: String,
    /// Absolute path of the saved copy.
    pub path: String,
    /// "image" (shown to the planner), "document" (read with document.read / office.*) or "media".
    pub kind: String,
    pub size: u64,
}

impl UserAttachment {
    pub fn from_path(path: &Path) -> Self {
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("file")
            .to_string();
        let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
        Self {
            name,
            path: path.display().to_string(),
            kind: attachment_kind(path).to_string(),
            size,
        }
    }

    pub fn is_image(&self) -> bool {
        self.kind == "image"
    }

    /// "report.pdf (PDF, 1.2 MB)".
    pub fn describe(&self) -> String {
        let ext = Path::new(&self.path)
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_ascii_uppercase();
        let size = if self.size >= 1_000_000 {
            format!("{:.1} MB", self.size as f64 / 1_000_000.0)
        } else {
            format!("{} KB", self.size.div_ceil(1000))
        };
        format!(
            "{} ({}{}{})",
            self.path,
            ext,
            if ext.is_empty() { "" } else { ", " },
            size
        )
    }
}

/// Where a task's attachments live: `<artifacts>/<task_id>/attachments`.
pub fn attachments_dir(artifacts_dir: &Path, task_id: &str) -> PathBuf {
    artifacts_dir.join(task_id).join("attachments")
}

/// A file name that is safe to create: base name only, Windows-reserved characters replaced, length capped.
pub fn safe_file_name(name: &str) -> String {
    let base = name.rsplit(['\\', '/']).next().unwrap_or(name).trim();
    let mut out: String = base
        .chars()
        .map(|c| {
            if c.is_control() || matches!(c, '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|') {
                '_'
            } else {
                c
            }
        })
        .collect();
    out = out.trim_matches(|c| c == '.' || c == ' ').to_string();
    if out.is_empty() {
        out = "file".to_string();
    }
    let stem_upper = out.split('.').next().unwrap_or("").to_ascii_uppercase();
    const RESERVED: [&str; 22] = [
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
        "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    ];
    if RESERVED.contains(&stem_upper.as_str()) {
        out = format!("_{out}");
    }
    if out.chars().count() > 120 {
        let ext = Path::new(&out)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{e}"))
            .unwrap_or_default();
        let stem: String = out.chars().take(120 - ext.chars().count()).collect();
        out = format!("{stem}{ext}");
    }
    out
}

/// Save `source` as an attachment of the task (copied unless it already lies in the attachment folder) and
/// describe it. A name that is already taken gets a numeric suffix.
pub fn attach_file(artifacts_dir: &Path, task_id: &str, source: &Path) -> Result<UserAttachment> {
    if !source.is_file() {
        bail!("attachment {} is not a file", source.display());
    }
    let dir = attachments_dir(artifacts_dir, task_id);
    std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    let inside = source
        .canonicalize()
        .ok()
        .zip(dir.canonicalize().ok())
        .is_some_and(|(s, d)| s.starts_with(&d));
    let target = if inside {
        source.to_path_buf()
    } else {
        let name = safe_file_name(
            source
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("file"),
        );
        let mut target = dir.join(&name);
        let mut n = 1;
        while target.exists() {
            n += 1;
            let p = Path::new(&name);
            let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
            let ext = p
                .extension()
                .and_then(|e| e.to_str())
                .map(|e| format!(".{e}"))
                .unwrap_or_default();
            target = dir.join(format!("{stem}-{n}{ext}"));
        }
        std::fs::copy(source, &target)
            .with_context(|| format!("copying {} to {}", source.display(), target.display()))?;
        target
    };
    Ok(UserAttachment::from_path(&target))
}

/// Only these image types can be sent to the brains (the gateway's attachment allow-list).
pub const PLANNER_IMAGE_EXTENSIONS: [&str; 5] = ["png", "jpg", "jpeg", "webp", "gif"];

pub fn attachment_kind(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .unwrap_or_default();
    match ext.as_str() {
        e if PLANNER_IMAGE_EXTENSIONS.contains(&e) => "image",
        "bmp" | "tif" | "tiff" | "heic" => "image",
        "mp3" | "wav" | "m4a" | "ogg" | "flac" | "aac" | "wma" | "opus" | "mp4" | "mov" | "mkv"
        | "avi" | "webm" | "wmv" | "m4v" => "media",
        _ => "document",
    }
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
    /// Choices offered with the open question (empty: free text only) and whether several may be picked.
    #[serde(default)]
    pub pending_options: Vec<String>,
    #[serde(default)]
    pub pending_multi: bool,
    pub runner_pid: Option<u32>,
    /// What the user and the agent said outside tool calls: the earlier request this one follows up on,
    /// questions the agent asked and the user's answers. Shown to the planner on every turn.
    #[serde(default)]
    pub conversation: Vec<String>,
    /// Files the user attached to the request. Images are shown to the planner; documents are read with tools.
    #[serde(default)]
    pub attachments: Vec<UserAttachment>,
    /// How a COMPLETED task was established: "verified" (the completion checks / cited evidence passed),
    /// "unverified" (accepted after repeated rejections; `unverified_checks` says what could not be confirmed),
    /// "direct_answer" (no tools were used: there was nothing to verify). None while not completed, and for
    /// records written before this field existed. Machine-readable: the summary text is not the source of truth.
    #[serde(default)]
    pub completion_basis: Option<String>,
    #[serde(default)]
    pub unverified_checks: Vec<String>,
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
            pending_options: Vec::new(),
            pending_multi: false,
            runner_pid: None,
            conversation: Vec::new(),
            attachments: Vec::new(),
            completion_basis: None,
            unverified_checks: Vec::new(),
        }
    }

    pub fn transition(&mut self, to: TaskStatus) -> Result<()> {
        if !self.status.can_transition(to) {
            bail!(
                "invalid task transition {} -> {}",
                self.status.as_str(),
                to.as_str()
            );
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
    format!(
        "{}-{}",
        Utc::now().format("%Y%m%d-%H%M%S"),
        &uuid::Uuid::new_v4().simple().to_string()[..6]
    )
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
            let mut file = std::fs::File::create(&tmp)
                .with_context(|| format!("creating {}", tmp.display()))?;
            file.write_all(&data)?;
            file.sync_all()?;
        }
        if path.is_file() && serde_json::from_slice::<TaskRecord>(&std::fs::read(&path)?).is_ok() {
            std::fs::copy(&path, &backup)
                .with_context(|| format!("backing up {}", path.display()))?;
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
        let Ok(entries) = std::fs::read_dir(&self.dir) else {
            return Ok(records);
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_some_and(|e| e == "json") {
                let id = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .ok_or_else(|| anyhow!("bad file name"))?;
                if let Ok((record, _)) = self.load(id) {
                    records.push(record);
                }
            }
        }
        records.sort_by_key(|record| std::cmp::Reverse(record.updated_at));
        Ok(records)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completion_basis_is_machine_readable_and_old_records_still_load() {
        let mut r = TaskRecord::new("t".into(), "g".into(), ExecutionMode::Normal);
        assert_eq!(r.completion_basis, None);
        r.completion_basis = Some("unverified".into());
        r.unverified_checks = vec!["check 0 (filesystem.stat): exists missing".into()];
        let json = serde_json::to_value(&r).unwrap();
        assert_eq!(json["completion_basis"], "unverified");
        // A record written before the field existed (no such keys) deserialises to None / empty.
        let mut old = json.clone();
        old.as_object_mut().unwrap().remove("completion_basis");
        old.as_object_mut().unwrap().remove("unverified_checks");
        let loaded: TaskRecord = serde_json::from_value(old).unwrap();
        assert_eq!(loaded.completion_basis, None);
        assert!(loaded.unverified_checks.is_empty());
    }

    #[test]
    fn transitions_follow_the_loop() {
        let mut r = TaskRecord::new("t".into(), "g".into(), ExecutionMode::Normal);
        r.transition(TaskStatus::Planning).unwrap();
        r.transition(TaskStatus::Executing).unwrap();
        r.transition(TaskStatus::Observing).unwrap();
        r.transition(TaskStatus::Planning).unwrap();
        assert!(
            r.transition(TaskStatus::Completed).is_err(),
            "completion must go through verification"
        );
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
