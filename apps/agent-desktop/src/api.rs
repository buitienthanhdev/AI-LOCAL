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

use crate::{extras, services};

const INDEX_HTML: &str = include_str!("../ui/index.html");
const MAX_TASK_CHARS: usize = 4000;
/// Files the user can attach to one message, and the size of each (the gateway forwards images up to 12 MB;
/// documents are read by tools, so they may be larger).
const MAX_ATTACHMENTS: usize = 8;
const MAX_ATTACHMENT_BYTES: usize = 25 * 1024 * 1024;
const MAX_TASKS_LISTED: usize = 100;
const MAX_EVENTS_PER_POLL: usize = 500;
const CONTROL_TIMEOUT: Duration = Duration::from_secs(30);
const SCREEN_CONTROL_MODE: &str = "pause_on_activity";
const VALID_MODES: [&str; 3] = ["normal", "trusted", "full_access"];
const GEMINI_API_KEY_SECRET: &str = "LGA_GEMINI_API_KEY";
const OPENROUTER_API_KEY_SECRET: &str = "LGA_OPENROUTER_API_KEY";
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
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3])
                    .ok()
                    .and_then(|h| u8::from_str_radix(h, 16).ok());
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
        self.config
            .logs_dir()
            .join("tasks")
            .join(format!("{task_id}.jsonl"))
    }

    fn screen_control_mode(&self, task_id: &str) -> Result<String> {
        let path = self.tasks_dir().join(format!("{task_id}.screen-mode"));
        match fs::read_to_string(path) {
            Ok(mode) if mode.trim() == SCREEN_CONTROL_MODE => Ok(SCREEN_CONTROL_MODE.to_string()),
            Ok(mode) => Err(anyhow!(
                "unsupported screen-control mode stored for task: {}",
                mode.trim()
            )),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                Ok(SCREEN_CONTROL_MODE.to_string())
            }
            Err(error) => Err(error.into()),
        }
    }

    /// Every call of the page is logged as one structured line (`logs/ui/api.jsonl`): time, correlation id (the page
    /// sends `X-Corr-Id`: the task it concerns, else a random id), method, path without query, status, duration. Bodies,
    /// queries and answers are never logged. Static assets and the 15 s status polls are skipped.
    pub fn handle(&self, request: Request<Vec<u8>>) -> Reply {
        let started = std::time::Instant::now();
        let method = request.method().to_string();
        let path = request.uri().path().to_string();
        let corr = request
            .headers()
            .get("x-corr-id")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || *c == '-')
            .take(48)
            .collect::<String>();
        let reply = self.handle_inner(request);
        let noisy = path.starts_with("/vendor/")
            || path == "/"
            || (method == "GET"
                && (path == "/api/status" || path.ends_with("/events") || path == "/api/tasks"));
        if !noisy {
            self.log_api(
                &corr,
                &method,
                &path,
                reply.status().as_u16(),
                started.elapsed().as_millis() as u64,
            );
        }
        reply
    }

    fn log_api(&self, corr: &str, method: &str, path: &str, status: u16, ms: u64) {
        let dir = self.config.logs_dir().join("ui");
        let _ = fs::create_dir_all(&dir);
        let file = dir.join("api.jsonl");
        if fs::metadata(&file)
            .map(|m| m.len() > 5 * 1024 * 1024)
            .unwrap_or(false)
        {
            let _ = fs::rename(&file, dir.join("api.1.jsonl"));
        }
        let line = json!({"ts": chrono::Utc::now().to_rfc3339(), "corr": corr, "method": method, "path": path, "status": status, "ms": ms});
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&file) {
            use std::io::Write;
            let _ = writeln!(f, "{line}");
        }
    }

    fn handle_inner(&self, request: Request<Vec<u8>>) -> Reply {
        let path = request.uri().path().trim_end_matches('/').to_string();
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        let method = request.method().clone();
        let body: Value = serde_json::from_slice(request.body()).unwrap_or(Value::Null);
        let result = match (&method, segments.as_slice()) {
            (&Method::GET, []) => {
                return reply(
                    StatusCode::OK,
                    "text/html; charset=utf-8",
                    INDEX_HTML.as_bytes().to_vec(),
                )
            }
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
            (&Method::GET, ["api", "learning"]) => Ok(self.learning()),
            (&Method::GET, ["api", "knowledge", "detail"]) => {
                let knowledge = lga_core::native::knowledge::Knowledge::new(&self.config);
                Ok(match knowledge.detail() {
                    Ok(v) => json_reply(StatusCode::OK, v),
                    Err(e) => error(
                        StatusCode::SERVICE_UNAVAILABLE,
                        format!("chưa đọc được chi tiết tri thức: {e}"),
                    ),
                })
            }
            (&Method::POST, ["api", "knowledge", "update"]) => {
                let knowledge = lga_core::native::knowledge::Knowledge::new(&self.config);
                let paths: Vec<String> = body["paths"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();
                knowledge
                    .start_update(&paths, false)
                    .map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::GET, ["api", "memory"]) => extras::memory_list(
                &self.config,
                &query_param(request.uri(), "q").unwrap_or_default(),
            )
            .map(|v| json_reply(StatusCode::OK, v)),
            (&Method::POST, ["api", "memory"]) => {
                extras::memory_add(&self.config, &body).map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::POST, ["api", "memory", id, "update"]) => {
                extras::memory_update(&self.config, id, &body)
                    .map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::POST, ["api", "memory", id, "delete"]) => {
                extras::memory_delete(&self.config, id).map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::POST, ["api", "feedback"]) => {
                extras::feedback(&self.config, &body).map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::POST, ["api", "open"]) => Ok(json_reply(
                StatusCode::OK,
                crate::openlink::handle(&self.config, &body),
            )),
            (&Method::GET, ["api", "skills"]) => Ok(json_reply(
                StatusCode::OK,
                extras::list_skills(&self.config),
            )),
            (&Method::POST, ["api", "skills"]) => {
                extras::save_skill(&self.config, &body).map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::POST, ["api", "skills", id, "delete"]) => {
                extras::delete_skill(&self.config, id).map(|v| json_reply(StatusCode::OK, v))
            }
            (&Method::GET, ["api", "usage"]) => {
                let days = query_param(request.uri(), "days")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(7)
                    .clamp(1, 60);
                Ok(json_reply(
                    StatusCode::OK,
                    extras::usage(&self.config, days),
                ))
            }
            (&Method::GET, ["api", "activity"]) => {
                let period = query_param(request.uri(), "period").unwrap_or_else(|| "today".into());
                Ok(json_reply(
                    StatusCode::OK,
                    crate::activity::summary(&self.config, &period),
                ))
            }
            (&Method::POST, ["api", "activity", "control"]) => {
                let action = body["action"].as_str().unwrap_or("");
                match crate::activity::control(&self.config, action) {
                    Ok(v) => Ok(json_reply(StatusCode::OK, v)),
                    Err(e) => Ok(error(StatusCode::BAD_REQUEST, e.to_string())),
                }
            }
            (&Method::GET, ["api", "status"]) => Ok(json_reply(StatusCode::OK, self.status())),
            (&Method::GET, ["api", "tasks"]) => Ok(json_reply(StatusCode::OK, self.list_tasks())),
            (&Method::POST, ["api", "tasks"]) => self.start_task(&body),
            (&Method::GET, ["api", "artifact"]) => Ok(self.artifact(request.uri())),
            (&Method::POST, ["api", "reveal"]) => self.reveal(&body),
            (&Method::POST, ["api", "conversations", id, "delete"]) if valid_task_id(id) => {
                Ok(self.delete_history(Some(id)))
            }
            (&Method::POST, ["api", "history", "clear"]) => Ok(self.delete_history(None)),
            (&Method::POST, ["api", "conversations", id, "meta"]) if valid_task_id(id) => {
                self.set_conversation_meta(id, &body)
            }
            (&Method::GET, ["api", "settings"]) => Ok(json_reply(StatusCode::OK, self.settings())),
            (&Method::POST, ["api", "settings"]) => self.save_settings(&body),
            (&Method::GET, ["api", "tasks", id]) if valid_task_id(id) => {
                // `?wait=<token>&timeout=<s>`: hold the call until the task changed (or the time is up), so the page
                // sees every change within milliseconds without asking every second.
                if let Some(token) = query_param(request.uri(), "wait") {
                    let limit = query_param(request.uri(), "timeout")
                        .and_then(|v| v.parse::<u64>().ok())
                        .unwrap_or(10)
                        .clamp(1, 25);
                    let deadline = std::time::Instant::now() + Duration::from_secs(limit);
                    while self.task_token(id) == token && std::time::Instant::now() < deadline {
                        std::thread::sleep(Duration::from_millis(60));
                    }
                }
                Ok(self.get_task(id))
            }
            (&Method::GET, ["api", "tasks", id, "events"]) if valid_task_id(id) => {
                let after = query_param(request.uri(), "after")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0);
                Ok(json_reply(StatusCode::OK, self.events(id, after)))
            }
            (&Method::POST, ["api", "tasks", id, "respond"]) if valid_task_id(id) => {
                self.respond(id, &body)
            }
            (&Method::POST, ["api", "tasks", id, action]) if valid_task_id(id) => {
                self.control(id, action, &body)
            }
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
        let set = |name| lga_core::secrets::resolve_secret(&self.config.root, name).is_some();
        json!({
            "gemini_api_key_set": set(GEMINI_API_KEY_SECRET),
            "openrouter_api_key_set": set(OPENROUTER_API_KEY_SECRET),
        })
    }

    /// Save (or with an empty value remove) the Gemini API key in the repository's git-ignored `.env`, then
    /// restart the gateway so it uses the key.
    fn save_settings(&self, body: &Value) -> Result<Reply> {
        let (field, secret) = if body.get("openrouter_api_key").is_some() {
            ("openrouter_api_key", OPENROUTER_API_KEY_SECRET)
        } else {
            ("gemini_api_key", GEMINI_API_KEY_SECRET)
        };
        let Some(key) = body.get(field).and_then(Value::as_str) else {
            return Err(anyhow!("nothing to save"));
        };
        let key = key.trim();
        if !key.is_empty()
            && (key.len() < 20
                || key.len() > 200
                || !key
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.')))
        {
            return Err(anyhow!(
                "Khóa API không hợp lệ (dán đúng chuỗi khóa của nhà cung cấp)"
            ));
        }
        let env_path = self.config.root.join(".env");
        let existing = fs::read_to_string(&env_path).unwrap_or_default();
        let mut lines: Vec<String> = existing
            .lines()
            .filter(|l| {
                l.split_once('=')
                    .is_none_or(|(k, _)| k.trim() != secret)
            })
            .map(String::from)
            .collect();
        if !key.is_empty() {
            lines.push(format!("{secret}={key}"));
        }
        fs::write(&env_path, lines.join("\n") + "\n")?;
        let agent = self.agent_exe.clone();
        let root = self.config.root.clone();
        std::thread::spawn(move || {
            for action in ["stop", "start"] {
                let _ =
                    run_with_timeout(&agent, &["gateway", action], &root, Duration::from_secs(90));
                if action == "stop" {
                    std::thread::sleep(Duration::from_secs(3));
                }
            }
        });
        Ok(json_reply(
            StatusCode::OK,
            json!({"ok": true, "key_set": !key.is_empty()}),
        ))
    }

    /// How much of the machine's data the assistant has learned, as a tree that adds up to 100 %.
    fn learning(&self) -> Reply {
        let knowledge = lga_core::native::knowledge::Knowledge::new(&self.config);
        match knowledge.learning_progress() {
            Ok(v) => json_reply(StatusCode::OK, v),
            Err(e) => error(
                StatusCode::SERVICE_UNAVAILABLE,
                format!("chưa đọc được danh mục tri thức: {e}"),
            ),
        }
    }

    fn list_tasks(&self) -> Value {
        let mut entries: Vec<(SystemTime, PathBuf)> = fs::read_dir(self.tasks_dir())
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                name.ends_with(".json")
                    && !name.ends_with(".request.json")
                    && !name.ends_with(".response.json")
            })
            .filter_map(|p| Some((fs::metadata(&p).ok()?.modified().ok()?, p)))
            .collect();
        entries.sort_by_key(|e| std::cmp::Reverse(e.0));
        for (_, path) in &entries {
            reap_interrupted(path);
        }
        let tasks: Vec<Value> = entries
            .iter()
            .filter_map(|(_, p)| read_json(p))
            .filter(|r| r.get("task_id").is_some())
            .take(MAX_TASKS_LISTED)
            .map(|r| {
                let summary: String = r["final_summary"].as_str().unwrap_or("").chars().take(400).collect();
                json!({
                    "summary": summary,
                    "task_id": r["task_id"], "goal": r["goal"], "status": r["status"], "mode": r["mode"],
                    "updated_at": r["updated_at"], "created_at": r["created_at"],
                    "steps": r["steps"].as_array().map_or(0, Vec::len),
                    "completion_basis": r["completion_basis"],
                    "attachments": r["attachments"].as_array().map_or(0, Vec::len),
                    "interrupted": interrupted(&r),
                    "conversation_id": self.conversation_of(r["task_id"].as_str().unwrap_or("")),
                })
            })
            .collect();
        json!({ "tasks": tasks, "meta": self.conversation_meta() })
    }

    fn meta_path(&self) -> PathBuf {
        self.tasks_dir().join("_conversations.meta.json")
    }

    /// Titles and pins the person set on conversations: `{conversation id: {title, pinned}}`.
    fn conversation_meta(&self) -> Value {
        read_json(&self.meta_path())
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({}))
    }

    fn set_conversation_meta(&self, id: &str, body: &Value) -> Result<Reply> {
        let mut meta = self.conversation_meta();
        let entry = meta[id].take();
        let mut entry = if entry.is_object() { entry } else { json!({}) };
        if let Some(title) = body["title"].as_str() {
            let title: String = title.trim().chars().take(120).collect();
            if title.is_empty() {
                entry.as_object_mut().map(|o| o.remove("title"));
            } else {
                entry["title"] = json!(title);
            }
        }
        if let Some(pinned) = body["pinned"].as_bool() {
            if pinned {
                entry["pinned"] = json!(true);
            } else {
                entry.as_object_mut().map(|o| o.remove("pinned"));
            }
        }
        if entry.as_object().is_some_and(|o| o.is_empty()) {
            meta.as_object_mut().map(|o| o.remove(id));
        } else {
            meta[id] = entry;
        }
        let tmp = self.meta_path().with_extension("tmp");
        fs::write(&tmp, serde_json::to_vec_pretty(&meta)?)?;
        fs::rename(&tmp, self.meta_path())?;
        Ok(json_reply(StatusCode::OK, json!({"ok": true})))
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
                    && r["runner_pid"]
                        .as_u64()
                        .is_some_and(|pid| process_alive(pid as u32))
            });
            if busy {
                running += 1;
                continue;
            }
            let prefix = format!("{id}.");
            for dir in [
                self.tasks_dir(),
                self.config.logs_dir().join("tasks"),
                self.config.logs_dir().join("ui"),
            ] {
                for entry in fs::read_dir(&dir).into_iter().flatten().flatten() {
                    if entry.file_name().to_string_lossy().starts_with(&prefix) {
                        let _ = fs::remove_file(entry.path());
                    }
                }
            }
            let _ = fs::remove_dir_all(self.config.artifacts_dir().join(&id));
            deleted += 1;
        }
        json_reply(
            StatusCode::OK,
            json!({"ok": true, "deleted": deleted, "running": running}),
        )
    }

    /// A cheap fingerprint of everything the page shows for a task: its record, its audit log, the answer being written,
    /// the pending question and the pause flag. It changes whenever any of them changes.
    fn task_token(&self, id: &str) -> String {
        let stamp = |p: &Path| -> String {
            fs::metadata(p)
                .map(|m| {
                    format!(
                        "{}:{}",
                        m.len(),
                        m.modified()
                            .ok()
                            .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
                            .map_or(0, |d| d.as_millis())
                    )
                })
                .unwrap_or_else(|_| "-".into())
        };
        let dir = self.tasks_dir();
        format!(
            "{}|{}|{}|{}|{}|{}",
            stamp(&dir.join(format!("{id}.json"))),
            stamp(&self.audit_path(id)),
            stamp(&dir.join(format!("{id}.partial.json"))),
            stamp(&request_path(&dir, id)),
            stamp(&dir.join(format!("{id}.pause"))),
            stamp(&dir.join(format!("{id}.activity-pause")))
        )
    }

    fn get_task(&self, id: &str) -> Reply {
        let Some(task) = read_json(&self.tasks_dir().join(format!("{id}.json"))) else {
            return error(StatusCode::NOT_FOUND, "unknown task");
        };
        let pending = read_json(&request_path(&self.tasks_dir(), id));
        let manual_paused = self.tasks_dir().join(format!("{id}.pause")).exists();
        let auto_paused = self
            .tasks_dir()
            .join(format!("{id}.activity-pause"))
            .exists();
        // A task that is not finished but whose runner process is gone (crash, reboot, killed) will never
        // move again on its own: the UI offers to run it again instead of showing it as busy forever.
        let interrupted = interrupted(&task);
        // The answer text of a reply that is still being written (core: `<id>.partial.json`), for the live card.
        let partial = if interrupted {
            None
        } else {
            read_json(&self.tasks_dir().join(format!("{id}.partial.json")))
                .and_then(|p| p["text"].as_str().map(String::from))
        };
        json_reply(
            StatusCode::OK,
            json!({"token": self.task_token(id), "task": task, "pending_request": pending,
                                          "pause_requested": manual_paused || auto_paused, "auto_paused": auto_paused, "partial": partial,
                                          "interrupted": interrupted, "conversation_id": self.conversation_of(id)}),
        )
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
                let Ok(event) = serde_json::from_str::<Value>(&line) else {
                    break;
                };
                events.push(event);
                cursor = number;
                if events.len() >= MAX_EVENTS_PER_POLL {
                    break;
                }
            }
        }
        json!({"events": events, "next": cursor})
    }

    /// `envs` pins per-request knobs such as the brain and screen-control mode.
    fn spawn_agent_with(&self, args: &[&str], log_name: &str, envs: &[(&str, &str)]) -> Result<()> {
        let log_dir = self.config.logs_dir().join("ui");
        fs::create_dir_all(&log_dir)?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_dir.join(log_name))?;
        Command::new(&self.agent_exe)
            .args(args)
            .envs(envs.iter().copied())
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
        let Some(items) = body["attachments"].as_array() else {
            return Ok(Vec::new());
        };
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
                return Err(anyhow!(
                    "Tệp {name} lớn hơn {} MB",
                    MAX_ATTACHMENT_BYTES / (1024 * 1024)
                ));
            }
            let mut target = dir.join(&name);
            let mut n = 1;
            while target.exists() {
                n += 1;
                let path = Path::new(&name);
                let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
                let ext = path
                    .extension()
                    .and_then(|e| e.to_str())
                    .map(|e| format!(".{e}"))
                    .unwrap_or_default();
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
        let screen_control_mode = body["screen_control_mode"]
            .as_str()
            .unwrap_or(SCREEN_CONTROL_MODE);
        let has_files = body["attachments"]
            .as_array()
            .is_some_and(|a| !a.is_empty());
        if (task.is_empty() && !has_files) || task.chars().count() > MAX_TASK_CHARS {
            return Err(anyhow!(
                "Yêu cầu phải dài 1–{MAX_TASK_CHARS} ký tự (hoặc có tệp đính kèm)"
            ));
        }
        if !VALID_MODES.contains(&mode) {
            return Err(anyhow!("chế độ không hợp lệ"));
        }
        if screen_control_mode != SCREEN_CONTROL_MODE {
            return Err(anyhow!("chế độ điều khiển màn hình này chưa được hỗ trợ"));
        }
        let task_id = new_task_id();
        // A follow-up names the conversation it continues; the first request of a chat starts one with its own id.
        let conversation = body["conversation_id"]
            .as_str()
            .filter(|c| valid_task_id(c))
            .unwrap_or(&task_id)
            .to_string();
        fs::create_dir_all(self.tasks_dir())?;
        fs::write(
            self.tasks_dir().join(format!("{task_id}.conv")),
            &conversation,
        )?;
        fs::write(
            self.tasks_dir().join(format!("{task_id}.screen-mode")),
            screen_control_mode,
        )?;
        let files = self.save_attachments(&task_id, body)?;
        let mut args: Vec<String> = ["run", "--ui-channel", "--task-id", &task_id, "--mode", mode]
            .iter()
            .map(|s| s.to_string())
            .collect();
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
        // A brain chosen in the model menu ("" / "auto" = the gateway's own order). Names are route names only.
        let brain = body["brain"].as_str().unwrap_or("").trim();
        let brain = if !brain.is_empty()
            && brain != "auto"
            && brain.len() <= 24
            && brain.chars().all(|c| c.is_ascii_lowercase() || c == '_')
        {
            brain
        } else {
            ""
        };
        let mut envs: Vec<(&str, &str)> = vec![("LGA_SCREEN_CONTROL_MODE", screen_control_mode)];
        if !brain.is_empty() {
            envs.push(("LGA_BRAIN", brain));
        }
        self.spawn_agent_with(&args, &format!("{task_id}.out.log"), &envs)?;
        Ok(json_reply(
            StatusCode::OK,
            json!({"task_id": task_id, "conversation_id": conversation, "attachments": files.len()}),
        ))
    }

    fn control(&self, id: &str, action: &str, body: &Value) -> Result<Reply> {
        match action {
            "resume" => {
                let screen_control_mode = self.screen_control_mode(id)?;
                let files = self.save_attachments(id, body)?;
                let mut args: Vec<String> = ["resume", "--ui-channel", id]
                    .iter()
                    .map(|s| s.to_string())
                    .collect();
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
                self.spawn_agent_with(
                    &args,
                    &format!("{id}.resume.log"),
                    &[("LGA_SCREEN_CONTROL_MODE", &screen_control_mode)],
                )?;
                Ok(json_reply(
                    StatusCode::OK,
                    json!({"ok": true, "output": "resuming"}),
                ))
            }
            "say" => {
                let text = body["text"].as_str().unwrap_or("").trim();
                if text.is_empty() || text.chars().count() > MAX_TASK_CHARS {
                    return Err(anyhow!("Tin nhắn phải dài 1–{MAX_TASK_CHARS} ký tự"));
                }
                let record = read_json(&self.tasks_dir().join(format!("{id}.json")))
                    .ok_or_else(|| anyhow!("không thấy việc này"))?;
                let status = record["status"].as_str().unwrap_or("");
                if matches!(status, "completed" | "failed" | "cancelled") || interrupted(&record) {
                    return Err(anyhow!("việc này đã dừng; hãy gửi như một yêu cầu mới"));
                }
                let path = self.tasks_dir().join(format!("{id}.say.jsonl"));
                let mut file = OpenOptions::new().create(true).append(true).open(path)?;
                use std::io::Write;
                writeln!(
                    file,
                    "{}",
                    json!({"text": text, "at": chrono::Utc::now().to_rfc3339()})
                )?;
                Ok(json_reply(StatusCode::OK, json!({"ok": true})))
            }
            "cancel" | "pause" | "unpause" => {
                let (code, output) = run_with_timeout(
                    &self.agent_exe,
                    &[action, id],
                    &self.config.root,
                    CONTROL_TIMEOUT,
                )?;
                let status = if code == 0 {
                    StatusCode::OK
                } else {
                    StatusCode::CONFLICT
                };
                Ok(json_reply(
                    status,
                    json!({"ok": code == 0, "output": output}),
                ))
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
        let (Ok(root), Ok(file)) = (
            self.config.artifacts_dir().canonicalize(),
            Path::new(&requested).canonicalize(),
        ) else {
            return error(StatusCode::NOT_FOUND, "not found");
        };
        if !file.starts_with(&root) || !file.is_file() {
            return error(StatusCode::NOT_FOUND, "not found");
        }
        let content_type = match file
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
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
        let target = if path.exists() {
            path
        } else {
            path.parent()
                .filter(|p| p.is_dir())
                .map(Path::to_path_buf)
                .ok_or_else(|| anyhow!("không tìm thấy tệp"))?
        };
        let arg = if target.is_file() {
            format!("/select,{}", target.display())
        } else {
            target.display().to_string()
        };
        Command::new("explorer.exe").raw_arg(arg).spawn()?;
        Ok(json_reply(StatusCode::OK, json!({"ok": true})))
    }
}

/// Not finished, but its runner process is gone (crash, reboot, killed): it will never move on its own.
fn interrupted(task: &Value) -> bool {
    let status = task["status"].as_str().unwrap_or("");
    let unfinished = !matches!(status, "completed" | "failed" | "cancelled" | "needs_user");
    unfinished
        && task["runner_pid"]
            .as_u64()
            .is_some_and(|pid| !process_alive(pid as u32))
}

/// A task whose runner process is gone while it was still planning/executing will never move on: mark it failed
/// with a plain reason instead of leaving it "planning" forever.
fn reap_interrupted(path: &Path) {
    let Some(mut task) = read_json(path) else {
        return;
    };
    let status = task["status"].as_str().unwrap_or("");
    if !matches!(status, "planning" | "executing") || !interrupted(&task) {
        return;
    }
    task["status"] = json!("failed");
    task["failure_reason"] = json!("The task was interrupted: its runner process ended before it finished (crash, shutdown or no brain available).");
    if let Ok(text) = serde_json::to_string_pretty(&task) {
        let tmp = path.with_extension("json.tmp");
        if fs::write(&tmp, text).is_ok() {
            let _ = fs::rename(&tmp, path);
        }
    }
}

fn process_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
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
fn run_with_timeout(
    program: &Path,
    args: &[&str],
    cwd: &Path,
    timeout: Duration,
) -> Result<(i32, String)> {
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
            let text = if output.stdout.is_empty() {
                output.stderr
            } else {
                output.stdout
            };
            return Ok((
                status.code().unwrap_or(-1),
                String::from_utf8_lossy(&text).trim().to_string(),
            ));
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
        for name in css
            .split("url(fonts/")
            .skip(1)
            .filter_map(|r| r.split(')').next())
        {
            assert!(
                crate::vendor::asset(&format!("katex/fonts/{name}")).is_some(),
                "{name}"
            );
        }
        assert!(crate::vendor::asset("katex/../../Cargo.toml").is_none());
        assert!(crate::vendor::asset("katex/fonts/").is_none());
    }

    /// The chat window shows how a completion was established; that only works if the API hands the core's
    /// machine-readable fields through, and still serves records written before they existed.
    #[test]
    fn completion_basis_reaches_the_ui_and_old_records_still_load() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let root =
            std::env::temp_dir().join(format!("lga-api-test-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(root.join("configs")).unwrap();
        std::fs::copy(
            repo.join("configs/config.example.toml"),
            root.join("configs/config.example.toml"),
        )
        .unwrap();
        let config = Config::load_from(&root).unwrap();
        let tasks = config.tasks_dir();
        std::fs::create_dir_all(&tasks).unwrap();
        let base = json!({"version": 1, "goal": "g", "mode": "normal", "status": "completed",
            "created_at": "2026-10-14T00:00:00Z", "updated_at": "2026-10-14T00:00:01Z", "plan": [], "steps": [],
            "consecutive_failures": 0, "worker_restarts": 0, "llm_calls": 0, "final_summary": "done", "failure_reason": null,
            "artifacts": [], "escalation": null, "pending_question": null, "runner_pid": null});
        let mut unverified = base.clone();
        unverified["task_id"] = json!("t-unverified");
        unverified["completion_basis"] = json!("unverified");
        unverified["unverified_checks"] =
            json!(["check 0 (filesystem.stat): exists = false, expected true"]);
        let mut old = base.clone();
        old["task_id"] = json!("t-old");
        std::fs::write(tasks.join("t-unverified.json"), unverified.to_string()).unwrap();
        std::fs::write(tasks.join("t-old.json"), old.to_string()).unwrap();
        let api = Api {
            config,
            agent_exe: PathBuf::from("agent.exe"),
        };
        let get = |uri: &str| {
            let request = Request::builder()
                .method(Method::GET)
                .uri(format!("lga://localhost{uri}"))
                .body(Vec::new())
                .unwrap();
            let response = api.handle(request);
            assert_eq!(response.status(), StatusCode::OK, "{uri}");
            serde_json::from_slice::<Value>(response.body()).unwrap()
        };
        let one = get("/api/tasks/t-unverified");
        assert_eq!(one["task"]["completion_basis"], "unverified");
        assert_eq!(
            one["task"]["unverified_checks"][0],
            "check 0 (filesystem.stat): exists = false, expected true"
        );
        let older = get("/api/tasks/t-old");
        assert!(
            older["task"].get("completion_basis").is_none(),
            "an old record must not gain a basis it never had"
        );
        let list = get("/api/tasks");
        let find = |id: &str| {
            list["tasks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|t| t["task_id"] == id)
                .cloned()
                .unwrap()
        };
        assert_eq!(find("t-unverified")["completion_basis"], "unverified");
        assert!(find("t-old")["completion_basis"].is_null());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn activity_pause_is_visible_and_unsupported_screen_modes_are_rejected() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let root = std::env::temp_dir().join(format!(
            "lga-screen-mode-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(root.join("configs")).unwrap();
        std::fs::copy(
            repo.join("configs/config.example.toml"),
            root.join("configs/config.example.toml"),
        )
        .unwrap();
        let config = Config::load_from(&root).unwrap();
        let api = Api {
            config,
            agent_exe: PathBuf::from("agent.exe"),
        };
        let tasks = api.tasks_dir();
        std::fs::create_dir_all(&tasks).unwrap();
        let id = format!("screen-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
        std::fs::write(
            tasks.join(format!("{id}.json")),
            json!({"task_id": id, "goal": "x", "status": "paused", "runner_pid": std::process::id()}).to_string(),
        ).unwrap();
        std::fs::write(tasks.join(format!("{id}.activity-pause")), "activity").unwrap();

        let request = Request::builder()
            .method(Method::GET)
            .uri(format!("lga://localhost/api/tasks/{id}"))
            .body(Vec::new())
            .unwrap();
        let response = api.handle(request);
        assert_eq!(response.status(), StatusCode::OK);
        let task: Value = serde_json::from_slice(response.body()).unwrap();
        assert_eq!(task["pause_requested"], true);
        assert_eq!(task["auto_paused"], true);

        let rejected =
            api.start_task(&json!({"task": "test", "screen_control_mode": "lock_input"}));
        assert!(rejected
            .unwrap_err()
            .to_string()
            .contains("chưa được hỗ trợ"));
        let _ = std::fs::remove_dir_all(&root);
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
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        let config = Config::load_from(&root).unwrap();
        let agent_exe = ["target/release/agent.exe", "target/debug/agent.exe"]
            .iter()
            .map(|p| root.join(p))
            .find(|p| p.is_file())
            .unwrap();
        Api { config, agent_exe }
    }

    fn call(api: &Api, method: Method, uri: &str, body: Value) -> (StatusCode, Value) {
        let request = Request::builder()
            .method(method)
            .uri(format!("lga://localhost{uri}"))
            .body(body.to_string().into_bytes())
            .unwrap();
        let response = api.handle(request);
        (
            response.status(),
            serde_json::from_slice(response.body()).unwrap_or(Value::Null),
        )
    }

    #[test]
    #[ignore]
    fn activity_summary_reads_the_tracker_database() {
        let api = api();
        let (code, v) = call(&api, Method::GET, "/api/activity?period=today", Value::Null);
        assert_eq!(code, StatusCode::OK, "{v}");
        assert!(v["enabled"].is_boolean() && v["apps"].is_array(), "{v}");
        let active = v["active_seconds"].as_f64().unwrap_or(0.0);
        if active > 0.0 {
            let sum: f64 = v["apps"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| a["seconds"].as_f64().unwrap())
                .sum();
            assert!(sum <= active + 1.0);
        }
        std::fs::write(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../logs/dev/activity.json"),
            v.to_string(),
        )
        .unwrap();
    }

    #[test]
    #[ignore]
    fn knowledge_detail_lists_folders_and_counts() {
        let api = api();
        let (code, v) = call(&api, Method::GET, "/api/knowledge/detail", Value::Null);
        assert_eq!(code, StatusCode::OK, "{v}");
        assert!(
            v["folders"].as_array().is_some_and(|f| !f.is_empty()),
            "{v}"
        );
        assert!(v["counts"]["chunks"].as_i64().unwrap_or(0) > 0);
        std::fs::write(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../logs/dev/knowledge_detail.json"),
            v.to_string(),
        )
        .unwrap();
    }

    #[test]
    fn user_skills_round_trip_through_the_api() {
        let api = api();
        let (code, saved) = call(
            &api,
            Method::POST,
            "/api/skills",
            json!({"name": "Thử nghiệm", "kind": "lesson", "triggers": ["bao cao", "excel"], "body": "Luôn ghi rõ đơn vị tiền."}),
        );
        assert_eq!(code, StatusCode::OK, "{saved}");
        let id = saved["id"].as_str().unwrap().to_string();
        let (_, list) = call(&api, Method::GET, "/api/skills", Value::Null);
        let found = list["skills"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["id"] == id.as_str())
            .expect("listed");
        assert_eq!(found["kind"], "lesson");
        assert_eq!(found["triggers"], json!(["bao cao", "excel"]));
        let (code, _) = call(
            &api,
            Method::POST,
            "/api/skills",
            json!({"name": "x", "body": "y", "triggers": []}),
        );
        assert_eq!(
            code,
            StatusCode::BAD_REQUEST,
            "a skill without triggers is refused"
        );
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/skills/{id}/delete"),
            json!({}),
        );
        assert_eq!(code, StatusCode::OK);
        let (_, list) = call(&api, Method::GET, "/api/skills", Value::Null);
        assert!(!list["skills"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s["id"] == id.as_str()));
    }

    #[test]
    fn memory_api_adds_lists_switches_off_and_deletes() {
        let api = api();
        let marker = format!("quytacthu{}", uuid::Uuid::new_v4().simple());
        let (code, added) = call(
            &api,
            Method::POST,
            "/api/memory",
            json!({"text": format!("Luôn kiểm tra {marker} trước khi gửi báo cáo"), "scope": "project"}),
        );
        assert_eq!(code, StatusCode::OK, "{added}");
        let id = added["id"].as_str().unwrap().to_string();
        let (_, list) = call(
            &api,
            Method::GET,
            &format!("/api/memory?q={marker}"),
            Value::Null,
        );
        assert_eq!(list["items"].as_array().unwrap().len(), 1, "{list}");
        let (code, _) = call(
            &api,
            Method::POST,
            "/api/memory",
            json!({"text": "mật khẩu của tôi: hunter2 12345"}),
        );
        assert_eq!(code, StatusCode::BAD_REQUEST, "a secret is refused");
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/memory/{id}/update"),
            json!({"status": "disabled"}),
        );
        assert_eq!(code, StatusCode::OK);
        let (_, list) = call(
            &api,
            Method::GET,
            &format!("/api/memory?q={marker}"),
            Value::Null,
        );
        assert_eq!(list["items"][0]["status"], "disabled");
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/memory/{id}/delete"),
            json!({}),
        );
        assert_eq!(code, StatusCode::OK);
        let (_, list) = call(
            &api,
            Method::GET,
            &format!("/api/memory?q={marker}"),
            Value::Null,
        );
        assert!(list["items"].as_array().unwrap().is_empty());
    }

    #[test]
    fn messages_sent_while_running_are_queued_for_the_task_and_conversations_can_be_renamed() {
        let api = api();
        let id = format!("t-say-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
        let dir = api.tasks_dir();
        std::fs::write(dir.join(format!("{id}.json")), json!({"task_id": id, "goal": "x", "status": "executing", "runner_pid": std::process::id()}).to_string()).unwrap();
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/tasks/{id}/say"),
            json!({"text": "dùng cột B nhé"}),
        );
        assert_eq!(code, StatusCode::OK);
        let said = std::fs::read_to_string(dir.join(format!("{id}.say.jsonl"))).unwrap();
        assert!(said.contains("dùng cột B nhé"), "{said}");
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/tasks/{id}/say"),
            json!({"text": "  "}),
        );
        assert_eq!(code, StatusCode::BAD_REQUEST);
        std::fs::write(
            dir.join(format!("{id}.json")),
            json!({"task_id": id, "goal": "x", "status": "completed"}).to_string(),
        )
        .unwrap();
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/tasks/{id}/say"),
            json!({"text": "muộn rồi"}),
        );
        assert_eq!(
            code,
            StatusCode::BAD_REQUEST,
            "a finished task takes no more messages"
        );
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/conversations/{id}/meta"),
            json!({"title": "Báo cáo tuần", "pinned": true}),
        );
        assert_eq!(code, StatusCode::OK);
        let (_, list) = call(&api, Method::GET, "/api/tasks", Value::Null);
        assert_eq!(list["meta"][&id]["title"], "Báo cáo tuần");
        assert_eq!(list["meta"][&id]["pinned"], true);
        let (code, _) = call(
            &api,
            Method::POST,
            &format!("/api/conversations/{id}/meta"),
            json!({"title": "", "pinned": false}),
        );
        assert_eq!(code, StatusCode::OK);
        let (_, list) = call(&api, Method::GET, "/api/tasks", Value::Null);
        assert!(
            list["meta"].get(&id).is_none(),
            "an emptied entry is removed"
        );
        for ext in ["json", "say.jsonl"] {
            let _ = std::fs::remove_file(dir.join(format!("{id}.{ext}")));
        }
    }

    #[test]
    fn a_held_task_request_returns_as_soon_as_the_task_changes() {
        let api = std::sync::Arc::new(api());
        let id = format!("t-wait-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
        let path = api.tasks_dir().join(format!("{id}.json"));
        std::fs::write(&path, json!({"task_id": id, "goal": "x", "status": "executing", "runner_pid": std::process::id(), "steps": []}).to_string()).unwrap();
        let (_, first) = call(&api, Method::GET, &format!("/api/tasks/{id}"), Value::Null);
        let token = first["token"].as_str().unwrap().to_string();
        // nothing changes: the call waits for its timeout (1 s) and still answers
        let started = std::time::Instant::now();
        let (code, same) = call(
            &api,
            Method::GET,
            &format!(
                "/api/tasks/{id}?wait={}&timeout=1",
                token.replace('|', "%7C")
            ),
            Value::Null,
        );
        assert_eq!(code, StatusCode::OK);
        assert!(
            started.elapsed() >= Duration::from_millis(900),
            "returned early: {:?}",
            started.elapsed()
        );
        assert_eq!(same["token"], first["token"]);
        // a change releases it at once
        let writer = {
            let path = path.clone();
            let id = id.clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(300));
                std::fs::write(&path, json!({"task_id": id, "goal": "x", "status": "executing", "runner_pid": std::process::id(), "steps": [], "plan": ["a"]}).to_string()).unwrap();
            })
        };
        let started = std::time::Instant::now();
        let (_, changed) = call(
            &api,
            Method::GET,
            &format!(
                "/api/tasks/{id}?wait={}&timeout=10",
                token.replace('|', "%7C")
            ),
            Value::Null,
        );
        writer.join().unwrap();
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "{:?}",
            started.elapsed()
        );
        assert_ne!(changed["token"], first["token"]);
        assert_eq!(changed["task"]["plan"][0], "a");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn usage_reads_the_audit_logs() {
        let api = api();
        let (code, v) = call(&api, Method::GET, "/api/usage?days=7", Value::Null);
        assert_eq!(code, StatusCode::OK, "{v}");
        assert!(v["backends"].is_array());
    }

    #[test]
    #[ignore]
    fn learning_tree_adds_up_to_one_hundred_percent() {
        let api = api();
        let (code, v) = call(&api, Method::GET, "/api/learning", Value::Null);
        assert_eq!(code, StatusCode::OK, "{v}");
        let root = &v["tree"];
        for key in ["percent_files", "percent_bytes"] {
            let sum: f64 = root["children"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c[key].as_f64().unwrap())
                .sum();
            assert!((sum - 100.0).abs() < 0.6, "{key}: children add up to {sum}");
        }
        let files: i64 = root["children"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["files"].as_i64().unwrap())
            .sum();
        assert_eq!(files, root["files"].as_i64().unwrap());
        let learned = &root["children"][0];
        let by_type: i64 = learned["children"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["files"].as_i64().unwrap())
            .sum();
        assert_eq!(by_type, learned["files"].as_i64().unwrap());
        std::fs::write(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../logs/dev/learning.json"),
            v.to_string(),
        )
        .unwrap();
    }

    #[test]
    #[ignore]
    fn runs_a_task_end_to_end() {
        let api = api();
        let (code, status) = call(&api, Method::GET, "/api/status", Value::Null);
        assert_eq!(code, StatusCode::OK, "{status}");
        println!("status: {status}");
        let (code, created) = call(
            &api,
            Method::POST,
            "/api/tasks",
            json!({"task": "Chạy lệnh PowerShell Get-Date và cho tôi biết hôm nay là thứ mấy", "mode": "full_access"}),
        );
        assert_eq!(code, StatusCode::OK, "{created}");
        let id = created["task_id"].as_str().unwrap().to_string();
        let deadline = std::time::Instant::now() + Duration::from_secs(240);
        let task = loop {
            let (code, data) = call(&api, Method::GET, &format!("/api/tasks/{id}"), Value::Null);
            if code == StatusCode::OK
                && ["completed", "failed", "cancelled"]
                    .contains(&data["task"]["status"].as_str().unwrap_or(""))
            {
                break data["task"].clone();
            }
            assert!(std::time::Instant::now() < deadline, "task did not finish");
            std::thread::sleep(Duration::from_secs(2));
        };
        let (_, events) = call(
            &api,
            Method::GET,
            &format!("/api/tasks/{id}/events?after=0"),
            Value::Null,
        );
        println!(
            "final: {} | {} events | {}",
            task["status"],
            events["events"].as_array().unwrap().len(),
            task["final_summary"]
        );
        assert_eq!(task["status"], "completed");
        let (_, list) = call(&api, Method::GET, "/api/tasks", Value::Null);
        assert_eq!(list["tasks"][0]["task_id"], id.as_str());
        assert_eq!(
            call(
                &api,
                Method::GET,
                "/api/artifact?path=C%3A%5CWindows%5Cwin.ini",
                Value::Null
            )
            .0,
            StatusCode::NOT_FOUND
        );
    }
}
