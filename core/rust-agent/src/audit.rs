//! Per-task audit trail: `logs/tasks/<task_id>.jsonl`, one redacted JSON event per line.
//!
//! Every event carries task_id, step_id, timestamp, provider, tool, input hash, result, duration,
//! error, retry count and verification result where applicable. The log is append-only and flushed
//! per event, so it survives crashes and can rebuild the task record (`reconstruct_task`).

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use anyhow::{anyhow, Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::redact::Redactor;
use crate::state::{StepRecord, TaskRecord, TaskStatus};

#[derive(Debug, Default, Serialize)]
pub struct AuditEvent {
    pub kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub step_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_hash: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_count: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verification_result: Option<String>,
    #[serde(skip_serializing_if = "Value::is_null")]
    pub detail: Value,
}

pub struct AuditLog {
    task_id: String,
    path: PathBuf,
    file: Mutex<std::fs::File>,
    redactor: Redactor,
}

pub fn input_hash(tool: &str, args: &Value) -> String {
    let mut hasher = Sha256::new();
    hasher.update(tool.as_bytes());
    hasher.update(b"\0");
    hasher.update(serde_json::to_vec(args).unwrap_or_default());
    hex::encode(&hasher.finalize()[..12])
}

pub fn audit_path(dir: &Path, task_id: &str) -> PathBuf {
    dir.join(format!("{task_id}.jsonl"))
}

impl AuditLog {
    pub fn open(dir: &Path, task_id: &str, redactor: Redactor) -> Result<Self> {
        std::fs::create_dir_all(dir)?;
        let path = audit_path(dir, task_id);
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .with_context(|| format!("opening audit log {}", path.display()))?;
        Ok(Self { task_id: task_id.into(), path, file: Mutex::new(file), redactor })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn record(&self, event: AuditEvent) -> Result<()> {
        let mut value = serde_json::to_value(&event)?;
        if let Value::Object(map) = &mut value {
            map.insert("ts".into(), json!(Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)));
            map.insert("task_id".into(), json!(self.task_id));
        }
        let line = serde_json::to_string(&self.redactor.redact_value(&value))?;
        let mut file = self.file.lock().map_err(|_| anyhow!("audit log lock poisoned"))?;
        writeln!(file, "{line}")?;
        file.flush()?;
        Ok(())
    }

    pub fn task_started(&self, record: &TaskRecord) -> Result<()> {
        self.record(AuditEvent {
            kind: "task_started",
            detail: json!({"goal": record.goal, "mode": record.mode, "created_at": record.created_at}),
            ..Default::default()
        })
    }

    pub fn status(&self, status: TaskStatus, reason: Option<&str>) -> Result<()> {
        self.record(AuditEvent { kind: "status", detail: json!({"status": status, "reason": reason}), ..Default::default() })
    }

    pub fn step(&self, step: &StepRecord) -> Result<()> {
        self.record(AuditEvent {
            kind: "step",
            step_id: Some(step.step_id.clone()),
            provider: Some(step.provider.clone()),
            tool: Some(step.tool.clone()),
            input_hash: Some(step.input_hash.clone()),
            result: Some(json!({"status": step.status, "observation": step.observation, "backend": step.backend})),
            duration_ms: Some(step.duration_ms),
            error: step.error.as_ref().map(|e| serde_json::to_value(e).unwrap_or(Value::Null)),
            retry_count: Some(step.retry_count),
            verification_result: step.verification.clone(),
            detail: serde_json::to_value(step)?,
        })
    }
}

/// Rebuild a task record from its audit log (used when the state file and its backup are destroyed).
pub fn reconstruct_task(dir: &Path, task_id: &str) -> Result<TaskRecord> {
    let path = audit_path(dir, task_id);
    let file = std::fs::File::open(&path).with_context(|| format!("opening {}", path.display()))?;
    let mut record: Option<TaskRecord> = None;
    for line in std::io::BufReader::new(file).lines() {
        let line = line?;
        let Ok(event) = serde_json::from_str::<Value>(&line) else { continue };
        match event.get("kind").and_then(Value::as_str) {
            Some("task_started") => {
                let detail = &event["detail"];
                let mode = serde_json::from_value(detail["mode"].clone()).context("mode in task_started")?;
                let mut r = TaskRecord::new(task_id.into(), detail["goal"].as_str().unwrap_or_default().into(), mode);
                if let Ok(created) = serde_json::from_value(detail["created_at"].clone()) {
                    r.created_at = created;
                }
                record = Some(r);
            }
            Some("step") => {
                if let (Some(r), Ok(step)) = (record.as_mut(), serde_json::from_value::<StepRecord>(event["detail"].clone())) {
                    r.steps.retain(|s| s.step_id != step.step_id);
                    r.steps.push(step);
                }
            }
            Some("plan") => {
                if let (Some(r), Some(plan)) = (record.as_mut(), event["detail"]["plan"].as_array()) {
                    r.plan = plan.iter().filter_map(Value::as_str).map(String::from).collect();
                }
            }
            Some("status") => {
                if let (Some(r), Ok(status)) = (record.as_mut(), serde_json::from_value::<TaskStatus>(event["detail"]["status"].clone())) {
                    r.status = status;
                }
            }
            _ => {}
        }
    }
    let mut record = record.ok_or_else(|| anyhow!("audit log has no task_started event"))?;
    record.updated_at = Utc::now();
    Ok(record)
}
