//! The page's API, served on the webview's private `lga://` scheme (no TCP port, no token).
//!
//! The desktop app owns no agent logic. It
//! * starts tasks by running `agent run --ui-channel --task-id <id> --mode <m> "<task>"` (the real CLI),
//! * controls them with `agent cancel|pause|unpause <id>` and `agent resume --ui-channel <id>`,
//! * reads progress from `memory/tasks/<id>.json` and the audit log `logs/tasks/<id>.jsonl`,
//! * answers confirmations/questions through `memory/tasks/<id>.request.json` / `.response.json`.
//!
//! Only this app's webview can reach the scheme; navigation to any other origin is refused in `main.rs`.

use std::borrow::Cow;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime};

use anyhow::{anyhow, Result};
use base64::Engine;
use http::{Method, Request, Response, StatusCode};
use lga_core::config::Config;
use lga_core::interaction::{request_path, response_path};
use lga_core::state::{attachments_dir, new_task_id, safe_file_name};
use serde_json::{json, Value};

use crate::services;

const INDEX_HTML: &str = include_str!("../ui/index.html");
const MAX_TASK_CHARS: usize = 4000;
/// Files the user can attach to one message, and the size of each (the gateway forwards images up to 12 MB;
/// documents are read by tools, so they may be larger).
const MAX_ATTACHMENTS: usize = 8;
const MAX_ATTACHMENT_BYTES: usize = 25 * 1024 * 1024;
const MAX_TASKS_LISTED: usize = 100;
const MAX_EVENTS_PER_POLL: usize = 500;
const CONTROL_TIMEOUT: Duration = Duration::from_secs(30);
const VALID_MODES: [&str; 3] = ["normal", "trusted", "full_access"];
const GEMINI_API_KEY_SECRET: &str = "LGA_GEMINI_API_KEY";
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;
pub const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

pub struct Api {
    pub config: Config,
    pub agent_exe: PathBuf,
}

type Reply = Response<Cow<'static, [u8]>>;

fn reply(status: StatusCode, content_type: &str, body: Vec<u8>) -> Reply {
    Response::builder()
        .status(status)
        .header("Content-Type", content_type)
        .header("Cache-Control", "no-store")
        .body(Cow::Owned(body))
        .expect("static response parts are valid")
}

fn json_reply(status: StatusCode, value: Value) -> Reply {
    reply(status, "application/json", value.to_string().into_bytes())
}

fn error(status: StatusCode, message: impl Into<String>) -> Reply {
    json_reply(status, json!({"error": message.into()}))
}

fn valid_task_id(id: &str) -> bool {
    (1..=80).contains(&id.len()) && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_str(&fs::read_to_string(path).ok()?).ok()
}

fn query_param(uri: &http::Uri, name: &str) -> Option<String> {
    uri.query()?.split('&').find_map(|pair| {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        (key == name).then(|| percent_decode(value))
    })
}

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok());
                match hex {
                    Some(byte) => {
                        out.push(byte);
                        i += 3;
                        continue;
                    }
                    None => out.push(b'%'),
                }
            }
            b'+' => out.push(b' '),
            other => out.push(other),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

impl Api {
    fn tasks_dir(&self) -> PathBuf {
        self.config.tasks_dir()
    }

    /// The conversation a task belongs to: `<task>.conv` holds its id; a task without one (older history) is its own
    /// conversation. Requests typed one after another in the same chat box share one id.
    fn conversation_of(&self, task_id: &str) -> String {
        fs::read_to_string(self.tasks_dir().join(format!("{task_id}.conv")))
            .ok()
            .map(|c| c.trim().to_string())
            .filter(|c| valid_task_id(c))
            .unwrap_or_else(|| task_id.to_string())
    }

    fn audit_path(&self, task_id: &str) -> PathBuf {
        self.config.logs_dir().join("tasks").join(format!("{task_id}.jsonl"))
    }

    pub fn handle(&self, request: Request<Vec<u8>>) -> Reply {
        let path = request.uri().path().trim_end_matches('/').to_string();
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        let method = request.method().clone();
        let body: Value = serde_json::from_slice(request.body()).unwrap_or(Value::Null);
        let result = match (&method, segments.as_slice()) {
            (&Method::GET, []) => return reply(StatusCode::OK, "text/html; charset=utf-8", INDEX_HTML.as_bytes().to_vec()),
            (&Method::GET, ["vendor", rest @ ..]) => {
                return match crate::vendor::asset(&rest.join("/")) {
                    Some((mime, bytes)) => Response::builder()
                        .status(StatusCode::OK)
                        .header("Content-Type", mime)
                        .header("Cache-Control", "max-age=86400")
                        .body(Cow::Borrowed(bytes))
                        .expect("static response parts are valid"),
                    None => error(StatusCode::NOT_FOUND, "unknown asset"),
                };
            }
            (&Method::GET, ["api", "status"]) => Ok(json_reply(StatusCode::OK, self.status())),
            (&Method::GET, ["api", "tasks"]) => Ok(json_reply(StatusCode::OK, self.list_tasks())),
            (&Method::POST, ["api", "tasks"]) => self.start_task(&body),
            (&Method::GET, ["api", "artifact"]) => Ok(self.artifact(request.uri())),
            (&Method::POST, ["api", "reveal"]) => self.reveal(&body),
            (&Method::POST, ["api", "conversations", id, "delete"]) if valid_task_id(id) => Ok(self.delete_history(Some(id))),
            (&Method::POST, ["api", "history", "clear"]) => Ok(self.delete_history(None)),
            (&Method::GET, ["api", "settings"]) => Ok(json_reply(StatusCode::OK, self.settings())),
            (&Method::POST, ["api", "settings"]) => self.save_settings(&body),
            (&Method::GET, ["api", "tasks", id]) if valid_task_id(id) => Ok(self.get_task(id)),
            (&Method::GET, ["api", "tasks", id, "events"]) if valid_task_id(id) => {
                let after = query_param(request.uri(), "after").and_then(|v| v.parse().ok()).unwrap_or(0);
                Ok(json_reply(StatusCode::OK, self.events(id, after)))
            }
            (&Method::POST, ["api", "tasks", id, "respond"]) if valid_task_id(id) => self.respond(id, &body),
            (&Method::POST, ["api", "tasks", id, action]) if valid_task_id(id) => self.control(id, action, &body),
            _ => return error(StatusCode::NOT_FOUND, "not found"),
        };
        result.unwrap_or_else(|e| error(StatusCode::BAD_REQUEST, e.to_string()))
    }

    fn status(&self) -> Value {
        let s = services::probe_all(&self.config);
        json!({
            "gateway": s.gateway,
            "knowledge": s.knowledge,
            "vision": s.vision,
            "default_mode": self.config.agent.mode.as_str(),
            "brains": if s.gateway { services::brains(&self.config) } else { json!([]) },
        })
    }

    fn settings(&self) -> Value {
        json!({"gemini_api_key_set": lga_core::secrets::resolve_secret(&self.config.root, GEMINI_API_KEY_SECRET).is_some()})
    }

    /// Save (or with an empty value remove) the Gemini API key in the repository's git-ignored `.env`, then
    /// restart the gateway so it uses the key.
    fn save_settings(&self, body: &Value) -> Result<Reply> {
        let Some(key) = body.get("gemini_api_key").and_then(Value::as_str) else {
            return Err(anyhow!("nothing to save"));
        };
        let key = key.trim();
        if !key.is_empty() && (key.len() < 20 || key.len() > 200 || !key.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))) {
            return Err(anyhow!("Khóa API không hợp lệ (dán đúng chuỗi khóa từ Google AI Studio)"));
        }
        let env_path = self.config.root.join(".env");
        let existing = fs::read_to_string(&env_path).unwrap_or_default();
        let mut lines: Vec<String> = existing
            .lines()
            .filter(|l| l.split_once('=').is_none_or(|(k, _)| k.trim() != GEMINI_API_KEY_SECRET))
            .map(String::from)
            .collect();
        if !key.is_empty() {
            lines.push(format!("{GEMINI_API_KEY_SECRET}={key}"));
        }
        fs::write(&env_path, lines.join("\n") + "\n")?;
        let agent = self.agent_exe.clone();
        let root = self.config.root.clone();
        std::thread::spawn(move || {
            for action in ["stop", "start"] {
                let _ = run_with_timeout(&agent, &["gateway", action], &root, Duration::from_secs(90));
                if action == "stop" {
                    std::thread::sleep(Duration::from_secs(3));
                }
            }
        });
        Ok(json_reply(StatusCode::OK, json!({"ok": true, "gemini_api_key_set": !key.is_empty()})))
    }

    fn list_tasks(&self) -> Value {
        let mut entries: Vec<(SystemTime, PathBuf)> = fs::read_dir(self.tasks_dir())
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                name.ends_with(".json") && !name.ends_with(".request.json") && !name.ends_with(".response.json")
            })
            .filter_map(|p| Some((fs::metadata(&p).ok()?.modified().ok()?, p)))
            .collect();
        entries.sort_by_key(|e| std::cmp::Reverse(e.0));
        let tasks: Vec<Value> = entries
            .iter()
            .filter_map(|(_, p)| read_json(p))
            .filter(|r| r.get("task_id").is_some())
            .take(MAX_TASKS_LISTED)
            .map(|r| {
                json!({
                    "task_id": r["task_id"], "goal": r["goal"], "status": r["status"], "mode": r["mode"],
                    "updated_at": r["updated_at"], "created_at": r["created_at"],
                    "steps": r["steps"].as_array().map_or(0, Vec::len),
                    "attachments": r["attachments"].as_array().map_or(0, Vec::len),
                    "interrupted": interrupted(&r),
                    "conversation_id": self.conversation_of(r["task_id"].as_str().unwrap_or("")),
                })
            })
            .collect();
        json!({ "tasks": tasks })
    }

    /// Delete a chat (`Some(conversation id)`) or the whole history (`None`): task records, audit logs, the
    /// artifacts folder (screenshots, attachments) and UI logs. A task whose runner is still alive is kept and
    /// reported, so nothing is pulled from under a running job.
    fn delete_history(&self, conversation: Option<&str>) -> Reply {
        let mut ids: Vec<String> = fs::read_dir(self.tasks_dir())
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| {
                let name = e.file_name().to_string_lossy().into_owned();
                let id = name.split('.').next()?.to_string();
                (valid_task_id(&id) && name != id).then_some(id)
            })
            .collect();
        ids.sort();
        ids.dedup();
        let (mut deleted, mut running) = (0, 0);
        for id in ids {
            if conversation.is_some_and(|c| self.conversation_of(&id) != c) {
                continue;
            }
            let record = read_json(&self.tasks_dir().join(format!("{id}.json")));
            let busy = record.as_ref().is_some_and(|r| {
                let status = r["status"].as_str().unwrap_or("");
                !matches!(status, "completed" | "failed" | "cancelled")
                    && r["runner_pid"].as_u64().is_some_and(|pid| process_alive(pid as u32))
            });
            if busy {
                running += 1;
                continue;
            }
            let prefix = format!("{id}.");
            for dir in [self.tasks_dir(), self.config.logs_dir().join("tasks"), self.config.logs_dir().join("ui")] {
                for entry in fs::read_dir(&dir).into_iter().flatten().flatten() {
                    if entry.file_name().to_string_lossy().starts_with(&prefix) {
                        let _ = fs::remove_file(entry.path());
                    }
                }
            }
            let _ = fs::remove_dir_all(self.config.artifacts_dir().join(&id));
            deleted += 1;
        }
        json_reply(StatusCode::OK, json!({"ok": true, "deleted": deleted, "running": running}))
    }

    fn get_task(&self, id: &str) -> Reply {
        let Some(task) = read_json(&self.tasks_dir().join(format!("{id}.json"))) else {
            return error(StatusCode::NOT_FOUND, "unknown task");
        };
        let pending = read_json(&request_path(&self.tasks_dir(), id));
        let paused = self.tasks_dir().join(format!("{id}.pause")).exists();
        // A task that is not finished but whose runner process is gone (crash, reboot, killed) will never
        // move again on its own: the UI offers to run it again instead of showing it as busy forever.
        let interrupted = interrupted(&task);
        json_reply(StatusCode::OK, json!({"task": task, "pending_request": pending, "pause_requested": paused,
                                          "interrupted": interrupted, "conversation_id": self.conversation_of(id)}))
    }

    /// Audit events after line `after`. A line still being written (invalid JSON) ends the read; it is
    /// picked up on the next poll.
    fn events(&self, id: &str, after: usize) -> Value {
        let mut events = Vec::new();
        let mut cursor = after;
        if let Ok(file) = File::open(self.audit_path(id)) {
            for (index, line) in BufReader::new(file).lines().enumerate() {
                let number = index + 1;
                if number <= after {
                    continue;
                }
                let Ok(line) = line else { break };
                let Ok(event) = serde_json::from_str::<Value>(&line) else { break };
                events.push(event);
                cursor = number;
                if events.len() >= MAX_EVENTS_PER_POLL {
                    break;
                }
            }
        }
        json!({"events": events, "next": cursor})
    }

    fn spawn_agent(&self, args: &[&str], log_name: &str) -> Result<()> {
        let log_dir = self.config.logs_dir().join("ui");
        fs::create_dir_all(&log_dir)?;
        let log = OpenOptions::new().create(true).append(true).open(log_dir.join(log_name))?;
        Command::new(&self.agent_exe)
            .args(args)
            .current_dir(&self.config.root)
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log)
            .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
            .spawn()?;
        Ok(())
    }

    /// Save the files sent with a message (`attachments: [{name, data_base64}]`) under the task's attachment
    /// folder and return their paths. Names are sanitised; a duplicate name gets a numeric suffix.
    fn save_attachments(&self, task_id: &str, body: &Value) -> Result<Vec<PathBuf>> {
        let Some(items) = body["attachments"].as_array() else { return Ok(Vec::new()) };
        if items.is_empty() {
            return Ok(Vec::new());
        }
        if items.len() > MAX_ATTACHMENTS {
            return Err(anyhow!("Tối đa {MAX_ATTACHMENTS} tệp đính kèm mỗi lần"));
        }
        let dir = attachments_dir(&self.config.artifacts_dir(), task_id);
        fs::create_dir_all(&dir)?;
        let mut saved = Vec::new();
        for item in items {
            let name = safe_file_name(item["name"].as_str().unwrap_or("file"));
            let data = item["data_base64"].as_str().unwrap_or("");
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(data.trim())
                .map_err(|_| anyhow!("Tệp {name} không đọc được (dữ liệu hỏng)"))?;
            if bytes.is_empty() {
                return Err(anyhow!("Tệp {name} rỗng"));
            }
            if bytes.len() > MAX_ATTACHMENT_BYTES {
                return Err(anyhow!("Tệp {name} lớn hơn {} MB", MAX_ATTACHMENT_BYTES / (1024 * 1024)));
            }
            let mut target = dir.join(&name);
            let mut n = 1;
            while target.exists() {
                n += 1;
                let path = Path::new(&name);
                let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
                let ext = path.extension().and_then(|e| e.to_str()).map(|e| format!(".{e}")).unwrap_or_default();
                target = dir.join(format!("{stem}-{n}{ext}"));
            }
            fs::write(&target, bytes)?;
            saved.push(target);
        }
        Ok(saved)
    }

    fn start_task(&self, body: &Value) -> Result<Reply> {
        let task = body["task"].as_str().unwrap_or("").trim();
        let mode = body["mode"].as_str().unwrap_or("normal");
        let has_files = body["attachments"].as_array().is_some_and(|a| !a.is_empty());
        if (task.is_empty() && !has_files) || task.chars().count() > MAX_TASK_CHARS {
            return Err(anyhow!("Yêu cầu phải dài 1–{MAX_TASK_CHARS} ký tự (hoặc có tệp đính kèm)"));
        }
        if !VALID_MODES.contains(&mode) {
            return Err(anyhow!("chế độ không hợp lệ"));
        }
        let task_id = new_task_id();
        // A follow-up names the conversation it continues; the first request of a chat starts one with its own id.
        let conversation = body["conversation_id"].as_str().filter(|c| valid_task_id(c)).unwrap_or(&task_id).to_string();
        fs::create_dir_all(self.tasks_dir())?;
        fs::write(self.tasks_dir().join(format!("{task_id}.conv")), &conversation)?;
        let files = self.save_attachments(&task_id, body)?;
        let mut args: Vec<String> = ["run", "--ui-channel", "--task-id", &task_id, "--mode", mode].iter().map(|s| s.to_string()).collect();
        let context = body["context"].as_str().unwrap_or("").trim();
        if !context.is_empty() {
            args.push("--context".into());
            args.push(context.into());
        }
        for file in &files {
            args.push("--attach".into());
            args.push(file.display().to_string());
        }
        // "--" so a request that starts with "-" is never read as an option.
        args.push("--".into());
        args.push(task.into());
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        self.spawn_agent(&args, &format!("{task_id}.out.log"))?;
        Ok(json_reply(StatusCode::OK, json!({"task_id": task_id, "conversation_id": conversation, "attachments": files.len()})))
    }

    fn control(&self, id: &str, action: &str, body: &Value) -> Result<Reply> {
        match action {
            "resume" => {
                let files = self.save_attachments(id, body)?;
                let mut args: Vec<String> = ["resume", "--ui-channel", id].iter().map(|s| s.to_string()).collect();
                let answer = body["answer"].as_str().unwrap_or("").trim();
                if !answer.is_empty() {
                    args.push("--answer".into());
                    args.push(answer.into());
                }
                for file in &files {
                    args.push("--attach".into());
                    args.push(file.display().to_string());
                }
                let args: Vec<&str> = args.iter().map(String::as_str).collect();
                self.spawn_agent(&args, &format!("{id}.resume.log"))?;
                Ok(json_reply(StatusCode::OK, json!({"ok": true, "output": "resuming"})))
            }
            "cancel" | "pause" | "unpause" => {
                let (code, output) = run_with_timeout(&self.agent_exe, &[action, id], &self.config.root, CONTROL_TIMEOUT)?;
                let status = if code == 0 { StatusCode::OK } else { StatusCode::CONFLICT };
                Ok(json_reply(status, json!({"ok": code == 0, "output": output})))
            }
            _ => Ok(error(StatusCode::NOT_FOUND, "unknown action")),
        }
    }

    fn respond(&self, id: &str, body: &Value) -> Result<Reply> {
        let tasks_dir = self.tasks_dir();
        let Some(pending) = read_json(&request_path(&tasks_dir, id)) else {
            return Ok(error(StatusCode::CONFLICT, "no pending request"));
        };
        let mut answer = json!({"id": pending["id"]});
        if pending["kind"] == "confirm" {
            answer["approve"] = json!(body["approve"].as_bool().unwrap_or(false));
        } else {
            answer["answer"] = json!(body["answer"].as_str().unwrap_or(""));
        }
        let target = response_path(&tasks_dir, id);
        let tmp = target.with_extension("tmp");
        fs::write(&tmp, answer.to_string())?;
        fs::rename(&tmp, &target)?;
        Ok(json_reply(StatusCode::OK, json!({"ok": true})))
    }

    /// Screenshots and other files, but only from inside the artifacts directory.
    fn artifact(&self, uri: &http::Uri) -> Reply {
        let requested = query_param(uri, "path").unwrap_or_default();
        let (Ok(root), Ok(file)) = (self.config.artifacts_dir().canonicalize(), Path::new(&requested).canonicalize()) else {
            return error(StatusCode::NOT_FOUND, "not found");
        };
        if !file.starts_with(&root) || !file.is_file() {
            return error(StatusCode::NOT_FOUND, "not found");
        }
        let content_type = match file.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase).as_deref() {
            Some("png") => "image/png",
            Some("jpg" | "jpeg") => "image/jpeg",
            Some("webp") => "image/webp",
            Some("gif") => "image/gif",
            Some("bmp") => "image/bmp",
            Some("pdf") => "application/pdf",
            Some("txt" | "log") => "text/plain; charset=utf-8",
            _ => "application/octet-stream",
        };
        match fs::read(&file) {
            Ok(bytes) => reply(StatusCode::OK, content_type, bytes),
            Err(e) => error(StatusCode::NOT_FOUND, e.to_string()),
        }
    }

    /// Show a produced file in Explorer (selected, never opened or executed).
    fn reveal(&self, body: &Value) -> Result<Reply> {
        let path = PathBuf::from(body["path"].as_str().unwrap_or(""));
        let target = if path.exists() { path } else { path.parent().filter(|p| p.is_dir()).map(Path::to_path_buf).ok_or_else(|| anyhow!("không tìm thấy tệp"))? };
        let arg = if target.is_file() { format!("/select,{}", target.display()) } else { target.display().to_string() };
        Command::new("explorer.exe").raw_arg(arg).spawn()?;
        Ok(json_reply(StatusCode::OK, json!({"ok": true})))
    }
}

/// Not finished, but its runner process is gone (crash, reboot, killed): it will never move on its own.
fn interrupted(task: &Value) -> bool {
    let status = task["status"].as_str().unwrap_or("");
    let unfinished = !matches!(status, "completed" | "failed" | "cancelled" | "needs_user");
    unfinished && task["runner_pid"].as_u64().is_some_and(|pid| !process_alive(pid as u32))
}

fn process_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
    // SAFETY: the handle is checked and closed; GetExitCodeProcess writes into a local.
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut code = 0u32;
        let alive = GetExitCodeProcess(handle, &mut code) != 0 && code == STILL_ACTIVE as u32;
        CloseHandle(handle);
        alive
    }
}

/// Run a short CLI command without a console window; kill it after `timeout`.
fn run_with_timeout(program: &Path, args: &[&str], cwd: &Path, timeout: Duration) -> Result<(i32, String)> {
    let mut child = Command::new(program)
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()?;
    let started = std::time::Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            let output = child.wait_with_output()?;
            let text = if output.stdout.is_empty() { output.stderr } else { output.stdout };
            return Ok((status.code().unwrap_or(-1), String::from_utf8_lossy(&text).trim().to_string()));
        }
        if started.elapsed() > timeout {
            let _ = child.kill();
            return Err(anyhow!("agent command timed out"));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_ids_are_restricted() {
        assert!(valid_task_id("20261008-101010-abc123"));
        assert!(!valid_task_id("../etc"));
        assert!(!valid_task_id(""));
        assert!(!valid_task_id(&"a".repeat(81)));
    }

    #[test]
    fn embedded_formula_assets_are_served_and_nothing_else() {
        let (mime, js) = crate::vendor::asset("katex/katex.min.js").expect("katex.js is embedded");
        assert!(mime.starts_with("application/javascript") && js.len() > 100_000);
        let (_, css) = crate::vendor::asset("katex/katex.min.css").unwrap();
        let css = String::from_utf8_lossy(css);
        // every font the stylesheet names is embedded
        for name in css.split("url(fonts/").skip(1).filter_map(|r| r.split(')').next()) {
            assert!(crate::vendor::asset(&format!("katex/fonts/{name}")).is_some(), "{name}");
        }
        assert!(crate::vendor::asset("katex/../../Cargo.toml").is_none());
        assert!(crate::vendor::asset("katex/fonts/").is_none());
    }

    #[test]
    fn percent_decoding() {
        assert_eq!(percent_decode("C%3A%5Ctemp%5Cx.png"), r"C:\temp\x.png");
        assert_eq!(percent_decode("a+b%2"), "a b%2");
        assert_eq!(percent_decode("%E1%BA%A1"), "ạ");
    }
}

/// Drives the real API against this repository's config and agent CLI (`cargo test -- --ignored`).
#[cfg(test)]
mod live {
    use super::*;

    fn api() -> Api {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let config = Config::load_from(&root).unwrap();
        let agent_exe = ["target/release/agent.exe", "target/debug/agent.exe"].iter().map(|p| root.join(p)).find(|p| p.is_file()).unwrap();
        Api { config, agent_exe }
    }

    fn call(api: &Api, method: Method, uri: &str, body: Value) -> (StatusCode, Value) {
        let request = Request::builder().method(method).uri(format!("lga://localhost{uri}")).body(body.to_string().into_bytes()).unwrap();
        let response = api.handle(request);
        (response.status(), serde_json::from_slice(response.body()).unwrap_or(Value::Null))
    }

    #[test]
    #[ignore]
    fn runs_a_task_end_to_end() {
        let api = api();
        let (code, status) = call(&api, Method::GET, "/api/status", Value::Null);
        assert_eq!(code, StatusCode::OK, "{status}");
        println!("status: {status}");
        let (code, created) = call(&api, Method::POST, "/api/tasks", json!({"task": "Chạy lệnh PowerShell Get-Date và cho tôi biết hôm nay là thứ mấy", "mode": "full_access"}));
        assert_eq!(code, StatusCode::OK, "{created}");
        let id = created["task_id"].as_str().unwrap().to_string();
        let deadline = std::time::Instant::now() + Duration::from_secs(240);
        let task = loop {
            let (code, data) = call(&api, Method::GET, &format!("/api/tasks/{id}"), Value::Null);
            if code == StatusCode::OK && ["completed", "failed", "cancelled"].contains(&data["task"]["status"].as_str().unwrap_or("")) {
                break data["task"].clone();
            }
            assert!(std::time::Instant::now() < deadline, "task did not finish");
            std::thread::sleep(Duration::from_secs(2));
        };
        let (_, events) = call(&api, Method::GET, &format!("/api/tasks/{id}/events?after=0"), Value::Null);
        println!("final: {} | {} events | {}", task["status"], events["events"].as_array().unwrap().len(), task["final_summary"]);
        assert_eq!(task["status"], "completed");
        let (_, list) = call(&api, Method::GET, "/api/tasks", Value::Null);
        assert_eq!(list["tasks"][0]["task_id"], id.as_str());
        assert_eq!(call(&api, Method::GET, "/api/artifact?path=C%3A%5CWindows%5Cwin.ini", Value::Null).0, StatusCode::NOT_FOUND);
    }
}
