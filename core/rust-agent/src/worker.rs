//! IPC with the Python worker: JSON lines over the child's stdio (protocol in docs/tool-contract.md).
//!
//! `shell.exec`, `filesystem.*` and `system.environment` are not sent to the worker: `call` runs them in
//! Rust (`crate::native`) and returns the same envelope.
//!
//! Each request carries `request_id`, `task_id`, `step_id`, `tool`, `args`, `deadline_unix_ms`; each
//! response `request_id`, `status`, `result`, `error`, `metrics`. A request whose response does not
//! arrive by `deadline + hung_grace` marks the worker as hung; the caller restarts it.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};

use crate::config::Config;
use crate::error::ToolErrorInfo;
use crate::native::NativeTools;
use crate::redact::Redactor;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

#[derive(Debug, Error)]
pub enum WorkerError {
    #[error("worker failed to start: {0}")]
    Start(String),
    #[error("worker process exited")]
    Died,
    #[error("worker did not answer within {0:?} after the deadline; it is considered hung")]
    Hung(Duration),
    #[error("worker I/O error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerificationInfo {
    pub status: String,
    #[serde(default)]
    pub checks: Vec<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResult {
    pub data: Value,
    pub verification: VerificationInfo,
    #[serde(default)]
    pub artifacts: Vec<Value>,
    #[serde(default)]
    pub observation: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Metrics {
    #[serde(default)]
    pub started_unix_ms: u64,
    #[serde(default)]
    pub duration_ms: u64,
    #[serde(default)]
    pub backend: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResponse {
    pub request_id: String,
    pub status: String,
    pub result: Option<ToolResult>,
    pub error: Option<ToolErrorInfo>,
    #[serde(default)]
    pub metrics: Metrics,
}

impl ToolResponse {
    pub fn is_ok(&self) -> bool {
        self.status == "ok"
    }
}

#[derive(Debug, Clone)]
pub struct ToolCall {
    pub request_id: String,
    pub task_id: String,
    pub step_id: String,
    pub tool: String,
    pub args: Value,
    pub timeout: Duration,
}

impl ToolCall {
    pub fn new(task_id: &str, step_id: &str, tool: &str, args: Value, timeout: Duration) -> Self {
        Self {
            request_id: uuid::Uuid::new_v4().simple().to_string(),
            task_id: task_id.into(),
            step_id: step_id.into(),
            tool: tool.into(),
            args,
            timeout,
        }
    }
}

type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<Value>>>>;

pub struct WorkerClient {
    stdin: Mutex<ChildStdin>,
    pending: Pending,
    alive: Arc<AtomicBool>,
    child: Mutex<Child>,
    tools: Vec<String>,
    native: Arc<NativeTools>,
    pid: u32,
    hung_grace: Duration,
    #[cfg(windows)]
    _job: crate::job::JobObject,
}

fn unix_ms(at: SystemTime) -> u64 {
    at.duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

impl WorkerClient {
    pub async fn spawn(config: &Config, redactor: Redactor) -> Result<Self, WorkerError> {
        let native = Arc::new(NativeTools::new(config).map_err(|e| WorkerError::Start(format!("native tools: {e:#}")))?);
        let python = config.path(&config.worker.python);
        if !python.is_file() {
            return Err(WorkerError::Start(format!(
                "{} not found; run `uv sync` in services/python-agent",
                python.display()
            )));
        }
        let mut command = Command::new(&python);
        command
            .args(["-X", "utf8", "-m", "lga_worker"])
            .current_dir(&config.root)
            .env("LGA_ROOT", &config.root)
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);
        let mut child = command.spawn().map_err(|e| WorkerError::Start(format!("{}: {e}", python.display())))?;
        let pid = child.id().ok_or_else(|| WorkerError::Start("worker exited immediately".into()))?;
        #[cfg(windows)]
        let job = {
            let job = crate::job::JobObject::kill_on_close().map_err(|e| WorkerError::Start(format!("job object: {e}")))?;
            job.assign(pid).map_err(|e| WorkerError::Start(format!("assigning worker to job: {e}")))?;
            job
        };
        let stdin = child.stdin.take().ok_or_else(|| WorkerError::Start("no stdin".into()))?;
        let stdout = child.stdout.take().ok_or_else(|| WorkerError::Start("no stdout".into()))?;
        let stderr = child.stderr.take().ok_or_else(|| WorkerError::Start("no stderr".into()))?;

        let stderr_log = config.logs_dir().join("worker").join("stderr.log");
        spawn_stderr_pump(stderr, stderr_log, redactor);

        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let alive = Arc::new(AtomicBool::new(true));
        let (ready_tx, ready_rx) = oneshot::channel::<Value>();
        spawn_stdout_reader(stdout, pending.clone(), alive.clone(), ready_tx);

        let startup = Duration::from_millis(config.worker.startup_timeout_ms);
        let ready = match tokio::time::timeout(startup, ready_rx).await {
            Ok(Ok(ready)) => ready,
            Ok(Err(_)) => {
                return Err(WorkerError::Start(format!(
                    "worker exited before it was ready; see {}",
                    config.logs_dir().join("worker").join("stderr.log").display()
                )))
            }
            Err(_) => return Err(WorkerError::Start(format!("worker not ready within {startup:?}"))),
        };
        let mut tools: Vec<String> = ready
            .get("tools")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).map(String::from).collect())
            .unwrap_or_default();
        tools.extend(crate::native::TOOLS.iter().map(|t| t.to_string()));
        tools.sort();
        tools.dedup();
        Ok(Self {
            stdin: Mutex::new(stdin),
            pending,
            alive,
            child: Mutex::new(child),
            tools,
            native,
            pid,
            hung_grace: Duration::from_millis(config.worker.hung_grace_ms),
            #[cfg(windows)]
            _job: job,
        })
    }

    pub fn tools(&self) -> &[String] {
        &self.tools
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    async fn send(&self, message: &Value) -> Result<(), WorkerError> {
        if !self.is_alive() {
            return Err(WorkerError::Died);
        }
        let mut line = serde_json::to_vec(message).expect("serializable message");
        line.push(b'\n');
        let mut stdin = self.stdin.lock().await;
        stdin.write_all(&line).await?;
        stdin.flush().await?;
        Ok(())
    }

    async fn register(&self, request_id: &str) -> oneshot::Receiver<Value> {
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(request_id.to_string(), tx);
        rx
    }

    pub async fn call(&self, call: &ToolCall) -> Result<ToolResponse, WorkerError> {
        if NativeTools::handles(&call.tool) {
            return Ok(self.native.call(call).await);
        }
        let deadline = SystemTime::now() + call.timeout;
        let rx = self.register(&call.request_id).await;
        let message = json!({
            "type": "request", "request_id": call.request_id, "task_id": call.task_id,
            "step_id": call.step_id, "tool": call.tool, "args": call.args,
            "deadline_unix_ms": unix_ms(deadline),
        });
        if let Err(err) = self.send(&message).await {
            self.pending.lock().await.remove(&call.request_id);
            return Err(err);
        }
        let wait = call.timeout + self.hung_grace;
        match tokio::time::timeout(wait, rx).await {
            Ok(Ok(value)) => serde_json::from_value(value)
                .map_err(|e| WorkerError::Io(std::io::Error::new(std::io::ErrorKind::InvalidData, e))),
            Ok(Err(_)) => Err(WorkerError::Died),
            Err(_) => {
                self.pending.lock().await.remove(&call.request_id);
                Err(WorkerError::Hung(self.hung_grace))
            }
        }
    }

    /// Ask the worker to cancel an in-flight request. The response (CANCELLED) still arrives via `call`.
    pub async fn cancel(&self, request_id: &str) -> Result<(), WorkerError> {
        if self.native.cancel(request_id) {
            return Ok(());
        }
        self.send(&json!({"type": "cancel", "request_id": request_id})).await
    }

    pub async fn ping(&self, timeout: Duration) -> Result<Duration, WorkerError> {
        let request_id = format!("ping-{}", uuid::Uuid::new_v4().simple());
        let rx = self.register(&request_id).await;
        let started = Instant::now();
        self.send(&json!({"type": "ping", "request_id": request_id})).await?;
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(_)) => Ok(started.elapsed()),
            Ok(Err(_)) => Err(WorkerError::Died),
            Err(_) => Err(WorkerError::Hung(timeout)),
        }
    }

    pub async fn shutdown(&self) {
        if self.send(&json!({"type": "shutdown"})).await.is_ok() {
            let mut child = self.child.lock().await;
            if tokio::time::timeout(SHUTDOWN_GRACE, child.wait()).await.is_ok() {
                return;
            }
        }
        self.kill().await;
    }

    pub async fn kill(&self) {
        self.alive.store(false, Ordering::SeqCst);
        let mut child = self.child.lock().await;
        // The process may already be gone; start_kill on an exited child is harmless.
        let _ = child.start_kill();
        let _ = tokio::time::timeout(SHUTDOWN_GRACE, child.wait()).await;
    }
}

fn spawn_stdout_reader(
    stdout: tokio::process::ChildStdout,
    pending: Pending,
    alive: Arc<AtomicBool>,
    ready_tx: oneshot::Sender<Value>,
) {
    tokio::spawn(async move {
        let mut ready_tx = Some(ready_tx);
        let mut lines = BufReader::new(stdout).lines();
        loop {
            let line = match lines.next_line().await {
                Ok(Some(line)) => line,
                Ok(None) | Err(_) => break,
            };
            let Ok(message) = serde_json::from_str::<Value>(&line) else { continue };
            match message.get("type").and_then(Value::as_str) {
                Some("ready") => {
                    if let Some(tx) = ready_tx.take() {
                        let _ = tx.send(message);
                    }
                }
                Some("response") | Some("pong") => {
                    let Some(id) = message.get("request_id").and_then(Value::as_str).map(String::from) else {
                        continue;
                    };
                    if let Some(tx) = pending.lock().await.remove(&id) {
                        let _ = tx.send(message);
                    }
                }
                _ => {}
            }
        }
        alive.store(false, Ordering::SeqCst);
        // Dropping the senders wakes every waiter with WorkerError::Died.
        pending.lock().await.clear();
    });
}

fn spawn_stderr_pump(stderr: tokio::process::ChildStderr, path: PathBuf, redactor: Redactor) {
    tokio::spawn(async move {
        if let Some(parent) = path.parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
        }
        let file = tokio::fs::OpenOptions::new().create(true).append(true).open(&path).await;
        let Ok(mut file) = file else { return };
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let entry = format!("{} {}\n", chrono::Local::now().format("%Y-%m-%dT%H:%M:%S"), redactor.redact(&line));
            if file.write_all(entry.as_bytes()).await.is_err() {
                return;
            }
        }
    });
}
