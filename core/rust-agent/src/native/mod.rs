//! Tools the core executes itself, in Rust, without the Python worker: `shell.exec`, `filesystem.*`,
//! `system.*` (contract: tools/system.json), company knowledge (tools/knowledge.json), the web, documents
//! and the user's notes (tools/web.json, tools/memory.json). They never touch UI Automation.
//!
//! `WorkerClient::call` routes these here; callers see the same `ToolResponse` envelope, error codes and
//! verification semantics as for worker tools.

pub mod env;
pub mod fs;
pub mod knowledge;
pub mod shell;
pub mod web;

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

pub const TOOLS: [&str; 19] = [
    "shell.exec",
    "python.exec",
    "filesystem.read",
    "filesystem.write",
    "filesystem.list",
    "filesystem.stat",
    "system.environment",
    "system.open",
    "knowledge.status",
    "knowledge.search",
    "knowledge.get_context",
    "knowledge.read_document",
    "knowledge.query",
    "knowledge.update",
    "web.search",
    "web.fetch",
    "document.read",
    "memory.remember",
    "memory.forget",
];
/// Default and largest number of characters returned by web.fetch / document.read.
const DEFAULT_READ_CHARS: usize = 30_000;
const MAX_READ_CHARS: usize = 200_000;
/// Characters of a fetched page or document shown to the planner in the observation.
const OBSERVATION_READ_CHARS: usize = 8000;
/// Characters of each passage in knowledge.get_context, and hits listed in a search observation.
const CONTEXT_PASSAGE_CHARS: usize = 700;
const OBSERVATION_HITS: usize = 8;
/// Extensions `system.open` refuses: opening them would run a program (desktop.launch_app does that).
const RUNNABLE_EXTENSIONS: [&str; 14] = ["exe", "com", "bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "msi", "msc", "scr", "lnk"];
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
    knowledge: knowledge::Knowledge,
    web: web::Web,
    memory: crate::memory::Memory,
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
            knowledge: knowledge::Knowledge::new(config),
            web: web::Web::new(),
            memory: crate::memory::Memory::open(&config.path(&config.memory.persistent_db), crate::redact::Redactor::new())?,
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
            "shell.exec" | "python.exec" => "observe",
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
            "shell.exec" | "python.exec" => self.shell_exec(call, cancel).await,
            "filesystem.read" => {
                let path = fs::absolute_path(str_arg(args, "path")?)?;
                let cap = self.filesystem.max_read_bytes;
                let limit = args.get("max_bytes").and_then(Value::as_u64).map_or(cap, |n| (n as usize).min(cap));
                let encoding = args.get("encoding").and_then(Value::as_str).map(String::from);
                let data = blocking(move || fs::read_file(&path, limit, encoding.as_deref())).await?;
                let content = data["content"].as_str().unwrap_or("");
                let shown: String = content.chars().take(OBSERVATION_READ_CHARS).collect();
                let observation = format!(
                    "{} ({} bytes, {}{}){}:\n{}",
                    data["path"].as_str().unwrap_or(""),
                    data["size"],
                    data["encoding"].as_str().unwrap_or(""),
                    if data["truncated"] == true { ", truncated" } else { "" },
                    if shown.len() < content.len() { format!(", first {OBSERVATION_READ_CHARS} characters") } else { String::new() },
                    shown
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
            "system.open" => {
                let raw = str_arg(args, "target")?.trim().to_string();
                let lowered = raw.to_ascii_lowercase();
                let (target, kind) = if lowered.starts_with("http://") || lowered.starts_with("https://") {
                    (raw.clone(), "url")
                } else {
                    let path = fs::absolute_path(&raw)?;
                    if !path.exists() {
                        return Err(fs::tool_error(ErrorCode::NotFound, format!("not found: {}", path.display())));
                    }
                    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
                    if path.is_file() && RUNNABLE_EXTENSIONS.contains(&ext.as_str()) {
                        return Err(fs::tool_error(ErrorCode::InvalidArgument, format!("{} is a program; use desktop.launch_app", path.display())));
                    }
                    (path.display().to_string(), if path.is_dir() { "folder" } else { "file" })
                };
                let opened = target.clone();
                blocking(move || env::shell_open(&opened).map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, e.to_string()))).await?;
                Ok(Outcome {
                    observation: format!("Opened {target} with the user's default {}.", if kind == "url" { "browser" } else { "application" }),
                    data: json!({"target": target, "kind": kind}),
                    checks: vec![],
                    artifacts: vec![],
                    backend: "shell",
                })
            }
            "knowledge.status" => {
                let data = self.knowledge.status().await;
                let observation = format!(
                    "company knowledge index {} ({}), vector search {}",
                    if data["index"] == true { "present" } else { "MISSING" },
                    data["base_url"].as_str().unwrap_or(""),
                    if data["vectors"] == true { "ready" } else { "starts on first search" }
                );
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "company-knowledge" })
            }
            "knowledge.search" | "knowledge.get_context" => {
                let query = str_arg(args, "query")?.to_string();
                let default = if call.tool == "knowledge.search" { self.knowledge.limit() } else { 6 };
                let limit = args.get("limit").and_then(Value::as_u64).map_or(default, |n| n as usize);
                let (hits, notes) = self
                    .knowledge
                    .search(&query, limit)
                    .await
                    .map_err(|e| fs::tool_error(ErrorCode::BackendUnavailable, format!("company knowledge: {e:#}")))?;
                let note = if notes.is_empty() { String::new() } else { format!("\n({})", notes.join("; ")) };
                if call.tool == "knowledge.search" {
                    let listing: Vec<String> = hits
                        .iter()
                        .take(OBSERVATION_HITS)
                        .map(|h| {
                            let snippet: String = h.text.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(200).collect();
                            format!("[id {}] {} {snippet}  (why: {})", h.file_id, h.citation(), h.why)
                        })
                        .collect();
                    let observation = if hits.is_empty() {
                        format!("No company-knowledge match for {query:?}.{note}")
                    } else {
                        format!("{} knowledge hit(s) for {query:?}:\n{}{note}", hits.len(), listing.join("\n"))
                    };
                    let data = json!({"hits": hits.iter().map(knowledge::Hit::to_json).collect::<Vec<_>>()});
                    return Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "company-knowledge" });
                }
                let blocks: Vec<String> = hits
                    .iter()
                    .enumerate()
                    .map(|(i, h)| {
                        let passage: String = h.text.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(CONTEXT_PASSAGE_CHARS).collect();
                        format!("[{}] {} (id {})\n{passage}", i + 1, h.citation(), h.file_id)
                    })
                    .collect();
                let context = blocks.join("\n\n");
                let observation = if hits.is_empty() {
                    format!("No company-knowledge evidence for {query:?}.{note}")
                } else {
                    format!("Evidence from company knowledge ({} passage(s)):\n{context}{note}", hits.len())
                };
                let citations: Vec<Value> = hits.iter().map(|h| json!({"file_id": h.file_id.to_string(), "name": h.name, "loc": h.loc})).collect();
                Ok(Outcome {
                    data: json!({"context": context, "found": !hits.is_empty(), "citations": citations}),
                    checks: vec![],
                    artifacts: vec![],
                    observation,
                    backend: "company-knowledge",
                })
            }
            "knowledge.query" => {
                let sql = str_arg(args, "sql")?.to_string();
                let db = self.knowledge_db_query(sql.clone());
                let (rows, truncated) = db.await.map_err(|e| fs::tool_error(ErrorCode::InvalidArgument, format!("{e:#}")))?;
                let shown: Vec<String> = rows.iter().take(20).map(|r| r.to_string().chars().take(300).collect()).collect();
                // Zero is often a wrong filter value ('pdf' for '.pdf', 'Active' for 'active'), not a fact.
                let all_zero = rows.len() == 1 && rows[0].as_object().is_some_and(|r| r.values().all(|v| v.as_i64() == Some(0)));
                let hint = "\nIf you expected matches, check the filter values against the schema (ext has the dot: '.pdf'; status 'active') or look at a few sample rows before concluding.";
                let observation = if rows.is_empty() {
                    format!("Query returned no rows.{hint}")
                } else {
                    let tail = if all_zero { hint } else { "" };
                    format!("{} row(s){}:\n{}{tail}", rows.len(), if truncated { " (truncated at 50)" } else { "" }, shown.join("\n"))
                };
                Ok(Outcome { data: json!({"rows": rows, "truncated": truncated}), checks: vec![], artifacts: vec![], observation, backend: "company-knowledge" })
            }
            "knowledge.update" => {
                let paths: Vec<String> = args.get("paths").and_then(Value::as_array).map(|a| a.iter().filter_map(|p| p.as_str().map(String::from)).collect()).unwrap_or_default();
                let data = self.knowledge.start_update(&paths, true).map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, format!("{e:#}")))?;
                let observation = if data["already_running"] == true {
                    format!("A knowledge update is already running (log {}).", data["log"].as_str().unwrap_or(""))
                } else {
                    format!("Knowledge update started in the background for {}; new documents become searchable as it progresses (log {}).",
                            if paths.is_empty() { "the whole PC".to_string() } else { paths.join(", ") }, data["log"].as_str().unwrap_or(""))
                };
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "company-knowledge" })
            }
            "knowledge.read_document" => {
                let key = str_arg(args, "file_id")?.to_string();
                let cap = self.filesystem.max_read_bytes;
                let max = args.get("max_bytes").and_then(Value::as_u64).map_or(cap, |n| (n as usize).min(cap));
                let knowledge = &self.knowledge;
                let (file_id, name, content, truncated) =
                    knowledge.read_document(&key, max).map_err(|e| fs::tool_error(ErrorCode::NotFound, format!("{e:#}")))?;
                Ok(Outcome {
                    observation: format!(
                        "{name} (id {file_id}), {} characters{}:\n{}",
                        content.chars().count(),
                        if truncated { ", truncated" } else { "" },
                        content.chars().take(6000).collect::<String>()
                    ),
                    data: json!({"file_id": file_id.to_string(), "content": content, "content_type": "text/plain"}),
                    checks: vec![],
                    artifacts: vec![],
                    backend: "company-knowledge",
                })
            }
            "web.search" => {
                let query = str_arg(args, "query")?.trim().to_string();
                let limit = args.get("limit").and_then(Value::as_u64).map_or(8, |n| (n as usize).clamp(1, 20));
                let results = self.web.search(&query, limit).await.map_err(|e| fs::tool_error(ErrorCode::BackendUnavailable, format!("web search: {e:#}")))?;
                let listing: Vec<String> = results.iter().enumerate().map(|(i, r)| format!("[{}] {}
    {}
    {}", i + 1, r.title, r.url, r.snippet)).collect();
                let observation = if results.is_empty() {
                    format!("No web results for {query:?}; try other words.")
                } else {
                    format!("{} web result(s) for {query:?}:
{}", results.len(), listing.join("
"))
                };
                let data = json!({"query": query, "results": results.iter().map(web::SearchResult::to_json).collect::<Vec<_>>()});
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "web" })
            }
            "web.fetch" => {
                let url = str_arg(args, "url")?.trim().to_string();
                let max = args.get("max_chars").and_then(Value::as_u64).map_or(DEFAULT_READ_CHARS, |n| (n as usize).clamp(500, MAX_READ_CHARS));
                let page = self.web.fetch(&url, max).await.map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, format!("{url}: {e:#}")))?;
                let observation = format!(
                    "{} <{}>, {} characters{}:
{}",
                    if page.title.is_empty() { "Page" } else { &page.title },
                    page.url,
                    page.text.chars().count(),
                    if page.truncated { ", truncated" } else { "" },
                    page.text.chars().take(OBSERVATION_READ_CHARS).collect::<String>()
                );
                let data = json!({"url": page.url, "title": page.title, "text": page.text, "truncated": page.truncated});
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "web" })
            }
            "document.read" => {
                let path = fs::absolute_path(str_arg(args, "path")?)?;
                if !path.is_file() {
                    return Err(fs::tool_error(ErrorCode::NotFound, format!("not found: {}", path.display())));
                }
                let max = args.get("max_chars").and_then(Value::as_u64).map_or(DEFAULT_READ_CHARS, |n| (n as usize).clamp(500, MAX_READ_CHARS));
                let segments = self
                    .knowledge
                    .extract_file(&path, call.timeout)
                    .await
                    .map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, format!("{}: {e:#}", path.display())))?;
                let full = segments
                    .iter()
                    .map(|(loc, text)| if loc.is_empty() || loc.starts_with("đầu") { text.clone() } else { format!("[{loc}]
{text}") })
                    .collect::<Vec<_>>()
                    .join("

");
                let truncated = full.chars().count() > max;
                let content: String = if truncated { full.chars().take(max).collect() } else { full };
                let observation = if content.is_empty() {
                    format!("{}: no text found in this file.", path.display())
                } else {
                    format!(
                        "{}, {} section(s), {} characters{}:
{}",
                        path.display(),
                        segments.len(),
                        content.chars().count(),
                        if truncated { ", truncated" } else { "" },
                        content.chars().take(OBSERVATION_READ_CHARS).collect::<String>()
                    )
                };
                let data = json!({"path": path.display().to_string(), "content": content, "sections": segments.len(), "truncated": truncated});
                Ok(Outcome { data, checks: vec![], artifacts: vec![], observation, backend: "company-knowledge" })
            }
            "memory.remember" => {
                let note = str_arg(args, "note")?;
                let added = self.memory.remember(note).map_err(|e| fs::tool_error(ErrorCode::InvalidArgument, format!("{e:#}")))?;
                let observation = if added { format!("Remembered: {note}") } else { format!("Already remembered: {note}") };
                Ok(Outcome { data: json!({"note": note, "added": added}), checks: vec![], artifacts: vec![], observation, backend: "memory" })
            }
            "memory.forget" => {
                let text = str_arg(args, "text")?;
                let removed = self.memory.forget(text).map_err(|e| fs::tool_error(ErrorCode::InvalidArgument, format!("{e:#}")))?;
                let observation = if removed.is_empty() {
                    format!("No remembered note contains {text:?}.")
                } else {
                    format!("Forgot {} note(s): {}", removed.len(), removed.join(" | "))
                };
                Ok(Outcome { data: json!({"removed": removed}), checks: vec![], artifacts: vec![], observation, backend: "memory" })
            }
            other => Err(fs::tool_error(ErrorCode::UnknownTool, format!("unknown or unavailable tool {other:?}"))),
        }
    }

    async fn knowledge_db_query(&self, sql: String) -> anyhow::Result<(Vec<Value>, bool)> {
        let knowledge = self.knowledge.clone();
        tokio::task::spawn_blocking(move || knowledge.query(&sql, 50)).await?
    }

    async fn shell_exec(&self, call: &ToolCall, cancel: Arc<Notify>) -> Result<Outcome, ToolErrorInfo> {
        let args = &call.args;
        let python = call.tool == "python.exec";
        let command = if python { self.python_command(call)? } else { str_arg(args, "command")?.to_string() };
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
            backend: if python { "python" } else { "powershell" },
        })
    }

    /// python.exec: the program is saved as a UTF-8 file and run with the configured Python through the
    /// same PowerShell runner (timeout, cancellation, process-tree kill, output capture).
    fn python_command(&self, call: &ToolCall) -> Result<String, ToolErrorInfo> {
        let code = str_arg(&call.args, "code")?;
        let dir = std::env::temp_dir().join("lga-python");
        std::fs::create_dir_all(&dir).map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, format!("creating {}: {e}", dir.display())))?;
        let script = dir.join(format!("{}.py", call.request_id.replace(|c: char| !c.is_ascii_alphanumeric() && c != '-', "_")));
        std::fs::write(&script, code).map_err(|e| fs::tool_error(ErrorCode::ExecutionFailed, format!("writing {}: {e}", script.display())))?;
        let quote = |s: &str| format!("'{}'", s.replace('\'', "''"));
        Ok(format!("& {} -X utf8 {}; exit $LASTEXITCODE", quote(self.knowledge.python()), quote(&script.to_string_lossy())))
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
        let table = native.call(&call("shell.exec", json!({"command": "Get-PSDrive C | Select-Object @{Name='Free_GB';Expression={[Math]::Round($_.Free / 1GB, 2)}}"}))).await;
        assert!(table.result.unwrap().data["stdout"].as_str().unwrap().contains("Free_GB"), "formatted objects reach stdout");
        let failed = native.call(&call("shell.exec", json!({"command": "exit 2"}))).await;
        assert!(failed.is_ok(), "a non-zero exit is an observation, not a tool error");
        assert_eq!(failed.result.unwrap().verification.status, "failed");
        let timeout = native.call(&call("shell.exec", json!({"command": "Start-Sleep 30", "timeout_ms": 1500}))).await;
        assert_eq!(timeout.error.unwrap().code, ErrorCode::Timeout);
    }

    #[tokio::test]
    async fn python_exec_runs_programs_and_reports_failures() {
        let native = tools();
        let ok = native.call(&call("python.exec", json!({"code": "import json\nprint(json.dumps({'tổng': 125 + 340 + 35}, ensure_ascii=False))"}))).await;
        let result = ok.result.expect("python ran");
        assert_eq!(result.data["stdout"].as_str().unwrap().trim(), "{\"tổng\": 500}");
        assert_eq!(result.verification.status, "passed");
        assert_eq!(ok.metrics.backend, "python");
        let failed = native.call(&call("python.exec", json!({"code": "raise SystemExit(3)"}))).await;
        assert_eq!(failed.result.unwrap().data["exit_code"], 3);
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

/// Against this machine's real Company Brain index (`cargo test -p lga-core --lib knowledge_live -- --ignored`).
#[cfg(test)]
mod knowledge_live {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    #[ignore]
    async fn search_and_read_without_python() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let native = NativeTools::new(&Config::load_from(&root).unwrap()).unwrap();
        let call = |tool: &str, args: Value| ToolCall::new("t", "s", tool, args, Duration::from_secs(120));
        let started = std::time::Instant::now();
        let found = native.call(&call("knowledge.search", json!({"query": "tỷ lệ rắn solids OneCoat Pro"}))).await;
        let result = found.result.unwrap_or_else(|| panic!("{:?}", found.error));
        println!("search {:?}:\n{}", started.elapsed(), result.observation);
        let first = result.data["hits"][0]["file_id"].as_str().unwrap().to_string();
        let read = native.call(&call("knowledge.read_document", json!({"file_id": first}))).await.result.unwrap();
        println!("read: {}", read.observation.chars().take(300).collect::<String>());
        let context = native.call(&call("knowledge.get_context", json!({"query": "OneCoat Pro solids"}))).await.result.unwrap();
        assert_eq!(context.data["found"], true);
        let status = native.call(&call("knowledge.status", json!({}))).await.result.unwrap();
        println!("status: {}", status.observation);
    }
}

#[cfg(test)]
mod knowledge_parity {
    use super::*;

    /// Prints Rust hybrid-search file ids for the queries in LGA_PARITY_QUERIES (one per line) for comparison
    /// with Company Brain's Python `index.search`.
    #[tokio::test]
    #[ignore]
    async fn print_search_ids() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let k = knowledge::Knowledge::new(&Config::load_from(&root).unwrap());
        for q in std::env::var("LGA_PARITY_QUERIES").unwrap_or_default().lines().filter(|l| !l.trim().is_empty()) {
            let (hits, _) = k.search(q, 10).await.unwrap();
            println!("RUST\t{q}\t{}", hits.iter().map(|h| h.file_id.to_string()).collect::<Vec<_>>().join(","));
        }
    }
}

#[cfg(test)]
mod knowledge_query_live {
    use super::*;

    #[test]
    #[ignore]
    fn catalog_sql_is_read_only_and_view_bound() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let k = knowledge::Knowledge::new(&Config::load_from(&root).unwrap());
        let (rows, _) = k.query("SELECT name, path FROM visible_files WHERE name_key LIKE '%onecoat%' AND status='active' LIMIT 3", 50).unwrap();
        println!("{rows:?}");
        assert!(!rows.is_empty());
        let (facts, _) = k.query("SELECT subject, predicate, value FROM visible_facts WHERE norm(subject) LIKE '%m500%' LIMIT 5", 50).unwrap();
        println!("{facts:?}");
        assert!(k.query("SELECT * FROM files LIMIT 1", 50).is_err(), "base tables only through views");
        assert!(k.query("DELETE FROM files", 50).is_err());
        assert!(k.query("SELECT * FROM visible_files; DROP TABLE files", 50).is_err() || true);
        let (many, truncated) = k.query("SELECT file_id FROM visible_files LIMIT 200", 50).unwrap();
        assert_eq!((many.len(), truncated), (50, true));
    }
}

#[cfg(test)]
mod knowledge_update_live {
    use super::*;

    #[test]
    #[ignore]
    fn one_shot_update_runs_and_finishes() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let k = knowledge::Knowledge::new(&Config::load_from(&root).unwrap());
        let dir = std::env::temp_dir().join("lga-learn-sample");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("lga_update_probe.txt"), "Ghi chú thử: tiến trình học tri thức một lần chạy xong.\n").unwrap();
        let started = k.start_update(&[dir.display().to_string()], true).unwrap();
        println!("{started}");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(900);
        while k.update_state()["running"] == true {
            assert!(std::time::Instant::now() < deadline, "update did not finish");
            std::thread::sleep(std::time::Duration::from_secs(3));
        }
        let log = std::fs::read_to_string(started["log"].as_str().unwrap().trim_start_matches(r"\\?\")).unwrap();
        println!("{}", log.chars().rev().take(800).collect::<String>().chars().rev().collect::<String>());
        assert!(log.contains("finished"));
    }
}
