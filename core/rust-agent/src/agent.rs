//! The execution loop:
//!
//! USER TASK -> PLAN -> TOOL CALL -> (validate, policy) -> EXECUTE -> OBSERVE -> VERIFY
//!           -> CONTINUE / RECOVER -> ... -> task.complete -> run completion checks -> COMPLETE
//!
//! The planner (Gemini via `ChatProvider`) only proposes tool calls. This module decides whether and
//! how they run, retries idempotent calls on retryable errors, restarts a dead or hung worker,
//! observes the UI after unverified actions, and accepts completion only when every completion check
//! the planner supplied passes when the core runs it itself.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use chrono::Utc;
use serde_json::{json, Value};

use crate::audit::{input_hash, AuditEvent, AuditLog};
use crate::cancel::CancelToken;
use crate::config::{Config, ExecutionMode};
use crate::error::{ErrorCode, ToolErrorInfo};
use crate::llm::{ChatMessage, ChatProvider, ChatRequest, Part};
use crate::memory::Memory;
use crate::planner::{parse_reply, system_prompt, user_turn, PlannerAction, TurnContext, MAX_ACTIONS_PER_REPLY};
use crate::policy::{Policy, Verdict};
use crate::redact::Redactor;
use crate::retry::Backoff;
use crate::secrets::resolve_secret;
use crate::state::{StepRecord, StepStatus, TaskRecord, TaskStatus, TaskStore, PLANNER_IMAGE_EXTENSIONS};
use crate::tools::{ToolCatalog, ToolSpec, VerificationMode};
use crate::worker::{ToolCall, ToolResponse, WorkerClient, WorkerError};

/// Elements requested for the automatic observation after an unverified desktop action.
const OBSERVE_MAX_ELEMENTS: u64 = 80;
const OBSERVE_TIMEOUT: Duration = Duration::from_secs(30);
const MIN_TOOL_TIMEOUT: Duration = Duration::from_secs(1);
/// Characters of each completion check's result data shown to the verifier.
const VERIFIER_DATA_CHARS: usize = 1500;
const VERIFIER_OBSERVATION_CHARS: usize = 600;
const VERIFIER_RECENT_STEPS: usize = 6;
const VERIFIER_PROMPT: &str = "You are a strict, independent verifier for a Windows automation agent. You receive the user's goal, the agent's claimed summary, the completion checks it ran (with the ACTUAL result data) and its recent steps. Decide whether this evidence proves that the goal was achieved as the user asked. Reject when: the evidence is generic and would also pass if the goal were not achieved (e.g. only 'URL contains github.com' for 'open the Windows-Use repository'); the actual data contradicts the summary (wrong page, wrong file, wrong value); a part of the goal has no evidence at all. Accept only when every part of the goal is supported by the actual data. Reply with exactly one JSON object: {\"verdict\": \"accept\" | \"reject\", \"reason\": \"<one or two sentences; for reject, say what is wrong or what evidence is missing>\"}";

fn clip_text(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        text.to_string()
    } else {
        format!("{}...", text.chars().take(max).collect::<String>())
    }
}

fn clip_json(value: &Value, max: usize) -> Value {
    let text = value.to_string();
    if text.chars().count() <= max {
        value.clone()
    } else {
        Value::String(clip_text(&text, max))
    }
}

/// What the user interface (CLI, future UI) must provide to the runner.
pub trait Interaction: Send + Sync {
    fn event(&self, event: RunEvent<'_>);
    /// Ask for consent to run a call the policy marked "confirm". Returns true to proceed.
    fn confirm(&self, tool: &str, args: &Value, reasons: &[String]) -> bool;
    /// Ask the user a question; None when nobody can answer (non-interactive).
    fn ask(&self, question: &str) -> Option<String>;
}

pub enum RunEvent<'a> {
    Started(&'a TaskRecord),
    Status(TaskStatus, Option<&'a str>),
    Plan(&'a [String]),
    Thinking { step_id: &'a str, thought: &'a str, tool: &'a str, args: &'a Value },
    StepDone(&'a StepRecord),
    Notice(&'a str),
}

pub struct Runtime {
    pub config: Config,
    pub catalog: ToolCatalog,
    pub policy: Policy,
    pub llm: Arc<dyn ChatProvider>,
    pub memory: Memory,
    pub store: TaskStore,
    pub redactor: Redactor,
    worker: Option<Arc<WorkerClient>>,
}

pub fn build_redactor(config: &Config) -> Redactor {
    let mut redactor = Redactor::new();
    for name in &config.observability.redact_env {
        redactor.add_literal(resolve_secret(&config.root, name).as_deref());
    }
    redactor
}

impl Runtime {
    pub fn new(config: Config, mode: ExecutionMode, llm: Arc<dyn ChatProvider>) -> Result<Self> {
        let redactor = build_redactor(&config);
        let catalog = ToolCatalog::load(&config.path(&config.paths.tools_dir))?;
        let policy = Policy::load(&config, mode)?;
        let memory = Memory::open(&config.path(&config.memory.persistent_db), redactor.clone())?;
        let store = TaskStore::new(config.tasks_dir(), config.logs_dir().join("tasks"));
        Ok(Self { config, catalog, policy, llm, memory, store, redactor, worker: None })
    }

    pub async fn worker(&mut self) -> Result<Arc<WorkerClient>> {
        if let Some(worker) = &self.worker {
            if worker.is_alive() {
                return Ok(worker.clone());
            }
        }
        let worker = Arc::new(WorkerClient::spawn(&self.config, self.redactor.clone()).await?);
        self.worker = Some(worker.clone());
        Ok(worker)
    }

    async fn restart_worker(&mut self) -> Result<Arc<WorkerClient>> {
        if let Some(old) = self.worker.take() {
            old.kill().await;
        }
        self.worker().await
    }

    pub async fn shutdown(&mut self) {
        if let Some(worker) = self.worker.take() {
            worker.shutdown().await;
        }
    }

    /// Run one tool outside a task loop (CLI `agent screenshot`, doctor).
    pub async fn call_tool(&mut self, task_id: &str, tool: &str, args: Value) -> Result<ToolResponse> {
        let spec = self.catalog.get(tool).ok_or_else(|| anyhow!("unknown tool {tool}"))?.clone();
        let violations = self.catalog.validate_args(tool, &args);
        if !violations.is_empty() {
            return Err(anyhow!("invalid arguments for {tool}: {}", violations.join("; ")));
        }
        let decision = self.policy.evaluate(&spec, &args);
        if decision.verdict == Verdict::Deny {
            return Err(anyhow!("policy denied {tool}: {}", decision.reasons.join("; ")));
        }
        let worker = self.worker().await?;
        let call = ToolCall::new(task_id, "adhoc", tool, decision.args, Duration::from_millis(spec.timeout_ms));
        Ok(worker.call(&call).await?)
    }
}

struct StepOutcome {
    record: StepRecord,
    observation_for_planner: Option<String>,
    screenshot: Option<PathBuf>,
    /// A state the planner must deal with before the batch goes on (a dialog opened, the window closed).
    surprise: Option<String>,
}

/// What an automatic look at the desktop or browser returned.
struct Observation {
    text: String,
    screenshot: Option<PathBuf>,
    surprise: Option<String>,
}

pub struct TaskRunner<'a> {
    rt: &'a mut Runtime,
    ui: &'a dyn Interaction,
    cancel: CancelToken,
    audit: AuditLog,
    backoff: Backoff,
    notices: Vec<String>,
    latest_observation: Option<String>,
    latest_screenshots: Vec<PathBuf>,
    pending_failures: Vec<(String, ErrorCode, String)>,
    /// "This computer" section of the planner prompt (user, real folder paths), built once per run.
    environment: String,
    /// task.complete attempts rejected so far in this run.
    completion_rejections: u32,
    /// Tool groups shown in full to the planner (the others are listed in one line each until enabled).
    loaded_groups: Vec<String>,
    /// Shown while the agent operates applications with the mouse and keyboard.
    banner: Option<crate::banner::Banner>,
}

/// Steps that operate applications directly: the person should keep their hands off meanwhile.
/// After this many lookups (knowledge.*, web.*, document.read) in a row the planner is told to stop and answer.
const LOOKUP_STREAK_WARN: usize = 6;
const HANDS_ON_TOOLS: [&str; 16] = [
    "desktop.click", "desktop.type", "desktop.press_key", "desktop.hotkey", "desktop.drag", "desktop.scroll",
    "desktop.move_mouse", "desktop.launch_app", "desktop.focus_window", "desktop.close_app", "desktop.set_value",
    "paint.draw", "paint.draw_image", "art.draw", "art.paint", "desktop.save_as",
];
const BANNER_TEXT: &str = "Trợ lý đang điều khiển máy tính — xin tạm thời không dùng chuột, bàn phím. Muốn dừng: bấm Dừng trong cửa sổ Trợ lý.";

/// Tool groups always shown in full; the rest load when the request mentions them or the planner asks.
const BASE_GROUPS: [&str; 7] = ["system", "knowledge", "web", "documents", "memory", "assistant", "task"];
/// Words (lower case, without accents) that load a group from the start.
const GROUP_TRIGGERS: [(&str, &[&str]); 8] = [
    ("office", &["excel", "word", "powerpoint", "outlook", "email", "e-mail", "mail", "thu dien tu", "gui thu", "hop thu", "pdf",
                 ".xlsx", ".docx", ".pptx", "bang tinh", "van ban"]),
    ("desktop", &["mo ", "open", "click", "bam ", "nhan ", "cua so", "window", "ung dung", "app", "notepad", "word", "excel",
                  "powerpoint", "paint", "go ", "type", "nhap ", "man hinh", "screen", "chuot", "mouse", "ban phim",
                  "keyboard", "dong ", "close", "zalo", "chrome", "luu ", "save", "giao dien", "phan mem"]),
    ("browser", &["trang web", "website", "web page", "dang nhap", "login", "form", "trinh duyet", "browser", "youtube",
                  "facebook", "dien ", "tai ve", "download", "google", "http"]),
    ("vision", &["man hinh", "screen", "nhin", "anh chup", "screenshot", "hinh anh", "thay gi", "doc chu tren"]),
    ("autocad", &["autocad", "cad", "ban ve", "mat bang", "dwg", "dxf", "ve "]),
    ("paint", &["paint", "mspaint"]),
    ("activity", &["theo doi", "tracking", "hom nay toi", "toi dang", "toi da lam", "toi lam gi", "dang lam gi", "thoi gian su dung", "screen time", "nang suat", "dung bao lau", "mat bao lau"]),
    ("art", &["ve ", "draw", "tranh", "paint", "mspaint", "autocad", "dwg", "sketch", "ki hoa", "svg", "doraemon", "photo", "hinh anh"]),
];
/// Words that mean AutoCAD even when the request also says "paint" (a bare "ve " only means drawing).
const AUTOCAD_STRONG: [&str; 5] = ["autocad", "dwg", "dxf", "mat bang", "ban ve"];

/// The groups a request needs from the start, from its words (and the previous request it follows up).
fn groups_for(text: &str) -> Vec<String> {
    let normalized = format!(" {} ", brain_core::text::norm(text));
    let mut groups: Vec<String> = BASE_GROUPS.iter().map(|g| g.to_string()).collect();
    for (group, words) in GROUP_TRIGGERS {
        // "ve doraemon bang paint" is a Paint drawing, not an AutoCAD one: the bare word "ve " must not load AutoCAD.
        if group == "autocad" && normalized.contains("paint") && !AUTOCAD_STRONG.iter().any(|w| normalized.contains(w)) {
            continue;
        }
        if words.iter().any(|w| normalized.contains(w)) {
            groups.push(group.to_string());
        }
    }
    groups
}

/// Attached images sent to the planner with every turn (documents are read with tools instead).
const MAX_USER_IMAGES_PER_TURN: usize = 4;
/// After this many rejected completions the task is completed anyway, with the unmet checks reported to
/// the user, instead of failing work that was actually done because a check was phrased badly.
const MAX_COMPLETION_REJECTIONS: u32 = 2;
/// Pauses before retrying a planner call that found no brain available.
const LLM_RETRY_DELAYS: [Duration; 3] = [Duration::from_secs(5), Duration::from_secs(20), Duration::from_secs(45)];

/// Repair the shapes small models give task.complete: `checks` as an object instead of a list, stray
/// non-object check entries, unknown top-level keys, a non-string summary.
fn normalize_completion(args: &Value) -> Value {
    let mut out = serde_json::Map::new();
    if let Some(summary) = args.get("summary") {
        out.insert("summary".into(), summary.as_str().map_or_else(|| json!(summary.to_string()), |s| json!(s)));
    }
    let checks: Vec<Value> = match args.get("checks") {
        Some(Value::Array(items)) => items.clone(),
        Some(Value::Object(map)) => map.values().cloned().collect(),
        _ => Vec::new(),
    };
    let checks: Vec<Value> = checks
        .into_iter()
        .filter(|c| c.get("tool").and_then(Value::as_str).is_some())
        .map(|c| {
            let mut check = serde_json::Map::new();
            for key in ["tool", "args", "expect"] {
                if let Some(v) = c.get(key) {
                    check.insert(key.into(), v.clone());
                }
            }
            check.entry("args").or_insert_with(|| json!({}));
            Value::Object(check)
        })
        .collect();
    if !checks.is_empty() {
        out.insert("checks".into(), Value::Array(checks));
    }
    for key in ["evidence_steps", "artifacts"] {
        if let Some(Value::Array(items)) = args.get(key) {
            let strings: Vec<Value> = items.iter().filter(|v| v.is_string()).cloned().collect();
            if !strings.is_empty() {
                out.insert(key.into(), Value::Array(strings));
            }
        }
    }
    Value::Object(out)
}

/// The shape every planner reply must have, with the tool names limited to the tools of this run. A local
/// model decoding under this schema cannot invent a tool or break the reply format; arguments are still
/// validated against each tool's own schema before anything runs.
fn planner_reply_schema(available: &[String]) -> Value {
    let mut tools: Vec<&str> = available.iter().map(String::as_str).collect();
    for core in ["task.complete", "task.fail", "task.ask_user", "tools.enable"] {
        if !tools.contains(&core) {
            tools.push(core);
        }
    }
    json!({
        "type": "object",
        "required": ["thought", "actions"],
        "properties": {
            "thought": {"type": "string"},
            "plan": {"type": "array", "items": {"type": "string"}},
            "actions": {
                "type": "array", "minItems": 1, "maxItems": MAX_ACTIONS_PER_REPLY,
                "items": {
                    "type": "object", "required": ["tool", "args"],
                    "properties": {"tool": {"enum": tools}, "args": {"type": "object"}}
                }
            }
        }
    })
}

/// Facts about this PC the planner must not guess (user name, real folder locations).
fn describe_computer() -> String {
    let user = std::env::var("USERNAME").unwrap_or_default();
    let computer = std::env::var("COMPUTERNAME").unwrap_or_default();
    let admin = if crate::native::env::is_admin() { "elevated" } else { "normal user rights, not elevated" };
    let now = chrono::Local::now();
    let mut out = format!(
        "Now: {} (UTC{}).\nWindows user: {user} on {computer} ({admin}).\nFolders:\n",
        now.format("%A %Y-%m-%d %H:%M"),
        now.format("%:z")
    );
    for (label, path) in crate::native::env::known_folders() {
        out.push_str(&format!("- {label}: {path}\n"));
    }
    out
}

fn now_ms(started: Instant) -> u64 {
    started.elapsed().as_millis() as u64
}

/// Whitespace-insensitive, case-insensitive form of a text for completion checks.
fn normalize_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
}

fn json_matches(actual: &Value, expect: &Value) -> Result<(), String> {
    let Value::Object(expected) = expect else {
        return if actual == expect { Ok(()) } else { Err(format!("expected {expect}, got {actual}")) };
    };
    for (key, want) in expected {
        let got = key
            .split('.')
            .try_fold(actual, |node, part| node.get(part))
            .ok_or_else(|| format!("{key} missing in result"))?;
        let ok = match (got, want) {
            (Value::Number(a), Value::Number(b)) => match (a.as_f64(), b.as_f64()) {
                (Some(x), Some(y)) => (x - y).abs() <= 1e-6 * y.abs().max(1.0),
                _ => a == b,
            },
            // A text expectation is a substring test: a file's content or a window's text is checked for the
            // part that matters, not reproduced in full (line endings and surrounding text differ anyway).
            (Value::String(a), Value::String(b)) => a == b || (!b.is_empty() && normalize_text(a).contains(&normalize_text(b))),
            (Value::Array(items), Value::String(b)) => items.iter().any(|i| i.as_str().is_some_and(|a| normalize_text(a).contains(&normalize_text(b)))),
            (a, b) => a == b,
        };
        if !ok {
            return Err(format!("{key} = {got}, expected {want}"));
        }
    }
    Ok(())
}

impl<'a> TaskRunner<'a> {
    pub fn new(rt: &'a mut Runtime, ui: &'a dyn Interaction, record: &TaskRecord) -> Result<Self> {
        let cancel = CancelToken::new(rt.store.dir(), &record.task_id);
        let audit = AuditLog::open(&rt.config.logs_dir().join("tasks"), &record.task_id, rt.redactor.clone())?;
        let backoff = Backoff::from_config(&rt.config.retry);
        Ok(Self {
            rt,
            ui,
            cancel,
            audit,
            backoff,
            notices: Vec::new(),
            latest_observation: None,
            latest_screenshots: Vec::new(),
            pending_failures: Vec::new(),
            environment: String::new(),
            completion_rejections: 0,
            loaded_groups: groups_for(&format!("{} {} {}", record.goal, record.conversation.join(" "),
                                               record.attachments.iter().map(|a| a.name.as_str()).collect::<Vec<_>>().join(" "))),
            banner: None,
        })
    }

    pub fn cancel_token(&self) -> CancelToken {
        self.cancel.clone()
    }

    fn save(&self, record: &TaskRecord) -> Result<()> {
        self.rt.store.save(record)
    }

    fn set_status(&self, record: &mut TaskRecord, status: TaskStatus, reason: Option<&str>) -> Result<()> {
        record.transition(status)?;
        self.audit.status(status, reason)?;
        self.save(record)?;
        self.ui.event(RunEvent::Status(status, reason));
        Ok(())
    }

    fn finish(&self, record: &mut TaskRecord, status: TaskStatus, reason: &str) -> Result<()> {
        if status == TaskStatus::Failed {
            record.failure_reason = Some(reason.to_string());
        }
        self.set_status(record, status, Some(reason))?;
        self.rt.memory.record_task(&record.task_id, &record.goal, status.as_str(), record.final_summary.as_deref().or(Some(reason)))?;
        Ok(())
    }

    /// Run (or resume) a task until it reaches a terminal state or needs the user.
    pub async fn run(&mut self, record: &mut TaskRecord) -> Result<()> {
        record.runner_pid = Some(std::process::id());
        if record.steps.is_empty() && record.status == TaskStatus::Pending {
            self.audit.task_started(record)?;
            self.ui.event(RunEvent::Started(record));
        }
        if let Some(last) = record.steps.last() {
            self.latest_observation = Some(last.observation.clone()).filter(|o| !o.is_empty());
        }
        self.cancel.clear().context("clearing stale cancel marker")?;
        if record.status == TaskStatus::Pending {
            self.set_status(record, TaskStatus::Planning, None)?;
        }
        let started = Instant::now();
        let budget = Duration::from_millis(self.rt.config.agent.task_timeout_ms);
        let result = self.run_loop(record, started, budget).await;
        if let Err(err) = &result {
            if !record.status.is_terminal() {
                let reason = format!("internal error: {err:#}");
                self.finish(record, TaskStatus::Failed, &reason)?;
            }
        }
        result
    }

    async fn run_loop(&mut self, record: &mut TaskRecord, started: Instant, budget: Duration) -> Result<()> {
        let max_steps = self.rt.config.agent.max_steps;
        // Shifted forward by time spent paused, so pauses do not consume the task's time budget.
        let mut started = started;
        loop {
            if self.cancel.is_cancelled() {
                return self.finish(record, TaskStatus::Cancelled, "cancelled by the user");
            }
            if record.steps.len() as u32 >= max_steps {
                return self.finish(record, TaskStatus::Failed, &format!("step budget of {max_steps} exhausted"));
            }
            if started.elapsed() >= budget {
                return self.finish(record, TaskStatus::Failed, "task time budget exhausted");
            }
            if record.consecutive_failures >= self.rt.config.agent.max_consecutive_failures {
                let last = record.steps.last().and_then(|s| s.error.as_ref()).map(|e| e.to_string()).unwrap_or_default();
                return self.finish(record, TaskStatus::Failed, &format!("{} consecutive failed steps; last: {last}", record.consecutive_failures));
            }
            if record.status != TaskStatus::Planning {
                self.set_status(record, TaskStatus::Planning, None)?;
            }
            if self.cancel.is_paused() {
                self.set_status(record, TaskStatus::Paused, Some("paused by the user"))?;
                let paused_at = Instant::now();
                while self.cancel.is_paused() && !self.cancel.is_cancelled() {
                    tokio::time::sleep(crate::cancel::POLL_INTERVAL).await;
                }
                if self.cancel.is_cancelled() {
                    return self.finish(record, TaskStatus::Cancelled, "cancelled while paused");
                }
                started += paused_at.elapsed();
                self.set_status(record, TaskStatus::Planning, Some("unpaused"))?;
            }

            let Some(actions) = self.plan_next(record).await? else { continue };
            // A reply may hold several independent actions; they run in order and the batch stops at the
            // first one that does not succeed, so the planner always sees that failure next.
            // Set once this batch ran a read-only step: its results were not visible when the reply was written.
            let mut read_in_batch = false;
            for action in actions {
                if self.cancel.is_cancelled() || record.steps.len() as u32 >= max_steps {
                    break;
                }
                // The previous action of the batch may have left the task Observing/Executing.
                if record.status != TaskStatus::Planning {
                    self.set_status(record, TaskStatus::Planning, None)?;
                }
                if let Some(plan) = &action.plan {
                    record.plan = plan.clone();
                    self.audit.record(AuditEvent { kind: "plan", detail: json!({"plan": plan}), ..Default::default() })?;
                    self.ui.event(RunEvent::Plan(&record.plan));
                }
                let (_, step_id) = record.next_step_id();
                self.audit.record(AuditEvent {
                    kind: "step_started",
                    step_id: Some(step_id.clone()),
                    tool: Some(action.tool.clone()),
                    detail: json!({"thought": action.thought, "args": action.args}),
                    ..Default::default()
                })?;
                self.ui.event(RunEvent::Thinking { step_id: &step_id, thought: &action.thought, tool: &action.tool, args: &action.args });

                match action.tool.as_str() {
                    "task.complete" if read_in_batch => {
                        // The summary was written before the results of this batch's reads were seen.
                        self.notices.push(
                            "Your task.complete came in the same reply as the steps that fetch the answer, so it was \
                             written before you saw their results. Read the results above, then complete with the real answer."
                                .into(),
                        );
                        break;
                    }
                    "task.complete" => {
                        if self.verify_completion(record, &action).await? {
                            return Ok(());
                        }
                        break;
                    }
                    "tools.enable" => {
                        let group = action.args.get("category").and_then(Value::as_str).unwrap_or_default().trim().to_lowercase();
                        let served: Vec<String> = self.rt.worker().await?.tools().to_vec();
                        let known = self.rt.catalog.categories(&served);
                        if known.contains(&group) {
                            if !self.loaded_groups.contains(&group) {
                                self.loaded_groups.push(group.clone());
                            }
                            self.notices.push(format!("The {group} tools are now listed under Tools; use them."));
                        } else {
                            self.notices.push(format!("tools.enable: no tool group {group:?}; groups: {}", known.join(", ")));
                        }
                        break;
                    }
                    "task.fail" => {
                        let reason = action.args.get("reason").and_then(Value::as_str).unwrap_or("planner gave up").to_string();
                        record.final_summary = Some(reason.clone());
                        return self.finish(record, TaskStatus::Failed, &reason);
                    }
                    "task.ask_user" => {
                        self.banner = None; // the person has to answer: hands back on
                        let question = action.args.get("question").and_then(Value::as_str).unwrap_or_default().to_string();
                        record.pending_question = Some(question.clone());
                        self.set_status(record, TaskStatus::NeedsUser, Some(&question))?;
                        match self.ui.ask(&question) {
                            Some(answer) => {
                                record.conversation.push(format!("You asked the user: {question}"));
                                record.conversation.push(format!("The user answered: {answer}"));
                                record.pending_question = None;
                                self.set_status(record, TaskStatus::Planning, None)?;
                                break;
                            }
                            None => return Ok(()), // `agent resume <id> --answer ...` continues after the user answers
                        }
                    }
                    _ => {
                        let outcome = self.execute_step(record, &action).await?;
                        let succeeded = outcome.record.status == StepStatus::Succeeded;
                        let surprise = outcome.surprise.clone();
                        if self.rt.catalog.get(&action.tool).is_some_and(|spec| !spec.changes_state()) {
                            read_in_batch = true;
                        }
                        self.after_step(record, outcome)?;
                        if record.status.is_terminal() {
                            return Ok(());
                        }
                        if !succeeded {
                            break;
                        }
                        if let Some(why) = surprise {
                            // The rest of the batch was planned for a screen that no longer exists.
                            self.notices.push(format!("The remaining actions of your reply were not run because {why}. Look at the latest observation and deal with that first."));
                            break;
                        }
                    }
                }
            }
        }
    }

    /// Native declarations of the offered tools plus the task.* / tools.enable core tools.
    fn tool_declarations(&self, offered: &[String]) -> Vec<Value> {
        let core = ["task.complete", "task.fail", "task.ask_user", "tools.enable"];
        offered
            .iter()
            .map(String::as_str)
            .chain(core.into_iter().filter(|c| !offered.iter().any(|o| o == c)))
            .filter_map(|name| self.rt.catalog.get(name))
            .map(|spec| json!({"name": spec.name, "description": spec.description, "parameters": spec.input_schema}))
            .collect()
    }

    /// Ask the planner for the next action. Returns None when the reply was unusable (counted as a failure).
    async fn plan_next(&mut self, record: &mut TaskRecord) -> Result<Option<Vec<PlannerAction>>> {
        let agent_cfg = self.rt.config.agent.clone();
        let llm_cfg = self.rt.config.llm.clone();
        let max_recalled = self.rt.config.memory.max_recalled_items;
        let available: Vec<String> = self.rt.worker().await?.tools().to_vec();
        if self.environment.is_empty() {
            self.environment = describe_computer();
        }
        // Steps already taken with a group's tools keep it loaded (a resumed task, a fallback brain).
        for step in &record.steps {
            let group = self.rt.catalog.get(&step.tool).map(|s| s.category.clone()).unwrap_or_default();
            if !group.is_empty() && !self.loaded_groups.contains(&group) {
                self.loaded_groups.push(group);
            }
        }
        let catalog = self.rt.catalog.render_for_prompt(&available, Some(&self.loaded_groups));
        let system = system_prompt(&catalog, self.rt.policy.mode(), &agent_cfg.language, &self.environment);
        let offered: Vec<String> = available
            .iter()
            .filter(|t| self.rt.catalog.get(t).is_some_and(|s| self.loaded_groups.contains(&s.category)))
            .cloned()
            .collect();
        let now = chrono::Local::now().format("%A %Y-%m-%d %H:%M (UTC%:z)").to_string();
        let recall = self.rt.memory.recall(&record.goal, max_recalled)?.render();
        let mut attachments = Vec::new();
        for image in record.attachments.iter().filter(|a| a.is_image()).take(MAX_USER_IMAGES_PER_TURN) {
            let path = PathBuf::from(&image.path);
            if !path.extension().and_then(|e| e.to_str()).is_some_and(|e| PLANNER_IMAGE_EXTENSIONS.contains(&e.to_ascii_lowercase().as_str())) {
                continue; // BMP/TIFF/HEIC: the brains cannot take them; document.read describes them instead
            }
            match self.rt.llm.upload(&path).await {
                Ok(id) => attachments.push(id),
                Err(err) => self.notices.push(format!("The attached image {} could not be sent to you ({err}); read it with document.read instead.", image.name)),
            }
        }
        for path in self.latest_screenshots.iter().rev().take(agent_cfg.history_images) {
            match self.rt.llm.upload(path).await {
                Ok(id) => attachments.push(id),
                Err(err) => self.notices.push(format!("The screenshot {} could not be sent to you ({err}); rely on vision.locate.", path.display())),
            }
        }
        for attempt in 0..=agent_cfg.max_parse_retries {
            let turn = user_turn(&TurnContext {
                goal: &record.goal,
                plan: &record.plan,
                steps: &record.steps,
                history_steps: agent_cfg.history_steps,
                max_steps: agent_cfg.max_steps,
                consecutive_failures: record.consecutive_failures,
                recall: &recall,
                latest_observation: self.latest_observation.as_deref(),
                notices: &self.notices,
                now: &now,
                conversation: &record.conversation,
                attachments: &record.attachments,
            });
            let mut user = ChatMessage::text("user", turn);
            user.content.extend(attachments.iter().map(|id| Part::Image { attachment_id: id.clone() }));
            let request = ChatRequest {
                messages: vec![ChatMessage::text("system", system.clone()), user],
                model: llm_cfg.model.clone(),
                timeout: Duration::from_millis(llm_cfg.request_timeout_ms),
                metadata: HashMap::from([("task_id".into(), record.task_id.clone())]),
                json: true,
                schema: Some(planner_reply_schema(&offered)),
                tools: Some(self.tool_declarations(&offered)),
            };
            let reply = self.chat_with_retry(record, request).await?;
            match parse_reply(&reply) {
                Ok(mut actions) => {
                    for action in &mut actions {
                        self.rt.catalog.coerce_args(&action.tool, &mut action.args);
                    }
                    self.notices.clear();
                    self.latest_screenshots.clear();
                    return Ok(Some(actions));
                }
                Err(problem) => {
                    self.notices.push(format!(
                        "Your previous reply was unusable ({problem}). Reply with exactly one JSON object as specified."
                    ));
                    self.audit.record(AuditEvent {
                        kind: "planner_reply_rejected",
                        detail: json!({"attempt": attempt, "problem": problem, "reply": reply.chars().take(2000).collect::<String>()}),
                        ..Default::default()
                    })?;
                }
            }
        }
        record.consecutive_failures += 1;
        self.save(record)?;
        Ok(None)
    }

    async fn chat_with_retry(&mut self, record: &mut TaskRecord, request: ChatRequest) -> Result<String> {
        let mut attempt = 0;
        loop {
            attempt += 1;
            record.llm_calls += 1;
            let started = Instant::now();
            let result = tokio::select! {
                r = self.rt.llm.chat(request.clone()) => r,
                _ = self.cancel.cancelled() => return Err(anyhow!("cancelled while waiting for the planner")),
            };
            match result {
                Ok(reply) => {
                    self.audit.record(AuditEvent {
                        kind: "llm",
                        provider: Some(format!("{}:{}", self.rt.llm.name(), reply.backend)),
                        duration_ms: Some(now_ms(started)),
                        retry_count: Some(attempt - 1),
                        detail: json!({"model": reply.model, "attempts": reply.attempts}),
                        ..Default::default()
                    })?;
                    return Ok(reply.content);
                }
                Err(err) => {
                    self.audit.record(AuditEvent {
                        kind: "llm_error",
                        provider: Some(self.rt.llm.name().to_string()),
                        duration_ms: Some(now_ms(started)),
                        retry_count: Some(attempt - 1),
                        error: Some(json!(err.to_string())),
                        ..Default::default()
                    })?;
                    // Brain outages (Gemini "high demand", Google throttling) usually pass within a minute or
                    // two: wait it out with growing pauses rather than failing work that is half done.
                    if !err.retryable() || attempt as usize > LLM_RETRY_DELAYS.len() {
                        return Err(anyhow!("planner unavailable: {err}"));
                    }
                    let delay = LLM_RETRY_DELAYS[attempt as usize - 1];
                    self.ui.event(RunEvent::Notice(&format!("LLM unavailable ({err}); retrying in {delay:?}")));
                    tokio::time::sleep(delay).await;
                }
            }
        }
    }

    fn new_step(&self, record: &TaskRecord, action: &PlannerAction, args: &Value) -> StepRecord {
        let (index, step_id) = record.next_step_id();
        StepRecord {
            step_id,
            index,
            started_at: Utc::now(),
            finished_at: None,
            thought: self.rt.redactor.redact(&action.thought),
            tool: action.tool.clone(),
            args: self.rt.redactor.redact_value(args),
            input_hash: input_hash(&action.tool, args),
            status: StepStatus::Running,
            verification: None,
            observation: String::new(),
            error: None,
            retry_count: 0,
            duration_ms: 0,
            backend: String::new(),
            provider: "worker".into(),
            artifacts: Vec::new(),
        }
    }

    fn rejected(&self, mut step: StepRecord, status: StepStatus, error: ToolErrorInfo) -> StepOutcome {
        step.status = status;
        step.error = Some(error);
        step.finished_at = Some(Utc::now());
        StepOutcome { record: step, observation_for_planner: None, screenshot: None, surprise: None }
    }

    async fn execute_step(&mut self, record: &mut TaskRecord, action: &PlannerAction) -> Result<StepOutcome> {
        let step = self.new_step(record, action, &action.args);
        let Some(spec) = self.rt.catalog.get(&action.tool).cloned() else {
            let err = ToolErrorInfo::new(ErrorCode::UnknownTool, format!("{} is not a tool", action.tool), false);
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        };
        let worker = self.rt.worker().await?;
        if !worker.tools().iter().any(|t| t == &spec.name) {
            let err = ToolErrorInfo::new(ErrorCode::UnknownTool, format!("{} is not available in this installation", spec.name), false);
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        }
        let violations = self.rt.catalog.validate_args(&spec.name, &action.args);
        if !violations.is_empty() {
            let err = ToolErrorInfo::new(ErrorCode::InvalidArgument, violations.join("; "), false);
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        }
        // Bounded repair: an exact call that already failed for a deterministic reason (a script error, a
        // missing file) fails the same way again; the planner has to change something.
        let failed_before = record.steps.iter().find(|s| {
            s.input_hash == step.input_hash
                && s.status == StepStatus::Failed
                && !s.error.as_ref().is_some_and(|e| matches!(e.code, ErrorCode::Timeout | ErrorCode::Busy | ErrorCode::BackendUnavailable))
        });
        if let Some(previous) = failed_before {
            let why = previous.error.as_ref().map(|e| e.message.clone()).unwrap_or_default();
            let err = ToolErrorInfo::new(
                ErrorCode::Conflict,
                format!("this exact call already failed in {} ({why}); fix the cause and change the call instead of repeating it", previous.step_id),
                false,
            );
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        }
        let identical = record.steps.iter().rev().take_while(|s| s.input_hash == step.input_hash).count() as u32;
        if identical >= self.rt.config.agent.max_identical_actions {
            let err = ToolErrorInfo::new(
                ErrorCode::Conflict,
                format!("this exact call was already made {identical} times in a row; change your approach"),
                false,
            );
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        }
        let decision = self.rt.policy.evaluate(&spec, &action.args);
        match decision.verdict {
            Verdict::Deny => {
                let err = ToolErrorInfo::new(ErrorCode::PolicyDenied, format!("{}. This is a firm refusal by the security policy: do not look for another way to the same data (other commands, other folders, searching for similar files). Tell the user it is not allowed and why, with task.complete.", decision.reasons.join("; ")), false);
                return Ok(self.rejected(step, StepStatus::Denied, err));
            }
            Verdict::Confirm => {
                if !self.ui.confirm(&spec.name, &decision.args, &decision.reasons) {
                    let err = ToolErrorInfo::new(ErrorCode::PolicyDenied, format!("the user declined: {}", decision.reasons.join("; ")), false);
                    return Ok(self.rejected(step, StepStatus::Denied, err));
                }
            }
            Verdict::Allow => {}
        }
        self.set_status(record, TaskStatus::Executing, None)?;
        if self.banner.is_none() && HANDS_ON_TOOLS.contains(&spec.name.as_str()) {
            self.banner = crate::banner::Banner::show(BANNER_TEXT);
        }
        self.run_tool(record, &spec, step, decision.args).await
    }

    async fn run_tool(&mut self, record: &mut TaskRecord, spec: &ToolSpec, mut step: StepRecord, args: Value) -> Result<StepOutcome> {
        let started = Instant::now();
        let budget_left = Duration::from_millis(self.rt.config.agent.task_timeout_ms).saturating_sub(started.elapsed());
        let timeout = Duration::from_millis(spec.timeout_ms).min(budget_left).max(MIN_TOOL_TIMEOUT);
        let mut attempt = 0u32;
        let response = loop {
            attempt += 1;
            let worker = self.rt.worker().await?;
            let call = ToolCall::new(&record.task_id, &step.step_id, &spec.name, args.clone(), timeout);
            let result = tokio::select! {
                r = worker.call(&call) => r,
                _ = self.cancel.cancelled() => {
                    // The worker stops the call (shell process trees are killed); its CANCELLED reply is not awaited.
                    if let Err(err) = worker.cancel(&call.request_id).await {
                        self.ui.event(RunEvent::Notice(&format!("could not deliver cancellation to the worker: {err}")));
                    }
                    step.status = StepStatus::Cancelled;
                    step.error = Some(ToolErrorInfo::new(ErrorCode::Cancelled, "cancelled by the user", false));
                    step.duration_ms = now_ms(started);
                    step.finished_at = Some(Utc::now());
                    return Ok(StepOutcome { record: step, observation_for_planner: None, screenshot: None, surprise: None });
                }
            };
            let failure = match result {
                Ok(response) => {
                    let retry = !response.is_ok()
                        && spec.idempotent
                        && response.error.as_ref().is_some_and(|e| e.retryable)
                        && attempt < self.backoff.max_attempts;
                    if !retry {
                        break response;
                    }
                    response.error.map(|e| e.to_string()).unwrap_or_default()
                }
                Err(err @ (WorkerError::Died | WorkerError::Hung(_))) => {
                    record.worker_restarts += 1;
                    self.audit.record(AuditEvent { kind: "worker_restart", tool: Some(spec.name.clone()), error: Some(json!(err.to_string())), ..Default::default() })?;
                    if record.worker_restarts > self.rt.config.worker.max_restarts {
                        return Err(anyhow!("worker failed {} times; giving up: {err}", record.worker_restarts));
                    }
                    self.ui.event(RunEvent::Notice(&format!("worker {err}; restarting it")));
                    self.rt.restart_worker().await?;
                    // A call that hung will most likely hang again (a browser that does not start, a dialog
                    // blocking the app): report it at once so the planner changes approach, instead of
                    // spending another full timeout on the same call.
                    let hung = matches!(err, WorkerError::Hung(_));
                    if hung || !spec.idempotent || attempt >= self.backoff.max_attempts {
                        // A non-idempotent action may or may not have happened: report it instead of repeating it.
                        break ToolResponse {
                            request_id: call.request_id,
                            status: "error".into(),
                            result: None,
                            error: Some(ToolErrorInfo::new(
                                ErrorCode::BackendUnavailable,
                                format!("the worker {err} during this action; it was restarted. Observe the state before retrying"),
                                true,
                            )),
                            metrics: Default::default(),
                        };
                    }
                    err.to_string()
                }
                Err(err) => return Err(err.into()),
            };
            step.retry_count = attempt;
            let delay = self.backoff.delay(attempt);
            self.ui.event(RunEvent::Notice(&format!("{} failed ({failure}); retry {attempt} in {delay:?}", spec.name)));
            tokio::time::sleep(delay).await;
        };

        step.duration_ms = now_ms(started);
        step.finished_at = Some(Utc::now());
        step.backend = response.metrics.backend.clone();
        let mut screenshot = None;
        match (&response.result, &response.error) {
            (Some(result), _) if response.is_ok() => {
                step.verification = Some(result.verification.status.clone());
                step.observation = self.rt.redactor.redact(&result.observation);
                for artifact in &result.artifacts {
                    if let Some(path) = artifact.get("path").and_then(Value::as_str) {
                        step.artifacts.push(path.to_string());
                        if artifact.get("kind").and_then(Value::as_str) == Some("screenshot") {
                            screenshot = Some(PathBuf::from(path));
                        }
                    }
                }
                step.status = if result.verification.status == "failed" { StepStatus::Failed } else { StepStatus::Succeeded };
                if step.status == StepStatus::Failed {
                    let failed: Vec<String> = result.verification.checks.iter()
                        .filter(|c| c.get("passed") == Some(&Value::Bool(false)))
                        .map(|c| format!("{}: {}", c["name"].as_str().unwrap_or("check"), c["detail"].as_str().unwrap_or("")))
                        .collect();
                    step.error = Some(ToolErrorInfo::new(ErrorCode::VerificationFailed, format!("post-condition failed: {}", failed.join("; ")), true));
                }
            }
            (_, Some(error)) => {
                step.status = StepStatus::Failed;
                step.error = Some(ToolErrorInfo { message: self.rt.redactor.redact(&error.message), ..error.clone() });
                if error.code == ErrorCode::ElevationRequired {
                    record.escalation = error.details.get("escalation_request").cloned().or(Some(error.details.clone()));
                }
            }
            _ => {
                step.status = StepStatus::Failed;
                step.error = Some(ToolErrorInfo::new(ErrorCode::Internal, "worker response had neither result nor error", false));
            }
        }

        // Look at the screen after every action that touches an application (also the self-verified ones:
        // the planner should see the window it just opened or the state after a save), after anything
        // unverified, and after a failed desktop/browser step.
        let hands_on = spec.category == "desktop" && HANDS_ON_TOOLS.contains(&spec.name.as_str());
        let needs_observation = matches!(step.verification.as_deref(), Some("deferred"))
            || hands_on
            || step.status == StepStatus::Failed && (spec.category == "desktop" || spec.category == "browser");
        let mut surprise = None;
        let observation = if needs_observation && !self.cancel.is_cancelled() {
            self.set_status(record, if step.status == StepStatus::Failed { TaskStatus::Recovering } else { TaskStatus::Observing }, None)?;
            match self.observe(record, spec).await {
                Some(seen) => {
                    if screenshot.is_none() {
                        screenshot = seen.screenshot;
                    }
                    surprise = seen.surprise;
                    Some(seen.text)
                }
                None => None,
            }
        } else {
            None
        };
        Ok(StepOutcome { record: step, observation_for_planner: observation, screenshot, surprise })
    }

    /// OBSERVE: capture the current UI so the planner can judge an unverified or failed action. Desktop work
    /// uses desktop.observe (working window, focus, text, controls, screenshot, and whether a dialog opened or
    /// the window closed); browser work reads the page state. The result is written to the audit log.
    async fn observe(&mut self, record: &TaskRecord, spec: &ToolSpec) -> Option<Observation> {
        let worker = self.rt.worker().await.ok()?;
        let has = |name: &str| worker.tools().iter().any(|t| t == name);
        let (tool, args) = if spec.category == "browser" {
            ("browser.get_state", json!({}))
        } else if has("desktop.observe") {
            ("desktop.observe", json!({"max_elements": OBSERVE_MAX_ELEMENTS}))
        } else {
            ("desktop.inspect_ui", json!({"max_elements": OBSERVE_MAX_ELEMENTS}))
        };
        if !has(tool) {
            return None;
        }
        let call = ToolCall::new(&record.task_id, "observe", tool, args, OBSERVE_TIMEOUT);
        let seen = match worker.call(&call).await {
            Ok(response) if response.is_ok() => {
                let result = response.result?;
                let screenshot = result.artifacts.iter()
                    .filter(|a| a.get("kind").and_then(Value::as_str) == Some("screenshot"))
                    .find_map(|a| a.get("path").and_then(Value::as_str).map(PathBuf::from));
                let surprise = result.data.get("surprise").and_then(Value::as_str).filter(|s| !s.is_empty()).map(String::from);
                Observation { text: format!("(automatic {tool}) {}", result.observation), screenshot, surprise }
            }
            Ok(response) => Observation { text: format!("(automatic {tool} failed: {})", response.error.map(|e| e.to_string()).unwrap_or_default()), screenshot: None, surprise: None },
            Err(err) => Observation { text: format!("(automatic {tool} failed: {err})"), screenshot: None, surprise: None },
        };
        let _ = self.audit.record(AuditEvent {
            kind: "observe",
            tool: Some(tool.into()),
            detail: json!({"text": clip_text(&seen.text, 6000), "screenshot": seen.screenshot, "surprise": seen.surprise}),
            ..Default::default()
        });
        Some(seen)
    }

    fn after_step(&mut self, record: &mut TaskRecord, outcome: StepOutcome) -> Result<()> {
        let step = outcome.record;
        self.audit.step(&step)?;
        // A long run of lookups with nothing produced is a loop, not research: say so, in the planner's own terms.
        let is_lookup = |t: &str| t.starts_with("knowledge.") || t.starts_with("web.") || t == "document.read";
        if is_lookup(&step.tool) {
            let streak = 1 + record.steps.iter().rev().take_while(|s| is_lookup(&s.tool)).count();
            if streak == LOOKUP_STREAK_WARN || streak == 2 * LOOKUP_STREAK_WARN {
                self.notices.push(format!(
                    "You have made {streak} lookups in a row. Stop searching: what you have found is all there is. Do the task with it now:                      write the answer, using a neutral wording or a placeholder for what was not found, and say plainly what you could not find.                      Do not look up names or details the user did not ask you to find."));
            }
        }
        match step.status {
            StepStatus::Succeeded => {
                record.consecutive_failures = 0;
                let recovered: Vec<(String, ErrorCode, String)> = self.pending_failures.iter().filter(|(t, _, _)| *t == step.tool).cloned().collect();
                for (tool, code, context) in recovered {
                    let recovery = format!("succeeded at {} after re-observing", step.step_id);
                    self.rt.memory.record_failure(&tool, code.as_str(), &context, Some(&recovery))?;
                }
                self.pending_failures.retain(|(t, _, _)| *t != step.tool);
            }
            StepStatus::Cancelled => {}
            _ => {
                record.consecutive_failures += 1;
                if let Some(error) = &step.error {
                    let context: String = format!("{} | {}", record.goal, error.message).chars().take(200).collect();
                    self.rt.memory.record_failure(&step.tool, error.code.as_str(), &context, None)?;
                    self.pending_failures.push((step.tool.clone(), error.code, context));
                }
            }
        }
        record.artifacts.extend(step.artifacts.iter().cloned());
        if let Some(path) = outcome.screenshot {
            self.latest_screenshots.push(path);
        }
        self.latest_observation = outcome.observation_for_planner.or_else(|| Some(step.observation.clone()).filter(|o| !o.is_empty()));
        self.ui.event(RunEvent::StepDone(&step));
        record.steps.push(step);
        if self.cancel.is_cancelled() {
            return self.finish(record, TaskStatus::Cancelled, "cancelled by the user");
        }
        self.save(record)
    }

    /// Second opinion on a completion whose checks passed: a separate LLM call in a strict verifier role
    /// judges whether the evidence actually proves the user's goal (generic checks such as "the URL
    /// contains github.com" do not prove "the Windows-Use repository page is open").
    /// Returns Ok(Ok(())) to accept, Ok(Err(reason)) to reject.
    async fn llm_verdict(&mut self, record: &mut TaskRecord, summary: &str, checks: &[Value]) -> Result<Result<(), String>> {
        let recent: Vec<Value> = record
            .steps
            .iter()
            .rev()
            .take(VERIFIER_RECENT_STEPS)
            .rev()
            .map(|s| json!({"step": s.step_id, "tool": s.tool, "args": s.args, "status": s.status,
                            "verification": s.verification, "observation": clip_text(&s.observation, VERIFIER_OBSERVATION_CHARS),
                            "error": s.error.as_ref().map(|e| e.to_string())}))
            .collect();
        let evidence = json!({"goal": record.goal, "claimed_summary": summary, "completion_checks": checks, "recent_steps": recent});
        let request = ChatRequest {
            messages: vec![
                ChatMessage::text("system", VERIFIER_PROMPT),
                ChatMessage::text("user", serde_json::to_string_pretty(&evidence)?),
            ],
            model: self.rt.config.llm.model.clone(),
            timeout: Duration::from_millis(self.rt.config.llm.request_timeout_ms),
            metadata: HashMap::from([("task_id".into(), record.task_id.clone()), ("role".into(), "verifier".into())]),
            json: true,
            schema: None,
            tools: None,
        };
        let reply = self.chat_with_retry(record, request).await?;
        let verdict = crate::planner::extract_json_value(&reply);
        let decision = verdict.as_ref().and_then(|v| v.get("verdict")).and_then(Value::as_str).unwrap_or("");
        let reason = verdict
            .as_ref()
            .and_then(|v| v.get("reason"))
            .and_then(Value::as_str)
            .unwrap_or("no reason given")
            .to_string();
        self.audit.record(AuditEvent {
            kind: "completion_verifier",
            verification_result: Some(decision.to_string()),
            detail: json!({"reply": clip_text(&reply, VERIFIER_OBSERVATION_CHARS)}),
            ..Default::default()
        })?;
        Ok(match decision {
            "accept" => Ok(()),
            "reject" => Err(reason),
            _ => Err(format!("verifier reply was not a verdict: {}", clip_text(&reply, 200))),
        })
    }

    /// VERIFY: run the planner's completion checks; complete only if all pass.
    async fn verify_completion(&mut self, record: &mut TaskRecord, action: &PlannerAction) -> Result<bool> {
        let normalized = PlannerAction { args: normalize_completion(&action.args), ..action.clone() };
        let action = &normalized;
        let violations = self.rt.catalog.validate_args("task.complete", &action.args);
        if !violations.is_empty() {
            self.notices.push(format!("task.complete arguments are invalid: {}", violations.join("; ")));
            // Counted like a rejected completion, not as a failed step: finished work must not be
            // thrown away because the closing call was malformed.
            self.completion_rejections += 1;
            if self.completion_rejections > MAX_COMPLETION_REJECTIONS + 1 {
                record.consecutive_failures += 1;
            }
            return Ok(false);
        }
        self.set_status(record, TaskStatus::Verifying, None)?;
        let checks = action.args["checks"].as_array().cloned().unwrap_or_default();
        let mut failures = Vec::new();
        let mut evidence = Vec::new();
        let mut check_results: Vec<Value> = Vec::new();
        let state_changing: Vec<String> = record
            .steps
            .iter()
            .filter(|s| s.status == StepStatus::Succeeded)
            .filter(|s| self.rt.catalog.get(&s.tool).is_some_and(|spec| spec.changes_state()))
            // A change the tool verified itself by reading it back (files, AutoCAD entities, whole drawings)
            // is already proven; only unconfirmed effects (clicks, typing, shell commands) need checks.
            .filter(|s| s.verification.as_deref() != Some("passed"))
            .map(|s| format!("{} {}", s.step_id, s.tool))
            .collect();
        if checks.is_empty() && !state_changing.is_empty() {
            failures.push(format!(
                "this task changed state ({}); evidence_steps are not enough - give read-only checks that prove the result",
                state_changing.join(", ")
            ));
        }
        for step_id in action.args["evidence_steps"].as_array().cloned().unwrap_or_default() {
            let id = step_id.as_str().unwrap_or_default();
            match record.steps.iter().find(|s| s.step_id == id) {
                None => failures.push(format!("evidence step {id} does not exist")),
                Some(step) if step.status != StepStatus::Succeeded => {
                    failures.push(format!("evidence step {id} did not succeed ({:?})", step.status))
                }
                Some(step) if !matches!(step.verification.as_deref(), Some("passed" | "not_applicable")) => failures.push(
                    format!("evidence step {id} is not verified (verification {})", step.verification.as_deref().unwrap_or("none")),
                ),
                Some(step) => evidence.push(format!("{id} {} succeeded (verification {})", step.tool, step.verification.as_deref().unwrap_or("?"))),
            }
        }
        for (i, check) in checks.iter().enumerate() {
            let tool = check["tool"].as_str().unwrap_or_default();
            let Some(spec) = self.rt.catalog.get(tool).cloned() else {
                failures.push(format!("check {i}: unknown tool {tool}"));
                continue;
            };
            if spec.verification != VerificationMode::None || spec.is_core_tool() {
                failures.push(format!("check {i}: {tool} is not a read-only tool; checks must only observe"));
                continue;
            }
            let mut args = check["args"].clone();
            self.rt.catalog.coerce_args(tool, &mut args);
            let violations = self.rt.catalog.validate_args(tool, &args);
            if !violations.is_empty() {
                failures.push(format!("check {i}: invalid args for {tool}: {}", violations.join("; ")));
                continue;
            }
            let decision = self.rt.policy.evaluate(&spec, &args);
            if decision.verdict == Verdict::Deny {
                failures.push(format!("check {i}: policy denied {tool}"));
                continue;
            }
            let worker = self.rt.worker().await?;
            let call = ToolCall::new(&record.task_id, "verify", tool, decision.args, Duration::from_millis(spec.timeout_ms));
            match worker.call(&call).await {
                Ok(response) if response.is_ok() => {
                    let data = response.result.map(|r| r.data).unwrap_or(Value::Null);
                    check_results.push(json!({"tool": tool, "args": args, "expect": check["expect"],
                                              "actual": clip_json(&data, VERIFIER_DATA_CHARS)}));
                    match json_matches(&data, &check["expect"]) {
                        Ok(()) => evidence.push(format!("{tool} {} -> {}", args, check["expect"])),
                        Err(problem) => failures.push(format!("check {i} ({tool}): {problem}")),
                    }
                }
                Ok(response) => failures.push(format!("check {i} ({tool}) failed: {}", response.error.map(|e| e.to_string()).unwrap_or_default())),
                Err(err) => failures.push(format!("check {i} ({tool}) could not run: {err}")),
            }
        }
        let summary = action.args["summary"].as_str().unwrap_or_default().to_string();
        // A direct answer (greeting, small talk, general knowledge) touched nothing: there is nothing for
        // a second opinion to verify, and asking one would only cost another slow LLM call.
        let direct_answer = record.steps.is_empty() && checks.is_empty();
        // The second opinion exists for UI work (desktop clicks, web pages), where a check can pass
        // without proving the goal. Shell, file, AutoCAD and knowledge results are exact data the core
        // has just re-read itself, so the extra slow LLM call is skipped for them.
        let ui_work = record.steps.iter().any(|s| {
            s.status == StepStatus::Succeeded
                && self.rt.catalog.get(&s.tool).is_some_and(|spec| matches!(spec.category.as_str(), "desktop" | "browser" | "vision"))
        });
        if failures.is_empty() && self.rt.config.agent.verify_completion_with_llm && !direct_answer && ui_work {
            if let Err(rejection) = self.llm_verdict(record, &summary, &check_results).await? {
                failures.push(format!("independent verifier rejected the completion: {rejection}"));
            }
        }
        self.audit.record(AuditEvent {
            kind: "completion_check",
            tool: Some("task.complete".into()),
            verification_result: Some(if failures.is_empty() { "passed".into() } else { "failed".into() }),
            detail: json!({"summary": summary, "evidence": evidence, "failures": failures}),
            ..Default::default()
        })?;
        let did_work = record.steps.iter().any(|s| s.status == StepStatus::Succeeded);
        let accept_anyway = !failures.is_empty() && did_work && self.completion_rejections >= MAX_COMPLETION_REJECTIONS;
        if failures.is_empty() || accept_anyway {
            let summary = if accept_anyway {
                let note = if self.rt.config.agent.language.starts_with("vi") {
                    "Lưu ý: chưa kiểm chứng được đầy đủ kết quả"
                } else {
                    "Note: the result could not be fully verified"
                };
                // The warning leads: the model's own claim ("I drew it") must not be what the person reads first.
                format!("⚠ {note} ({}).\n\n{summary}", clip_text(&failures.join("; "), 400))
            } else {
                summary
            };
            record.final_summary = Some(summary);
            if let Some(artifacts) = action.args.get("artifacts").and_then(Value::as_array) {
                record.artifacts.extend(artifacts.iter().filter_map(Value::as_str).map(String::from));
            }
            let procedure: Vec<String> = record.steps.iter().filter(|s| s.status == StepStatus::Succeeded).map(|s| s.tool.clone()).collect();
            self.rt.memory.record_procedure(&record.goal, &procedure.join(" -> "))?;
            let reason = if accept_anyway { "completed; some completion checks could not be confirmed" } else { "all completion checks passed" };
            self.finish(record, TaskStatus::Completed, reason)?;
            return Ok(true);
        }
        // Rejections are bounded by MAX_COMPLETION_REJECTIONS, not by the consecutive-failure limit, so
        // finished work is not thrown away because its checks were phrased badly.
        self.completion_rejections += 1;
        self.notices.push(format!(
            "task.complete was REJECTED because these checks failed: {}. The task is not done; fix it or adjust the checks to what the user actually asked.",
            failures.join(" | ")
        ));
        self.set_status(record, TaskStatus::Planning, Some("completion checks failed"))?;
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn requests_load_the_tool_groups_they_mention() {
        let g = super::groups_for("Vẽ mặt bằng nhà cấp 4 trong AutoCAD");
        assert!(g.contains(&"autocad".to_string()) && !g.contains(&"browser".to_string()));
        let g = super::groups_for("Mở Notepad, gõ dòng chữ rồi lưu");
        assert!(g.contains(&"desktop".to_string()));
        let g = super::groups_for("vẽ doraemon bằng paint");
        assert!(g.contains(&"paint".to_string()) && !g.contains(&"autocad".to_string()), "{g:?}");
        let g = super::groups_for("vẽ con mèo trong PowerPoint");
        assert!(g.contains(&"art".to_string()) && g.contains(&"office".to_string()), "{g:?}");
        let g = super::groups_for("Ổ C còn trống bao nhiêu GB?");
        assert_eq!(g.len(), super::BASE_GROUPS.len(), "{g:?}");
    }

    use super::*;

    #[test]
    fn malformed_completions_are_normalized() {
        let raw = json!({"summary": "done", "description": "x",
            "checks": {"a": {"tool": "autocad.verify_geometry", "args": {"handle": "80"}, "expect": {"matches": true}}, "b": "junk"},
            "evidence_steps": ["s001", 3]});
        let n = normalize_completion(&raw);
        assert_eq!(n["checks"].as_array().unwrap().len(), 1);
        assert_eq!(n["evidence_steps"], json!(["s001"]));
        assert!(n.get("description").is_none());
        assert_eq!(normalize_completion(&json!({"summary": "hi"})), json!({"summary": "hi"}));
    }
}
