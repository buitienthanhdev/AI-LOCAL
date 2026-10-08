//! Tools the core executes itself, in Rust, without the Python worker: `shell.exec`, `filesystem.*` and
//! `system.environment` (contract: tools/system.json). They never touch UI Automation.
//!
//! `WorkerClient::call` routes these here; callers see the same `ToolResponse` envelope, error codes and
//! verification semantics as for worker tools.

pub mod env;
pub mod fs;
pub mod shell;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::config::Config;
use crate::error::{ErrorCode, ToolErrorInfo};
use crate::worker::{Metrics, ToolCall, ToolResponse, ToolResult, VerificationInfo};

pub const TOOLS: [&str; 6] = ["shell.exec", "filesystem.read", "filesystem.write", "filesystem.list", "filesystem.stat", "system.environment"];
const STAT_HASH_MAX_BYTES: u64 = 256 * 1024 * 1024;
const OUTPUT_TAIL_CHARS: usize = 4000;

/// Whether a fresh attempt of the same call can reasonably succeed without changing anything
/// (same table as the worker's `DEFAULT_RETRYABLE`).
pub fn default_retryable(code: ErrorCode) -> bool {
    matches!(
        code,
        ErrorCode::WindowNotFound
            | ErrorCode::ElementNotFound
            | ErrorCode::StaleElement
            | ErrorCode::Timeout
            | ErrorCode::BackendUnavailable
            | ErrorCode::Busy
            | ErrorCode::VerificationFailed
    )
}

#[derive(Debug, Clone, Deserialize)]
struct ShellCfg {
    executable: String,
    default_timeout_ms: u64,
    max_timeout_ms: u64,
    max_output_bytes: usize,
}

#[derive(Debug, Clone, Deserialize)]
struct FilesystemCfg {
    max_read_bytes: usize,
    max_write_bytes: usize,
    max_list_entries: usize,
}

struct Outcome {
    data: Value,
    checks: Vec<(&'static str, bool, String)>,
    artifacts: Vec<Value>,
    observation: String,
    backend: &'static str,
}

pub struct NativeTools {
    shell: ShellCfg,
    filesystem: FilesystemCfg,
    chrome_path: String,
    prog_id: String,
    in_flight: Mutex<HashMap<String, Arc<Notify>>>,
}

fn tail(text: &str) -> &str {
    let start = text.char_indices().rev().nth(OUTPUT_TAIL_CHARS - 1).map_or(0, |(i, _)| i);
    &text[start..]
}

fn str_arg<'a>(args: &'a Value, key: &str) -> Result<&'a str, ToolErrorInfo> {
    args.get(key).and_then(Value::as_str).ok_or_else(|| fs::tool_error(ErrorCode::InvalidArgument, format!("{key} is required")))
}

impl NativeTools {
    pub fn new(config: &Config) -> anyhow::Result<Self> {
        Ok(Self {
            shell: config.section("shell")?,
            filesystem: config.section("filesystem")?,
            chrome_path: config.browser.chrome_path.clone(),
            prog_id: config.autocad.prog_id.clone(),
            in_flight: Mutex::new(HashMap::new()),
        })
    }

    pub fn handles(tool: &str) -> bool {
        TOOLS.contains(&tool)
    }

    /// Stop an in-flight call (shell process trees are killed). Returns false when `request_id` is not native.
    pub fn cancel(&self, request_id: &str) -> bool {
        match self.in_flight.lock().expect("in-flight lock").get(request_id) {
            Some(notify) => {
                notify.notify_one();
                true
            }
            None => false,
        }
    }

    pub async fn call(&self, call: &ToolCall) -> ToolResponse {
        let started_at = SystemTime::now();
        let started = Instant::now();
        let cancel = Arc::new(Notify::new());
        self.in_flight.lock().expect("in-flight lock").insert(call.request_id.clone(), Arc::clone(&cancel));
        let result = self.dispatch(call, cancel).await;
        self.in_flight.lock().expect("in-flight lock").remove(&call.request_id);
        let mut metrics = Metrics {
            started_unix_ms: started_at.duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0),
            duration_ms: started.elapsed().as_millis() as u64,
            backend: String::new(),
        };
        let verification_mode = match call.tool.as_str() {
            "shell.exec" => "observe",
            "filesystem.write" => "self",
            _ => "none",
        };
        match result {
            Ok(outcome) => {
                metrics.backend = outcome.backend.to_string();
                let all_passed = outcome.checks.iter().all(|c| c.1);
                let status = match verification_mode {
                    "none" => "not_applicable",
                    "observe" if outcome.checks.is_empty() => "deferred",
                    _ if all_passed => "passed",
                    _ => "failed",
                };
                let checks: Vec<Value> = outcome.checks.iter().map(|(name, passed, detail)| json!({"name": name, "passed": passed, "detail": detail})).collect();
                if verification_mode == "self" && status != "passed" {
                    let failed: Vec<String> = outcome.checks.iter().filter(|c| !c.1).map(|c| format!("{}: {}", c.0, c.2)).collect();
                    let error = fs::tool_error(ErrorCode::VerificationFailed, format!("{} post-condition failed: {}", call.tool, failed.join("; ")))
                        .with_details(json!({"checks": checks, "data": outcome.data}));
                    return ToolResponse { request_id: call.request_id.clone(), status: "error".into(), result: None, error: Some(error), metrics };
                }
                ToolResponse {
                    request_id: call.request_id.clone(),
                    status: "ok".into(),
                    result: Some(ToolResult {
                        data: outcome.data,
                        verification: VerificationInfo { status: status.into(), checks },
                        artifacts: outcome.artifacts,
                        observation: outcome.observation,
                    }),
                    error: None,
                    metrics,
                }
            }
            Err(error) => ToolResponse { request_id: call.request_id.clone(), status: "error".into(), result: None, error: Some(error), metrics },
        }
    }

    async fn dispatch(&self, call: &ToolCall, cancel: Arc<Notify>) -> Result<Outcome, ToolErrorInfo> {
        let args = &call.args;
        match call.tool.as_str() {
            "shell.exec" => self.shell_exec(call, cancel).await,
            "filesystem.read" => {
                let path = fs::absolute_path(str_arg(args, "path")?)?;
                let cap = self.filesystem.max_read_bytes;
                let limit = args.get("max_bytes").and_then(Value::as_u64).map_or(cap, |n| (n as usize).min(cap));
                let encoding = args.get("encoding").and_then(Value::as_str).map(String::from);
                let data = blocking(move || fs::read_file(&path, limit, encoding.as_deref())).await?;
                let observation = format!(
                    "{} ({} bytes, {}{})",
                    data["path"].as_str().unwrap_or(""),
                    data["size"],
                    data["encoding"].as_str().unwrap_or(""),
                    if data["truncated"] == true { ", truncated" } else { "" }
                );
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "filesystem" })
            }
            "filesystem.write" => {
                let path = fs::absolute_path(str_arg(args, "path")?)?;
                let content = str_arg(args, "content")?.to_string();
                let encoding = args.get("encoding").and_then(Value::as_str).unwrap_or("utf-8").to_string();
                let overwrite = args.get("overwrite").and_then(Value::as_bool).unwrap_or(false);
                let create_dirs = args.get("create_dirs").and_then(Value::as_bool).unwrap_or(true);
                let max = self.filesystem.max_write_bytes;
                let data = blocking(move || fs::write_file(&path, &content, &encoding, overwrite, create_dirs, max)).await?;
                let (path, size, sha) = (data["path"].as_str().unwrap_or("").to_string(), data["size"].clone(), data["sha256"].as_str().unwrap_or("").to_string());
                Ok(Outcome {
                    checks: vec![("read_back_matches", true, format!("{size} bytes sha256={}", &sha[..12.min(sha.len())]))],
                    artifacts: vec![json!({"kind": "file", "path": path})],
                    observation: format!("Wrote {size} bytes to {path} ({}).", if data["created"] == true { "created" } else { "replaced" }),
                    data,
                    backend: "filesystem",
                })
            }
            "filesystem.list" => {
                let path = fs::absolute_path(str_arg(args, "path")?)?;
                let cap = self.filesystem.max_list_entries;
                let limit = args.get("max_entries").and_then(Value::as_u64).map_or(cap, |n| (n as usize).min(cap));
                let shown = path.clone();
                let (entries, truncated) = blocking(move || fs::list_directory(&path, limit)).await?;
                let lines: Vec<String> = entries
                    .iter()
                    .map(|e| {
                        let name = e["name"].as_str().unwrap_or("");
                        if e["is_dir"] == true { format!("[dir] {name}") } else { format!("{name} ({} B)", e["size"]) }
                    })
                    .collect();
                let listing = if lines.is_empty() { "(empty)".to_string() } else { lines.join("\n") };
                Ok(Outcome {
                    data: json!({"entries": entries, "truncated": truncated}),
                    checks: vec![],
                    artifacts: vec![],
                    observation: format!("{}:\n{listing}", shown.display()),
                    backend: "filesystem",
                })
            }
            "filesystem.stat" => {
                let path = fs::absolute_path(str_arg(args, "path")?)?;
                let data = blocking(move || fs::stat(&path, STAT_HASH_MAX_BYTES)).await?;
                let shown = data["path"].as_str().unwrap_or("").to_string();
                let observation = if data["exists"] == true {
                    if data["is_dir"] == true { format!("{shown} exists (directory)") } else { format!("{shown} exists ({} bytes)", data["size"]) }
                } else {
                    format!("{shown} does not exist")
                };
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "filesystem" })
            }
            "system.environment" => {
                let (chrome, prog_id) = (self.chrome_path.clone(), self.prog_id.clone());
                let shell = shell::resolve_powershell(&self.shell.executable);
                let (data, observation) = blocking(move || Ok(env::environment(&chrome, &prog_id, shell.as_deref()))).await?;
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "registry" })
            }
            other => Err(fs::tool_error(ErrorCode::UnknownTool, format!("unknown or unavailable tool {other:?}"))),
        }
    }

    async fn shell_exec(&self, call: &ToolCall, cancel: Arc<Notify>) -> Result<Outcome, ToolErrorInfo> {
        let args = &call.args;
        let command = str_arg(args, "command")?.to_string();
        let cwd: Option<PathBuf> = match args.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty()) {
            Some(raw) => {
                let dir = fs::absolute_path(raw)?;
                if !dir.is_dir() {
                    return Err(fs::tool_error(ErrorCode::NotFound, format!("working directory {} does not exist", dir.display())));
                }
                Some(dir)
            }
            None => None,
        };
        let wanted = args.get("timeout_ms").and_then(Value::as_u64).unwrap_or(self.shell.default_timeout_ms);
        let timeout = std::time::Duration::from_millis(wanted.min(self.shell.max_timeout_ms)).min(call.timeout);
        let executable = shell::resolve_powershell(&self.shell.executable)
            .ok_or_else(|| fs::tool_error(ErrorCode::BackendUnavailable, "neither pwsh nor powershell.exe is on PATH"))?;
        let result = shell::run_powershell(&command, &executable, cwd.as_deref(), timeout, self.shell.max_output_bytes, cancel)
            .await
            .map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, format!("starting PowerShell: {e}")))?;
        if result.cancelled {
            return Err(fs::tool_error(ErrorCode::Cancelled, "cancelled by the core"));
        }
        let data = json!({"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr,
                          "timed_out": result.timed_out, "truncated": result.truncated, "duration_ms": result.duration_ms});
        if result.timed_out {
            return Err(fs::tool_error(ErrorCode::Timeout, format!("command exceeded {}s and was terminated with its children", timeout.as_secs()))
                .with_details(json!({"stdout_tail": tail(&result.stdout), "stderr_tail": tail(&result.stderr)})));
        }
        if result.looks_like_elevation_failure() {
            return Err(fs::tool_error(ErrorCode::ElevationRequired, "the command needs Administrator rights; the agent never elevates by itself")
                .with_details(json!({"escalation_request": {
                    "kind": "uac_elevation", "tool": "shell.exec", "command": command, "reason": tail(&result.stderr),
                    "how_to_grant": "run the command yourself in an elevated PowerShell, or start the agent elevated"}})));
        }
        let (stdout_tail, stderr_tail) = (tail(&result.stdout), tail(&result.stderr));
        let mut observation = format!("exit={} in {}ms\nstdout:\n{stdout_tail}", result.exit_code, result.duration_ms);
        if !stderr_tail.trim().is_empty() {
            observation.push_str(&format!("\nstderr:\n{stderr_tail}"));
        }
        Ok(Outcome {
            checks: vec![("exit_code_zero", result.exit_code == 0, format!("exit code {}", result.exit_code))],
            data,
            artifacts: vec![],
            observation,
            backend: "powershell",
        })
    }
}

/// Run blocking filesystem/registry work off the async threads.
async fn blocking<T: Send + 'static>(work: impl FnOnce() -> Result<T, ToolErrorInfo> + Send + 'static) -> Result<T, ToolErrorInfo> {
    tokio::task::spawn_blocking(work)
        .await
        .unwrap_or_else(|e| Err(fs::tool_error(ErrorCode::Internal, format!("native tool panicked: {e}"))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn tools() -> NativeTools {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        NativeTools::new(&Config::load_from(&root).unwrap()).unwrap()
    }

    fn call(tool: &str, args: Value) -> ToolCall {
        ToolCall::new("t", "s", tool, args, Duration::from_secs(60))
    }

    #[tokio::test]
    async fn shell_exec_envelope_and_failed_verification() {
        let native = tools();
        let ok = native.call(&call("shell.exec", json!({"command": "Write-Output hi"}))).await;
        let result = ok.result.unwrap();
        assert_eq!(result.data["stdout"].as_str().unwrap().trim(), "hi");
        assert_eq!(result.verification.status, "passed");
        assert_eq!(ok.metrics.backend, "powershell");
        let failed = native.call(&call("shell.exec", json!({"command": "exit 2"}))).await;
        assert!(failed.is_ok(), "a non-zero exit is an observation, not a tool error");
        assert_eq!(failed.result.unwrap().verification.status, "failed");
        let timeout = native.call(&call("shell.exec", json!({"command": "Start-Sleep 30", "timeout_ms": 1500}))).await;
        assert_eq!(timeout.error.unwrap().code, ErrorCode::Timeout);
    }

    #[tokio::test]
    async fn cancellation_stops_the_command() {
        let native = Arc::new(tools());
        let c = call("shell.exec", json!({"command": "Start-Sleep 30"}));
        let request_id = c.request_id.clone();
        let runner = { let native = Arc::clone(&native); tokio::spawn(async move { native.call(&c).await }) };
        tokio::time::sleep(Duration::from_millis(1500)).await;
        assert!(native.cancel(&request_id));
        let response = runner.await.unwrap();
        assert_eq!(response.error.unwrap().code, ErrorCode::Cancelled);
    }

    #[tokio::test]
    async fn filesystem_tools_round_trip() {
        let native = tools();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("note.txt").display().to_string();
        let written = native.call(&call("filesystem.write", json!({"path": path, "content": "xin chào"}))).await;
        let result = written.result.unwrap();
        assert_eq!(result.verification.status, "passed");
        assert_eq!(result.artifacts[0]["path"], path.as_str());
        let read = native.call(&call("filesystem.read", json!({"path": path}))).await.result.unwrap();
        assert_eq!(read.data["content"], "xin chào");
        assert_eq!(read.verification.status, "not_applicable");
        let again = native.call(&call("filesystem.write", json!({"path": path, "content": "x"}))).await;
        assert_eq!(again.error.unwrap().code, ErrorCode::Conflict);
        let listed = native.call(&call("filesystem.list", json!({"path": dir.path().display().to_string()}))).await.result.unwrap();
        assert!(listed.observation.contains("note.txt"));
        let stat = native.call(&call("filesystem.stat", json!({"path": path}))).await.result.unwrap();
        assert_eq!(stat.data["exists"], true);
        let env = native.call(&call("system.environment", json!({}))).await.result.unwrap();
        assert!(env.data["powershell"]["path"].is_string());
    }
}
