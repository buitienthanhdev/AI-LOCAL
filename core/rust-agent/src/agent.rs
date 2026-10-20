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
use crate::completion;
use crate::config::{Config, ExecutionMode};
use crate::error::{ErrorCode, ToolErrorInfo};
use crate::llm::{ChatMessage, ChatProvider, ChatRequest, Part};
use crate::memory::Memory;
use crate::planner::{
    parse_reply, system_prompt, user_turn, PlannerAction, TurnContext, MAX_ACTIONS_PER_REPLY,
};
use crate::policy::{Policy, Verdict};
use crate::redact::Redactor;
use crate::retry::Backoff;
use crate::secrets::resolve_secret;
use crate::state::{
    StepRecord, StepStatus, TaskRecord, TaskStatus, TaskStore, PLANNER_IMAGE_EXTENSIONS,
};
use crate::tools::{ToolCatalog, ToolSpec};
use crate::worker::{ToolCall, ToolResponse, WorkerClient, WorkerError};

/// Elements requested for the automatic observation after an unverified desktop action.
const OBSERVE_MAX_ELEMENTS: u64 = 80;
const OBSERVE_TIMEOUT: Duration = Duration::from_secs(30);
const MIN_TOOL_TIMEOUT: Duration = Duration::from_secs(1);
/// Characters of each completion check's result data shown to the verifier.
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

/// What the user interface (CLI, future UI) must provide to the runner.
pub trait Interaction: Send + Sync {
    fn event(&self, event: RunEvent<'_>);
    /// Ask for consent to run a call the policy marked "confirm". Returns true to proceed.
    fn confirm(&self, tool: &str, args: &Value, reasons: &[String]) -> bool;
    /// Consent for a call of the "always confirm" classes (guard.rs). Only a person can give it: an interface that
    /// auto-approves (`--yes`) or has nobody to ask must decline. The default asks like `confirm`.
    fn confirm_always(&self, tool: &str, args: &Value, reasons: &[String]) -> bool {
        self.confirm(tool, args, reasons)
    }
    /// Ask the user a question; None when nobody can answer (non-interactive).
    fn ask(&self, question: &str) -> Option<String>;
    /// Ask with choices (`multi`: several may be picked). The default shows the choices as text and takes a typed
    /// answer; the chat window overrides it with a real dialog whose last choice is always "Khác".
    fn ask_choice(&self, question: &str, options: &[String], _multi: bool) -> Option<String> {
        if options.is_empty() {
            return self.ask(question);
        }
        let listed: Vec<String> = options
            .iter()
            .enumerate()
            .map(|(i, o)| format!("{}. {o}", i + 1))
            .collect();
        self.ask(&format!("{question}\n{}", listed.join("\n")))
    }
}

pub enum RunEvent<'a> {
    Started(&'a TaskRecord),
    Status(TaskStatus, Option<&'a str>),
    Plan(&'a [String]),
    Thinking {
        step_id: &'a str,
        thought: &'a str,
        tool: &'a str,
        args: &'a Value,
    },
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
        Ok(Self {
            config,
            catalog,
            policy,
            llm,
            memory,
            store,
            redactor,
            worker: None,
        })
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
    pub async fn call_tool(
        &mut self,
        task_id: &str,
        tool: &str,
        args: Value,
    ) -> Result<ToolResponse> {
        let spec = self
            .catalog
            .get(tool)
            .ok_or_else(|| anyhow!("unknown tool {tool}"))?
            .clone();
        let violations = self.catalog.validate_args(tool, &args);
        if !violations.is_empty() {
            return Err(anyhow!(
                "invalid arguments for {tool}: {}",
                violations.join("; ")
            ));
        }
        let decision = self.policy.evaluate(&spec, &args);
        if decision.verdict == Verdict::Deny {
            return Err(anyhow!(
                "policy denied {tool}: {}",
                decision.reasons.join("; ")
            ));
        }
        let worker = self.worker().await?;
        let call = ToolCall::new(
            task_id,
            "adhoc",
            tool,
            decision.args,
            Duration::from_millis(spec.timeout_ms),
        );
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
    suppressed_asks: u32,
    /// Tool groups shown in full to the planner (the others are listed in one line each until enabled).
    loaded_groups: Vec<String>,
    /// Shown while the agent operates applications with the mouse and keyboard.
    banner: Option<crate::banner::Banner>,
    screen_activity_pause: bool,
    /// Per-call approvals of the always-confirm classes (single use, short lived).
    approvals: crate::guard::Ledger,
}

/// Phrases in an error that mean the environment forbids the action: more variations of the same idea will not help.
const BLOCKED_MARKERS: [&str; 9] = [
    "trust access",
    "access is denied",
    "permission denied",
    "not authorized",
    "requires elevation",
    "administrator",
    "is disabled by",
    "blocked by",
    "programmatic access",
];

/// A step that repeats an earlier step (same tool, same arguments) and got the same observation again: after the
/// third identical result the planner is told the answer will not change and to finish or do something else.
fn repeated_check_notice(previous: &[StepRecord], step: &StepRecord) -> Option<String> {
    if step.status != StepStatus::Succeeded {
        return None;
    }
    let same = previous
        .iter()
        .filter(|p| {
            p.tool == step.tool
                && p.input_hash == step.input_hash
                && p.observation == step.observation
        })
        .count()
        + 1;
    // Every third repeat, not only the first two: a model that ignores the notice twice needs it again, sharper.
    if same >= 3 && same % 3 == 0 {
        let seen: String = step.observation.chars().take(400).collect();
        let push = if same >= 6 {
            format!(
                " This is repeat {same}: stop calling {tool}. The state is exactly this:
{seen}
Compare it with the request word by word.                  If every part is satisfied, your next action MUST be task.complete with what was done. If a part is missing or wrong                  (for example values changed type, a column lost), fix exactly that with a DIFFERENT method.",
                tool = step.tool)
        } else {
            String::new()
        };
        return Some(format!(
            "You have made this exact {} call {same} times and got the same result each time; it will not change. If what you have              verified already satisfies the request, call task.complete now with the result. Otherwise do something different; do not              check again.{push}",
            step.tool));
    }
    None
}

/// What to tell the planner after a failed step so it changes approach instead of repeating itself: a block
/// imposed by the machine (security setting, permission) is reported to the user at once; a second failure in a
/// row asks for a different method with the cause named. None when the failure needs no coaching.
fn failure_coaching(consecutive: u32, step: &StepRecord) -> Option<String> {
    let detail = format!(
        "{} {}",
        step.error
            .as_ref()
            .map(|e| e.message.as_str())
            .unwrap_or(""),
        step.observation
    )
    .to_lowercase();
    if BLOCKED_MARKERS.iter().any(|m| detail.contains(m)) {
        return Some(format!(
            "Step {} failed because the machine blocks it (a security setting or permission), not because of your arguments. Do NOT              try another way round the block and do not change security settings. Tell the user in one short message what is blocked,              what they can switch on, and what you can do instead (task.ask_user), or finish what is possible and say what is left.",
            step.step_id));
    }
    if consecutive == 2 || consecutive == 3 {
        return Some(format!(
            "{consecutive} steps failed in a row. Before the next call, write in your thought: (1) the actual cause of the last error,              (2) why the previous attempt could not work, (3) a DIFFERENT method (another tool or a simpler script). Repeating the same              call with small changes is not allowed; if nothing different is possible, ask the user one short question."));
    }
    None
}

/// The text of the `summary` argument of a `task.complete` action in the planner's JSON reply received so far (the
/// reply may be cut anywhere). None while that part has not started: a reply that runs a tool shows nothing.
pub fn partial_summary(raw: &str) -> Option<String> {
    // The reply may list "args" before "tool": until the tool name is known a summary is taken to be the answer; once a
    // tool other than task.complete is named, nothing is shown.
    if let Some(at) = raw.find("\"tool\"") {
        let named = raw[at + 6..]
            .trim_start()
            .strip_prefix(':')
            .map(|r| r.trim_start());
        if named.is_some_and(|r| r.len() > 12 && !r.starts_with("\"task.complete\"")) {
            return None;
        }
    }
    let key = raw.rfind("\"summary\"")?;
    let rest = &raw[key + "\"summary\"".len()..];
    let rest = rest
        .trim_start()
        .strip_prefix(':')?
        .trim_start()
        .strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => break,
            '\\' => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => {}
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some('/') => out.push('/'),
                Some('u') => {
                    let hex: String = chars.by_ref().take(4).collect();
                    if hex.len() < 4 {
                        break; // cut inside the escape: wait for more
                    }
                    if let Some(ch) = u32::from_str_radix(&hex, 16).ok().and_then(char::from_u32) {
                        out.push(ch);
                    }
                }
                Some(other) => out.push(other),
                None => break,
            },
            c => out.push(c),
        }
    }
    (!out.is_empty()).then_some(out)
}

/// One planner call. With `stream` on, the reply is read as it arrives and the growing answer text is written to
/// `partial` (the chat window shows it while it is written). Anything that goes wrong with the stream falls back to the
/// ordinary call, so streaming can only make an answer appear sooner, never fail a task that would have worked.
async fn streamed_chat(
    llm: &dyn crate::llm::ChatProvider,
    request: ChatRequest,
    partial: &std::path::Path,
    stream: bool,
) -> Result<crate::llm::ChatReply, crate::llm::LlmError> {
    use crate::llm::StreamEvent;
    if !stream {
        return llm.chat(request).await;
    }
    let started = Instant::now();
    let Ok(mut rx) = llm.stream(request.clone()).await else {
        return llm.chat(request).await;
    };
    let mut raw = String::new();
    let (mut backend, mut model, mut attempts) = (String::new(), String::new(), Vec::new());
    let mut last_write = Instant::now() - Duration::from_secs(1);
    let mut last_text = String::new();
    let mut finished = false;
    let (mut deltas, mut first_ms) = (0u32, 0u64);
    while let Some(event) = rx.recv().await {
        match event {
            Ok(StreamEvent::Backend(b)) => backend = b,
            Ok(StreamEvent::Delta(d)) => {
                if deltas == 0 {
                    first_ms = started.elapsed().as_millis() as u64;
                }
                deltas += 1;
                raw.push_str(&d);
                if last_write.elapsed() >= Duration::from_millis(120) {
                    if let Some(text) = partial_summary(&raw) {
                        if text != last_text {
                            let body = serde_json::json!({"text": text, "at": chrono::Utc::now().to_rfc3339()}).to_string();
                            let tmp = partial.with_extension("tmp");
                            // atomic when possible; a reader that holds the file open (no delete sharing) blocks the
                            // rename on Windows, then the file is rewritten in place
                            let renamed = std::fs::write(&tmp, &body).is_ok()
                                && std::fs::rename(&tmp, partial).is_ok();
                            if !renamed {
                                let _ = std::fs::remove_file(&tmp);
                                let _ = std::fs::write(partial, &body);
                            }
                            last_text = text;
                        }
                    }
                    last_write = Instant::now();
                }
            }
            Ok(StreamEvent::Meta {
                model: m,
                attempts: a,
            }) => {
                model = m;
                attempts = a;
            }
            Ok(StreamEvent::Done) => {
                finished = true;
                break;
            }
            Err(_) => break,
        }
    }
    if !finished || raw.trim().is_empty() {
        return llm.chat(request).await;
    }
    Ok(crate::llm::ChatReply {
        content: raw,
        reasoning: None,
        model,
        backend,
        latency_ms: started.elapsed().as_millis() as u64,
        attempts,
        // no token counts from a stream; how it arrived is recorded instead (diagnostics)
        usage: Some(
            serde_json::json!({"streamed": true, "deltas": deltas, "first_delta_ms": first_ms}),
        ),
    })
}

/// A task may ask this many questions; after that the planner is told to decide with defaults.
const MAX_QUESTIONS_PER_TASK: usize = 4;

/// Questions that only end a turn politely ("bạn cần hỗ trợ gì thêm không?", "anything else?").
fn is_closing_question(question: &str, worked: bool) -> bool {
    let q = brain_core::text::norm(question);
    // always a pleasantry
    const ALWAYS: [&str; 8] = [
        "ho tro gi them",
        "giup gi them",
        "gi them khong",
        "gi nua khong",
        "anything else",
        "further assistance",
        "any further",
        "need anything",
    ];
    // a pleasantry once real work has been done ("... or any other action?"), a real question before
    const AFTER_WORK: [&str; 5] = [
        "lam them",
        "thuc hien them",
        "thao tac khac",
        "thao tac nao khac",
        "gui email thong bao",
    ];
    ALWAYS.iter().any(|m| q.contains(m)) || (worked && AFTER_WORK.iter().any(|m| q.contains(m)))
}

/// Asking whether to retry a failed step ("Bạn có muốn tôi thử lại không?").
fn is_retry_permission(question: &str) -> bool {
    let q = brain_core::text::norm(question);
    [
        "muon toi thu lai",
        "co muon thu lai",
        "thu lai khong",
        "thu lai mot lan nua",
        "should i retry",
        "try again",
        "shall i retry",
        "muon toi tiep tuc thu",
    ]
    .iter()
    .any(|m| q.contains(m))
}

/// Choices of a clarification question: trimmed, de-duplicated, without any "other" entry of the model's own (the chat
/// adds the one "Khác" at the end), at most 8. A reply that gives no usable list falls back to free text.
fn clarification_options(args: &Value) -> (Vec<String>, bool) {
    let mut options: Vec<String> = Vec::new();
    for item in args
        .get("options")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(text) = item.as_str() else { continue };
        let text: String = text
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .chars()
            .take(160)
            .collect();
        let folded = text.to_lowercase();
        let is_other = matches!(
            folded.trim_matches(|c: char| !c.is_alphanumeric()),
            "khác" | "khac" | "other" | "others" | "something else" | "khác nhập yêu cầu riêng"
        );
        if text.is_empty() || is_other || options.iter().any(|o| o.to_lowercase() == folded) {
            continue;
        }
        options.push(text);
        if options.len() == 8 {
            break;
        }
    }
    let multi = args.get("multi").and_then(Value::as_bool).unwrap_or(false) && options.len() > 1;
    (options, multi)
}

/// Steps that operate applications directly: the person should keep their hands off meanwhile.
/// After this many lookups (knowledge.*, web.*, document.read) in a row the planner is told to stop and answer.
const LOOKUP_STREAK_WARN: usize = 6;
const HANDS_ON_TOOLS: [&str; 16] = [
    "desktop.click",
    "desktop.type",
    "desktop.press_key",
    "desktop.hotkey",
    "desktop.drag",
    "desktop.scroll",
    "desktop.move_mouse",
    "desktop.launch_app",
    "desktop.focus_window",
    "desktop.close_app",
    "desktop.set_value",
    "paint.draw",
    "paint.draw_image",
    "art.draw",
    "art.paint",
    "desktop.save_as",
];
const BANNER_TEXT: &str =
    "Trợ lý đang điều khiển màn hình\nBạn thao tác → AI tạm dừng; chạy lại sau 3 giây yên lặng";
const ACTIVITY_PAUSE_TEXT: &str = "Bạn vừa thao tác · AI tự tiếp tục sau 3 giây yên lặng";
const ACTIVITY_PAUSE_BANNER_TEXT: &str =
    "Tạm dừng vì bạn đang thao tác\nAI tự tiếp tục sau 3 giây yên lặng";
const USER_IDLE_RESUME: Duration = Duration::from_secs(3);

/// Tool groups always shown in full; the rest load when the request mentions them or the planner asks.
const BASE_GROUPS: [&str; 7] = [
    "system",
    "knowledge",
    "web",
    "documents",
    "memory",
    "assistant",
    "task",
];
/// Words (lower case, without accents) that load a group from the start.
const GROUP_TRIGGERS: [(&str, &[&str]); 9] = [
    (
        "office",
        &[
            "excel",
            "word",
            "powerpoint",
            "outlook",
            "email",
            "e-mail",
            "mail",
            "thu dien tu",
            "gui thu",
            "hop thu",
            "pdf",
            ".xlsx",
            ".docx",
            ".pptx",
            "bang tinh",
            "van ban",
        ],
    ),
    (
        "desktop",
        &[
            "mo ",
            "open",
            "click",
            "bam ",
            "nhan ",
            "cua so",
            "window",
            "ung dung",
            "app",
            "notepad",
            "word",
            "excel",
            "powerpoint",
            "paint",
            "go ",
            "type",
            "nhap ",
            "man hinh",
            "screen",
            "chuot",
            "mouse",
            "ban phim",
            "keyboard",
            "dong ",
            "close",
            "zalo",
            "chrome",
            "luu ",
            "save",
            "giao dien",
            "phan mem",
        ],
    ),
    (
        "browser",
        &[
            "trang web",
            "website",
            "web page",
            "dang nhap",
            "login",
            "form",
            "trinh duyet",
            "browser",
            "youtube",
            "facebook",
            "dien ",
            "tai ve",
            "download",
            "google",
            "http",
        ],
    ),
    (
        "vision",
        &[
            "man hinh",
            "screen",
            "nhin",
            "anh chup",
            "screenshot",
            "hinh anh",
            "thay gi",
            "doc chu tren",
        ],
    ),
    (
        "autocad",
        &["autocad", "cad", "ban ve", "mat bang", "dwg", "dxf", "ve "],
    ),
    ("paint", &["paint", "mspaint"]),
    (
        "cad",
        &[
            "3d",
            "mo hinh 3d",
            "step",
            "stl",
            "3mf",
            "glb",
            "in 3d",
            "cad 3d",
            "build123d",
            "chi tiet co khi",
            "gia do",
            "ban le",
            "banh rang",
            "vo hop",
            "ngam",
            "ve 3d",
            "thiet ke 3d",
        ],
    ),
    (
        "activity",
        &[
            "theo doi",
            "tracking",
            "hom nay toi",
            "toi dang",
            "toi da lam",
            "toi lam gi",
            "dang lam gi",
            "thoi gian su dung",
            "screen time",
            "nang suat",
            "dung bao lau",
            "mat bao lau",
        ],
    ),
    (
        "art",
        &[
            "ve ", "draw", "tranh", "paint", "mspaint", "autocad", "dwg", "sketch", "ki hoa",
            "svg", "doraemon", "photo", "hinh anh",
        ],
    ),
];
/// Words that mean AutoCAD even when the request also says "paint" (a bare "ve " only means drawing).
const AUTOCAD_STRONG: [&str; 5] = ["autocad", "dwg", "dxf", "mat bang", "ban ve"];

/// The groups a request needs from the start, from its words (and the previous request it follows up).
fn groups_for(text: &str) -> Vec<String> {
    let normalized = format!(" {} ", brain_core::text::norm(text));
    let mut groups: Vec<String> = BASE_GROUPS.iter().map(|g| g.to_string()).collect();
    for (group, words) in GROUP_TRIGGERS {
        // "ve doraemon bang paint" is a Paint drawing, not an AutoCAD one: the bare word "ve " must not load AutoCAD.
        if group == "autocad"
            && normalized.contains("paint")
            && !AUTOCAD_STRONG.iter().any(|w| normalized.contains(w))
        {
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
const LLM_RETRY_DELAYS: [Duration; 3] = [
    Duration::from_secs(5),
    Duration::from_secs(20),
    Duration::from_secs(45),
];

/// Repair the shapes small models give task.complete: `checks` as an object instead of a list, stray
/// non-object check entries, unknown top-level keys, a non-string summary.
fn normalize_completion(args: &Value) -> Value {
    let mut out = serde_json::Map::new();
    if let Some(summary) = args.get("summary") {
        out.insert(
            "summary".into(),
            summary
                .as_str()
                .map_or_else(|| json!(summary.to_string()), |s| json!(s)),
        );
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
    for core in [
        "task.complete",
        "task.fail",
        "task.ask_user",
        "tools.enable",
    ] {
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
    let admin = if crate::native::env::is_admin() {
        "elevated"
    } else {
        "normal user rights, not elevated"
    };
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

impl<'a> TaskRunner<'a> {
    pub fn new(rt: &'a mut Runtime, ui: &'a dyn Interaction, record: &TaskRecord) -> Result<Self> {
        let screen_activity_pause = match std::env::var("LGA_SCREEN_CONTROL_MODE") {
            Ok(mode) if mode == "pause_on_activity" => true,
            Ok(mode) => {
                return Err(anyhow!(
                    "screen-control mode {mode:?} is not supported; use pause_on_activity"
                ))
            }
            Err(std::env::VarError::NotPresent) => true,
            Err(error) => return Err(anyhow!("could not read screen-control mode: {error}")),
        };
        let cancel = CancelToken::new(rt.store.dir(), &record.task_id);
        cancel.clear_activity_pause()?;
        let audit = AuditLog::open(
            &rt.config.logs_dir().join("tasks"),
            &record.task_id,
            rt.redactor.clone(),
        )?;
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
            suppressed_asks: 0,
            loaded_groups: groups_for(&format!(
                "{} {} {}",
                record.goal,
                record.conversation.join(" "),
                record
                    .attachments
                    .iter()
                    .map(|a| a.name.as_str())
                    .collect::<Vec<_>>()
                    .join(" ")
            )),
            banner: None,
            screen_activity_pause,
            approvals: crate::guard::Ledger::new(Duration::from_secs(120)),
        })
    }

    pub fn cancel_token(&self) -> CancelToken {
        self.cancel.clone()
    }

    fn save(&self, record: &TaskRecord) -> Result<()> {
        self.rt.store.save(record)
    }

    fn set_status(
        &self,
        record: &mut TaskRecord,
        status: TaskStatus,
        reason: Option<&str>,
    ) -> Result<()> {
        record.transition(status)?;
        self.audit.status(status, reason)?;
        self.save(record)?;
        self.ui.event(RunEvent::Status(status, reason));
        Ok(())
    }

    /// Messages the person sent while this task was running (`memory/tasks/<id>.say.jsonl`, one JSON line each, written
    /// by the chat window). They join the conversation the planner reads and are announced once as a notice, so the next
    /// planner turn takes them into account instead of waiting for the task to end.
    fn take_injected(&mut self, record: &mut TaskRecord) {
        let path = self
            .rt
            .store
            .dir()
            .join(format!("{}.say.jsonl", record.task_id));
        if !path.is_file() {
            return;
        }
        let taken = path.with_extension("taken");
        if std::fs::rename(&path, &taken).is_err() {
            return;
        }
        let Ok(text) = std::fs::read_to_string(&taken) else {
            return;
        };
        let _ = std::fs::remove_file(&taken);
        for line in text.lines() {
            let Ok(value) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let Some(said) = value
                .get("text")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|t| !t.is_empty())
            else {
                continue;
            };
            let said: String = said.chars().take(2000).collect();
            record
                .conversation
                .push(format!("The user added while you were working: {said}"));
            self.notices.push(format!(
                "The user sent a new message while you were working: \"{said}\". Take it into account now; if it changes what they want, follow it and say so."));
        }
    }

    /// `skills/auto-*.md`: a playbook the assistant wrote for itself from repeated verified successes (see
    /// `skills::write_auto_skill`). The person can see, edit and delete them in Settings.
    fn write_auto_skill(&self, skill: &crate::memory::GeneratedSkill) {
        if crate::skills::write_auto_skill(&self.rt.config.root.join("skills"), skill) {
            self.cortex_note(
                "hippocampus",
                &format!(
                    "new playbook {} from {} verified successes (triggers: {})",
                    skill.id,
                    skill.support,
                    skill.triggers.join(", ")
                ),
            );
        }
    }

    fn finish(&self, record: &mut TaskRecord, status: TaskStatus, reason: &str) -> Result<()> {
        if status == TaskStatus::Failed {
            record.failure_reason = Some(reason.to_string());
        }
        self.set_status(record, status, Some(reason))?;
        self.rt.memory.record_task(
            &record.task_id,
            &record.goal,
            status.as_str(),
            record.final_summary.as_deref().or(Some(reason)),
        )?;
        // Learning after the task: one episodic line built from the record (no model call, one SQLite write). A
        // memory problem must never fail the task, so it is only logged.
        let mut tools: Vec<String> = Vec::new();
        for step in &record.steps {
            if step.status == StepStatus::Succeeded
                && !step.tool.starts_with("task.")
                && !tools.contains(&step.tool)
            {
                tools.push(step.tool.clone());
            }
        }
        let verified = record.completion_basis.as_deref() == Some("verified");
        if status == TaskStatus::Completed && verified {
            let chain: Vec<String> = record
                .steps
                .iter()
                .filter(|s| s.status == StepStatus::Succeeded && !s.tool.starts_with("task."))
                .map(|s| s.tool.clone())
                .collect();
            let hints: Vec<String> = record
                .steps
                .iter()
                .filter(|s| {
                    s.status == StepStatus::Succeeded
                        && !s.tool.starts_with("task.")
                        && !s.thought.is_empty()
                })
                .map(|s| s.thought.chars().take(110).collect::<String>())
                .collect();
            match self.rt.memory.record_skill_candidate(
                &record.task_id,
                &record.goal,
                &chain,
                &hints,
            ) {
                Ok(Some(skill)) => self.write_auto_skill(&skill),
                Ok(None) => {}
                Err(e) => {
                    let _ = self.audit.record(AuditEvent {
                        kind: "memory_error",
                        error: Some(json!(e.to_string())),
                        ..Default::default()
                    });
                }
            }
        }
        if let Err(e) = self.rt.memory.record_episode(crate::memory::Episode {
            task_id: &record.task_id,
            goal: &record.goal,
            status: status.as_str(),
            summary: record.final_summary.as_deref(),
            failure: record
                .failure_reason
                .as_deref()
                .or(Some(reason))
                .filter(|_| status != TaskStatus::Completed),
            tools: &tools,
            steps: record.steps.len(),
            verified,
        }) {
            let _ = self.audit.record(AuditEvent {
                kind: "memory_error",
                error: Some(json!(e.to_string())),
                ..Default::default()
            });
        }
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
        self.cancel
            .clear()
            .context("clearing stale cancel marker")?;
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

    async fn run_loop(
        &mut self,
        record: &mut TaskRecord,
        started: Instant,
        budget: Duration,
    ) -> Result<()> {
        let max_steps = self.rt.config.agent.max_steps;
        // Shifted forward by time spent paused, so pauses do not consume the task's time budget.
        let mut started = started;
        let mut observed_activity = 0;
        loop {
            if self.cancel.is_cancelled() {
                return self.finish(record, TaskStatus::Cancelled, "cancelled by the user");
            }
            if record.steps.len() as u32 >= max_steps {
                return self.finish(
                    record,
                    TaskStatus::Failed,
                    &format!("step budget of {max_steps} exhausted"),
                );
            }
            if started.elapsed() >= budget {
                return self.finish(record, TaskStatus::Failed, "task time budget exhausted");
            }
            if record.consecutive_failures >= self.rt.config.agent.max_consecutive_failures {
                let last = record
                    .steps
                    .last()
                    .and_then(|s| s.error.as_ref())
                    .map(|e| e.to_string())
                    .unwrap_or_default();
                return self.finish(
                    record,
                    TaskStatus::Failed,
                    &format!(
                        "{} consecutive failed steps; last: {last}",
                        record.consecutive_failures
                    ),
                );
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

            self.take_injected(record);
            let Some(actions) = self.plan_next(record).await? else {
                continue;
            };
            // A reply may hold several independent actions; they run in order and the batch stops at the
            // first one that does not succeed, so the planner always sees that failure next.
            // Set once this batch ran a read-only step: its results were not visible when the reply was written.
            let mut read_in_batch = false;
            for action in actions {
                if self.cancel.is_cancelled() || record.steps.len() as u32 >= max_steps {
                    break;
                }
                if self.screen_activity_pause && HANDS_ON_TOOLS.contains(&action.tool.as_str()) {
                    if let Some(banner) = self.banner.as_ref() {
                        let generation = banner.activity_generation();
                        if generation != observed_activity {
                            observed_activity = generation;
                            banner
                                .set_text(ACTIVITY_PAUSE_BANNER_TEXT)
                                .map_err(|error| {
                                    anyhow!("screen-control ribbon update failed: {error}")
                                })?;
                            self.cancel.pause_for_activity()?;
                            self.set_status(record, TaskStatus::Paused, Some(ACTIVITY_PAUSE_TEXT))?;
                            let paused_at = Instant::now();
                            let mut quiet_since = Instant::now();
                            loop {
                                if self.cancel.is_cancelled() {
                                    self.cancel.clear_activity_pause()?;
                                    return self.finish(
                                        record,
                                        TaskStatus::Cancelled,
                                        "cancelled while waiting for desktop activity to stop",
                                    );
                                }
                                while self.cancel.is_paused() && !self.cancel.is_cancelled() {
                                    tokio::time::sleep(crate::cancel::POLL_INTERVAL).await;
                                }
                                if self.cancel.is_cancelled() {
                                    self.cancel.clear_activity_pause()?;
                                    return self.finish(
                                        record,
                                        TaskStatus::Cancelled,
                                        "cancelled while waiting for desktop activity to stop",
                                    );
                                }
                                let current = self
                                    .banner
                                    .as_ref()
                                    .map_or(observed_activity, |b| b.activity_generation());
                                if current != observed_activity {
                                    observed_activity = current;
                                    quiet_since = Instant::now();
                                }
                                if quiet_since.elapsed() >= USER_IDLE_RESUME {
                                    let latest = self
                                        .banner
                                        .as_ref()
                                        .map_or(observed_activity, |b| b.activity_generation());
                                    if latest == observed_activity {
                                        break;
                                    }
                                    observed_activity = latest;
                                    quiet_since = Instant::now();
                                }
                                tokio::time::sleep(Duration::from_millis(100)).await;
                            }
                            self.cancel.clear_activity_pause()?;
                            if let Some(banner) = self.banner.as_ref() {
                                banner.set_text(BANNER_TEXT).map_err(|error| {
                                    anyhow!("screen-control ribbon update failed: {error}")
                                })?;
                            }
                            started += paused_at.elapsed();
                            self.set_status(
                                record,
                                TaskStatus::Planning,
                                Some("desktop is idle; resuming"),
                            )?;
                        }
                    }
                }
                // The previous action of the batch may have left the task Observing/Executing.
                if record.status != TaskStatus::Planning {
                    self.set_status(record, TaskStatus::Planning, None)?;
                }
                if let Some(plan) = &action.plan {
                    record.plan = plan.clone();
                    self.audit.record(AuditEvent {
                        kind: "plan",
                        detail: json!({"plan": plan}),
                        ..Default::default()
                    })?;
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
                self.ui.event(RunEvent::Thinking {
                    step_id: &step_id,
                    thought: &action.thought,
                    tool: &action.tool,
                    args: &action.args,
                });

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
                        let group = action
                            .args
                            .get("category")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .trim()
                            .to_lowercase();
                        let served: Vec<String> = self.rt.worker().await?.tools().to_vec();
                        let known = self.rt.catalog.categories(&served);
                        if known.contains(&group) {
                            if !self.loaded_groups.contains(&group) {
                                self.loaded_groups.push(group.clone());
                            }
                            self.notices.push(format!(
                                "The {group} tools are now listed under Tools; use them."
                            ));
                        } else {
                            self.notices.push(format!(
                                "tools.enable: no tool group {group:?}; groups: {}",
                                known.join(", ")
                            ));
                        }
                        break;
                    }
                    "task.fail" => {
                        let reason = action
                            .args
                            .get("reason")
                            .and_then(Value::as_str)
                            .unwrap_or("planner gave up")
                            .to_string();
                        record.final_summary = Some(reason.clone());
                        return self.finish(record, TaskStatus::Failed, &reason);
                    }
                    "task.ask_user" => {
                        self.banner = None; // the person has to answer: hands back on
                        let question = action
                            .args
                            .get("question")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_string();
                        let (options, multi) = clarification_options(&action.args);
                        // "Do you need anything else?" is not a question the task depends on: it only parks the task
                        // (and ends the process after the wait). A chat turn ends with task.complete instead.
                        if is_closing_question(
                            &question,
                            record
                                .steps
                                .iter()
                                .any(|st| st.status == StepStatus::Succeeded),
                        ) {
                            record.final_summary = Some(question.clone());
                            record.completion_basis = Some("direct_answer".to_string());
                            return self.finish(record, TaskStatus::Completed, "answered");
                        }
                        // "Shall I try again?" is not a decision for the person: retry another way, or give up honestly.
                        if is_retry_permission(&question) {
                            self.suppressed_asks += 1;
                            if self.suppressed_asks >= 3 {
                                record.final_summary = Some(question.clone());
                                return self.finish(record, TaskStatus::Failed, &format!("could not finish and kept asking whether to retry: {question}"));
                            }
                            self.notices.push("Do not ask the user for permission to retry. If your last step failed, fix the cause (read the error, change the arguments or the tool) and continue; if it cannot be done, finish with task.fail and say why.".to_string());
                            break;
                        }
                        let asked = record
                            .conversation
                            .iter()
                            .filter(|c| c.starts_with("You asked the user:"))
                            .count();
                        if asked >= MAX_QUESTIONS_PER_TASK {
                            self.notices.push(format!(
                                "You have already asked the user {asked} questions in this task. Do not ask again: choose the most sensible default, say which assumption you made, and continue or finish."));
                            break;
                        }
                        record.pending_question = Some(question.clone());
                        record.pending_options = options.clone();
                        record.pending_multi = multi;
                        self.set_status(record, TaskStatus::NeedsUser, Some(&question))?;
                        match self.ui.ask_choice(&question, &options, multi) {
                            Some(answer) => {
                                record
                                    .conversation
                                    .push(format!("You asked the user: {question}"));
                                record
                                    .conversation
                                    .push(format!("The user answered: {answer}"));
                                record.pending_question = None;
                                record.pending_options.clear();
                                record.pending_multi = false;
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
                        if self
                            .rt
                            .catalog
                            .get(&action.tool)
                            .is_some_and(|spec| !spec.changes_state())
                        {
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
        let core = [
            "task.complete",
            "task.fail",
            "task.ask_user",
            "tools.enable",
        ];
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
            let group = self
                .rt
                .catalog
                .get(&step.tool)
                .map(|s| s.category.clone())
                .unwrap_or_default();
            if !group.is_empty() && !self.loaded_groups.contains(&group) {
                self.loaded_groups.push(group);
            }
        }
        let catalog = self
            .rt
            .catalog
            .render_for_prompt(&available, Some(&self.loaded_groups));
        let system = system_prompt(
            &catalog,
            self.rt.policy.mode(),
            &agent_cfg.language,
            &self.environment,
        );
        let offered: Vec<String> = available
            .iter()
            .filter(|t| {
                self.rt
                    .catalog
                    .get(t)
                    .is_some_and(|s| self.loaded_groups.contains(&s.category))
            })
            .cloned()
            .collect();
        let now = chrono::Local::now()
            .format("%A %Y-%m-%d %H:%M (UTC%:z)")
            .to_string();
        let recall = self.rt.memory.recall(&record.goal, max_recalled)?.render();
        let playbooks = {
            let book = crate::skills::load(&self.rt.config.root.join("skills"));
            let goal_n = brain_core::text::norm(&format!(
                "{} {}",
                record.goal,
                record.conversation.join(" ")
            ));
            let mut text = crate::skills::render(&crate::skills::matching(&book, &goal_n));
            // hints the bench-driven optimiser decided to keep (tests/bench/optimize_hints.py)
            if let Ok(hints) = std::fs::read_to_string(
                self.rt
                    .config
                    .root
                    .join("memory")
                    .join("optimizer")
                    .join("hints.md"),
            ) {
                let hints: String = hints.trim().chars().take(1500).collect();
                if !hints.is_empty() {
                    text.push_str(&format!(
                        "## Learned hints
{hints}

"
                    ));
                }
            }
            text
        };
        let mut attachments = Vec::new();
        for image in record
            .attachments
            .iter()
            .filter(|a| a.is_image())
            .take(MAX_USER_IMAGES_PER_TURN)
        {
            let path = PathBuf::from(&image.path);
            if !path.extension().and_then(|e| e.to_str()).is_some_and(|e| {
                PLANNER_IMAGE_EXTENSIONS.contains(&e.to_ascii_lowercase().as_str())
            }) {
                continue; // BMP/TIFF/HEIC: the brains cannot take them; document.read describes them instead
            }
            match self.rt.llm.upload(&path).await {
                Ok(id) => attachments.push(id),
                Err(err) => self.notices.push(format!("The attached image {} could not be sent to you ({err}); read it with document.read instead.", image.name)),
            }
        }
        for path in self
            .latest_screenshots
            .iter()
            .rev()
            .take(agent_cfg.history_images)
        {
            match self.rt.llm.upload(path).await {
                Ok(id) => attachments.push(id),
                Err(err) => self.notices.push(format!(
                    "The screenshot {} could not be sent to you ({err}); rely on vision.locate.",
                    path.display()
                )),
            }
        }
        for attempt in 0..=agent_cfg.max_parse_retries {
            let tool_decls = self.tool_declarations(&offered);
            // Context budget: whole prompt (system + tools + turn + images) must fit max_context_tokens. Older step
            // records are dropped first (they stay listed by name), then the latest observation is clipped.
            let fixed_chars = system.chars().count()
                + serde_json::to_string(&tool_decls).map_or(0, |t| t.chars().count());
            let image_tokens = attachments.len() * 800;
            let budget_chars = agent_cfg
                .max_context_tokens
                .saturating_sub(image_tokens + 1500)
                * 3;
            let mut history = agent_cfg.history_steps;
            let mut observation_cap = usize::MAX;
            let turn = loop {
                let observation = self
                    .latest_observation
                    .as_deref()
                    .map(|o| o.chars().take(observation_cap).collect::<String>());
                let turn = user_turn(&TurnContext {
                    playbooks: &playbooks,
                    goal: &record.goal,
                    plan: &record.plan,
                    steps: &record.steps,
                    history_steps: history,
                    max_steps: agent_cfg.max_steps,
                    consecutive_failures: record.consecutive_failures,
                    recall: &recall,
                    latest_observation: observation.as_deref(),
                    notices: &self.notices,
                    now: &now,
                    conversation: &record.conversation,
                    attachments: &record.attachments,
                });
                if fixed_chars + turn.chars().count() <= budget_chars {
                    break turn;
                }
                if history > 2 {
                    history -= 1;
                } else if observation_cap > 1500 {
                    observation_cap = if observation_cap == usize::MAX {
                        4000
                    } else {
                        observation_cap / 2
                    };
                } else {
                    break turn;
                }
            };
            let mut user = ChatMessage::text("user", turn);
            user.content
                .extend(attachments.iter().map(|id| Part::Image {
                    attachment_id: id.clone(),
                }));
            let request = ChatRequest {
                messages: vec![ChatMessage::text("system", system.clone()), user],
                model: llm_cfg.model.clone(),
                timeout: Duration::from_millis(llm_cfg.request_timeout_ms),
                metadata: HashMap::from([("task_id".into(), record.task_id.clone())]),
                json: true,
                schema: Some(planner_reply_schema(&offered)),
                tools: Some(tool_decls),
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

    async fn chat_with_retry(
        &mut self,
        record: &mut TaskRecord,
        request: ChatRequest,
    ) -> Result<String> {
        let mut attempt = 0;
        loop {
            attempt += 1;
            record.llm_calls += 1;
            let started = Instant::now();
            let partial = self
                .rt
                .store
                .dir()
                .join(format!("{}.partial.json", record.task_id));
            let streaming = self.rt.config.agent.stream_answers;
            let result = tokio::select! {
                r = streamed_chat(self.rt.llm.as_ref(), request.clone(), &partial, streaming) => r,
                _ = self.cancel.cancelled() => {
                    let _ = std::fs::remove_file(&partial);
                    return Err(anyhow!("cancelled while waiting for the planner"));
                }
            };
            let _ = std::fs::remove_file(&partial);
            match result {
                Ok(reply) => {
                    self.audit.record(AuditEvent {
                        kind: "llm",
                        provider: Some(format!("{}:{}", self.rt.llm.name(), reply.backend)),
                        duration_ms: Some(now_ms(started)),
                        retry_count: Some(attempt - 1),
                        detail: json!({"model": reply.model, "attempts": reply.attempts, "usage": reply.usage}),
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
                    self.ui.event(RunEvent::Notice(&format!(
                        "LLM unavailable ({err}); retrying in {delay:?}"
                    )));
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

    fn rejected(
        &self,
        mut step: StepRecord,
        status: StepStatus,
        error: ToolErrorInfo,
    ) -> StepOutcome {
        step.status = status;
        step.error = Some(error);
        step.finished_at = Some(Utc::now());
        StepOutcome {
            record: step,
            observation_for_planner: None,
            screenshot: None,
            surprise: None,
        }
    }

    /// One line in the audit log (and the activity panel) for every time a helper region of the assistant stepped in.
    fn cortex_note(&self, region: &str, note: &str) {
        let _ = self.audit.record(AuditEvent {
            kind: "cortex",
            detail: json!({"region": region, "note": note}),
            ..Default::default()
        });
        self.ui
            .event(RunEvent::Notice(&format!("[{region}] {note}")));
    }

    async fn execute_step(
        &mut self,
        record: &mut TaskRecord,
        action: &PlannerAction,
    ) -> Result<StepOutcome> {
        // cerebellum: arguments that only break the schema in trivial ways are repaired instead of costing a planner turn
        let repaired;
        let action = match self.rt.catalog.get(&action.tool).filter(|spec| {
            !self
                .rt
                .catalog
                .validate_args(&spec.name, &action.args)
                .is_empty()
        }) {
            Some(spec) => match crate::cortex::refine_args(&spec.input_schema, &action.args) {
                Some((fixed, notes))
                    if self.rt.catalog.validate_args(&spec.name, &fixed).is_empty() =>
                {
                    let note = format!("{}: {}", spec.name, notes.join(", "));
                    self.cortex_note("cerebellum", &note);
                    repaired = PlannerAction {
                        args: fixed,
                        ..action.clone()
                    };
                    &repaired
                }
                _ => action,
            },
            None => action,
        };
        let step = self.new_step(record, action, &action.args);
        let Some(spec) = self.rt.catalog.get(&action.tool).cloned() else {
            let err = ToolErrorInfo::new(
                ErrorCode::UnknownTool,
                format!("{} is not a tool", action.tool),
                false,
            );
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        };
        let worker = self.rt.worker().await?;
        if !worker.tools().iter().any(|t| t == &spec.name) {
            let err = ToolErrorInfo::new(
                ErrorCode::UnknownTool,
                format!("{} is not available in this installation", spec.name),
                false,
            );
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
                && !s.error.as_ref().is_some_and(|e| {
                    matches!(
                        e.code,
                        ErrorCode::Timeout | ErrorCode::Busy | ErrorCode::BackendUnavailable
                    )
                })
        });
        if let Some(previous) = failed_before {
            let why = previous
                .error
                .as_ref()
                .map(|e| e.message.clone())
                .unwrap_or_default();
            let err = ToolErrorInfo::new(
                ErrorCode::Conflict,
                format!("this exact call already failed in {} ({why}); fix the cause and change the call instead of repeating it", previous.step_id),
                false,
            );
            return Ok(self.rejected(step, StepStatus::Rejected, err));
        }
        let identical = record
            .steps
            .iter()
            .rev()
            .take_while(|s| s.input_hash == step.input_hash)
            .count() as u32;
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
                let always = !decision.always_confirm.is_empty();
                let approved = if always {
                    self.ui
                        .confirm_always(&spec.name, &decision.args, &decision.reasons)
                } else {
                    self.ui
                        .confirm(&spec.name, &decision.args, &decision.reasons)
                };
                if !approved {
                    let err = ToolErrorInfo::new(
                        ErrorCode::PolicyDenied,
                        format!("the user declined: {}", decision.reasons.join("; ")),
                        false,
                    );
                    return Ok(self.rejected(step, StepStatus::Denied, err));
                }
                if always {
                    // The approval belongs to exactly this call. It is recorded, then consumed by the check below
                    // right before the call runs; anything that changed the call in between would not match.
                    self.approvals.approve(&decision.fingerprint);
                    self.audit.record(AuditEvent {
                        kind: "always_confirm_approved",
                        step_id: Some(step.step_id.clone()),
                        tool: Some(spec.name.clone()),
                        detail: json!({"classes": decision.always_confirm, "fingerprint": decision.fingerprint, "reasons": decision.reasons}),
                        ..Default::default()
                    })?;
                }
            }
            Verdict::Allow => {}
        }
        if !decision.always_confirm.is_empty() {
            let executed = crate::guard::fingerprint(&spec.name, &decision.args);
            if !self.approvals.consume(&executed) {
                let err = ToolErrorInfo::new(
                    ErrorCode::PolicyDenied,
                    "the confirmation does not match the call that was about to run; it was not executed",
                    false,
                );
                return Ok(self.rejected(step, StepStatus::Denied, err));
            }
        }
        self.set_status(record, TaskStatus::Executing, None)?;
        if self.banner.is_none() && HANDS_ON_TOOLS.contains(&spec.name.as_str()) {
            self.banner = Some(
                crate::banner::Banner::show(
                    BANNER_TEXT,
                    self.cancel.clone(),
                    self.screen_activity_pause,
                )
                .map_err(|error| {
                    anyhow!(
                        "screen control blocked because its stop ribbon could not start: {error}"
                    )
                })?,
            );
        }
        self.run_tool(record, &spec, step, decision.args).await
    }

    async fn run_tool(
        &mut self,
        record: &mut TaskRecord,
        spec: &ToolSpec,
        mut step: StepRecord,
        args: Value,
    ) -> Result<StepOutcome> {
        let started = Instant::now();
        let budget_left = Duration::from_millis(self.rt.config.agent.task_timeout_ms)
            .saturating_sub(started.elapsed());
        let timeout = Duration::from_millis(spec.timeout_ms)
            .min(budget_left)
            .max(MIN_TOOL_TIMEOUT);
        let mut attempt = 0u32;
        let response = loop {
            attempt += 1;
            let worker = self.rt.worker().await?;
            let call = ToolCall::new(
                &record.task_id,
                &step.step_id,
                &spec.name,
                args.clone(),
                timeout,
            );
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
                    self.audit.record(AuditEvent {
                        kind: "worker_restart",
                        tool: Some(spec.name.clone()),
                        error: Some(json!(err.to_string())),
                        ..Default::default()
                    })?;
                    if record.worker_restarts > self.rt.config.worker.max_restarts {
                        return Err(anyhow!(
                            "worker failed {} times; giving up: {err}",
                            record.worker_restarts
                        ));
                    }
                    self.ui
                        .event(RunEvent::Notice(&format!("worker {err}; restarting it")));
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
            self.ui.event(RunEvent::Notice(&format!(
                "{} failed ({failure}); retry {attempt} in {delay:?}",
                spec.name
            )));
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
                step.status = if result.verification.status == "failed" {
                    StepStatus::Failed
                } else {
                    StepStatus::Succeeded
                };
                if step.status == StepStatus::Failed {
                    let failed: Vec<String> = result
                        .verification
                        .checks
                        .iter()
                        .filter(|c| c.get("passed") == Some(&Value::Bool(false)))
                        .map(|c| {
                            format!(
                                "{}: {}",
                                c["name"].as_str().unwrap_or("check"),
                                c["detail"].as_str().unwrap_or("")
                            )
                        })
                        .collect();
                    step.error = Some(ToolErrorInfo::new(
                        ErrorCode::VerificationFailed,
                        format!("post-condition failed: {}", failed.join("; ")),
                        true,
                    ));
                }
            }
            (_, Some(error)) => {
                step.status = StepStatus::Failed;
                step.error = Some(ToolErrorInfo {
                    message: self.rt.redactor.redact(&error.message),
                    ..error.clone()
                });
                if error.code == ErrorCode::ElevationRequired {
                    record.escalation = error
                        .details
                        .get("escalation_request")
                        .cloned()
                        .or(Some(error.details.clone()));
                }
            }
            _ => {
                step.status = StepStatus::Failed;
                step.error = Some(ToolErrorInfo::new(
                    ErrorCode::Internal,
                    "worker response had neither result nor error",
                    false,
                ));
            }
        }

        // Look at the screen after every action that touches an application (also the self-verified ones:
        // the planner should see the window it just opened or the state after a save), after anything
        // unverified, and after a failed desktop/browser step.
        let hands_on = spec.category == "desktop" && HANDS_ON_TOOLS.contains(&spec.name.as_str());
        let needs_observation = matches!(step.verification.as_deref(), Some("deferred"))
            || hands_on
            || step.status == StepStatus::Failed
                && (spec.category == "desktop" || spec.category == "browser");
        let mut surprise = None;
        let observation = if needs_observation && !self.cancel.is_cancelled() {
            self.set_status(
                record,
                if step.status == StepStatus::Failed {
                    TaskStatus::Recovering
                } else {
                    TaskStatus::Observing
                },
                None,
            )?;
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
        Ok(StepOutcome {
            record: step,
            observation_for_planner: observation,
            screenshot,
            surprise,
        })
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
            (
                "desktop.observe",
                json!({"max_elements": OBSERVE_MAX_ELEMENTS}),
            )
        } else {
            (
                "desktop.inspect_ui",
                json!({"max_elements": OBSERVE_MAX_ELEMENTS}),
            )
        };
        if !has(tool) {
            return None;
        }
        let call = ToolCall::new(&record.task_id, "observe", tool, args, OBSERVE_TIMEOUT);
        let seen = match worker.call(&call).await {
            Ok(response) if response.is_ok() => {
                let result = response.result?;
                let screenshot = result
                    .artifacts
                    .iter()
                    .filter(|a| a.get("kind").and_then(Value::as_str) == Some("screenshot"))
                    .find_map(|a| a.get("path").and_then(Value::as_str).map(PathBuf::from));
                let surprise = result
                    .data
                    .get("surprise")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(String::from);
                Observation {
                    text: format!("(automatic {tool}) {}", result.observation),
                    screenshot,
                    surprise,
                }
            }
            Ok(response) => Observation {
                text: format!(
                    "(automatic {tool} failed: {})",
                    response.error.map(|e| e.to_string()).unwrap_or_default()
                ),
                screenshot: None,
                surprise: None,
            },
            Err(err) => Observation {
                text: format!("(automatic {tool} failed: {err})"),
                screenshot: None,
                surprise: None,
            },
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
        // Many steps for one request: stop and take stock (a model that flails varies its calls, so no repeat check sees it).
        let n_steps = record.steps.len() + 1;
        if n_steps.is_multiple_of(12) {
            self.notices.push(format!(
                "You have used {n_steps} steps. Take stock: what is done and verified, what remains. Do not give up and do not hand the                  work back to the user: the request will be finished. If you keep getting the same result, you are repeating yourself:                  use a different tool, one script instead of many calls, or split the work into parts you finish one by one. If the                  request is already satisfied, call task.complete."));
        }
        // The same check with the same answer again and again is a model that cannot decide it is finished.
        if let Some(notice) = repeated_check_notice(&record.steps, &step) {
            self.notices.push(notice);
        }
        // A long run of lookups with nothing produced is a loop, not research: say so, in the planner's own terms.
        let is_lookup =
            |t: &str| t.starts_with("knowledge.") || t.starts_with("web.") || t == "document.read";
        if is_lookup(&step.tool) {
            let streak = 1 + record
                .steps
                .iter()
                .rev()
                .take_while(|s| is_lookup(&s.tool))
                .count();
            if streak == LOOKUP_STREAK_WARN || streak == 2 * LOOKUP_STREAK_WARN {
                self.notices.push(format!(
                    "You have made {streak} lookups in a row. Stop searching: what you have found is all there is. Do the task with it now:                      write the answer, using a neutral wording or a placeholder for what was not found, and say plainly what you could not find.                      Do not look up names or details the user did not ask you to find."));
            }
        }
        match step.status {
            StepStatus::Succeeded => {
                record.consecutive_failures = 0;
                let recovered: Vec<(String, ErrorCode, String)> = self
                    .pending_failures
                    .iter()
                    .filter(|(t, _, _)| *t == step.tool)
                    .cloned()
                    .collect();
                for (tool, code, context) in recovered {
                    let recovery = format!("succeeded at {} after re-observing", step.step_id);
                    self.rt.memory.record_failure(
                        &tool,
                        code.as_str(),
                        &context,
                        Some(&recovery),
                    )?;
                }
                self.pending_failures.retain(|(t, _, _)| *t != step.tool);
            }
            StepStatus::Cancelled => {}
            _ => {
                record.consecutive_failures += 1;
                if let Some(notice) = failure_coaching(record.consecutive_failures, &step) {
                    self.notices.push(notice);
                }
                if let Some(error) = &step.error {
                    let context: String = format!("{} | {}", record.goal, error.message)
                        .chars()
                        .take(200)
                        .collect();
                    self.rt.memory.record_failure(
                        &step.tool,
                        error.code.as_str(),
                        &context,
                        None,
                    )?;
                    self.pending_failures
                        .push((step.tool.clone(), error.code, context));
                }
            }
        }
        record.artifacts.extend(step.artifacts.iter().cloned());
        if let Some(path) = outcome.screenshot {
            self.latest_screenshots.push(path);
        }
        self.latest_observation = outcome
            .observation_for_planner
            .or_else(|| Some(step.observation.clone()).filter(|o| !o.is_empty()));
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
    async fn llm_verdict(
        &mut self,
        record: &mut TaskRecord,
        summary: &str,
        checks: &[Value],
    ) -> Result<Result<(), String>> {
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
            metadata: HashMap::from([
                ("task_id".into(), record.task_id.clone()),
                ("role".into(), "verifier".into()),
            ]),
            json: true,
            schema: None,
            tools: None,
        };
        let reply = self.chat_with_retry(record, request).await?;
        let verdict = crate::planner::extract_json_value(&reply);
        let decision = verdict
            .as_ref()
            .and_then(|v| v.get("verdict"))
            .and_then(Value::as_str)
            .unwrap_or("");
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
            _ => Err(format!(
                "verifier reply was not a verdict: {}",
                clip_text(&reply, 200)
            )),
        })
    }

    /// VERIFY: run the planner's completion checks; complete only if all pass. The rules (what counts as proof,
    /// when the second opinion is asked, when a completion is accepted and as what) are in `completion.rs`.
    async fn verify_completion(
        &mut self,
        record: &mut TaskRecord,
        action: &PlannerAction,
    ) -> Result<bool> {
        let normalized = PlannerAction {
            args: normalize_completion(&action.args),
            ..action.clone()
        };
        let action = &normalized;
        let violations = self.rt.catalog.validate_args("task.complete", &action.args);
        if !violations.is_empty() {
            self.notices.push(format!(
                "task.complete arguments are invalid: {}",
                violations.join("; ")
            ));
            // Counted like a rejected completion, not as a failed step: finished work must not be
            // thrown away because the closing call was malformed.
            self.completion_rejections += 1;
            if self.completion_rejections > MAX_COMPLETION_REJECTIONS + 1 {
                record.consecutive_failures += 1;
            }
            return Ok(false);
        }
        self.set_status(record, TaskStatus::Verifying, None)?;
        let assessment = {
            let mut executor = WorkerChecks {
                rt: &mut *self.rt,
                task_id: record.task_id.clone(),
            };
            completion::assess(&action.args, &record.steps, &mut executor).await?
        };
        let mut failures = assessment.failures;
        let summary = action.args["summary"]
            .as_str()
            .unwrap_or_default()
            .to_string();
        if let Some(why) = crate::cortex::unfinished_work(&record.steps, &summary) {
            self.cortex_note("cingulate", &why);
            failures.push(format!("not finished: {why}"));
        }
        let direct_answer = completion::is_direct_answer(&record.steps, &action.args);
        let ui_work = completion::has_ui_work(&record.steps, &self.rt.catalog);
        if completion::needs_second_opinion(
            &failures,
            self.rt.config.agent.verify_completion_with_llm,
            direct_answer,
            ui_work,
        ) {
            if let Err(rejection) = self
                .llm_verdict(record, &summary, &assessment.check_results)
                .await?
            {
                failures.push(format!(
                    "independent verifier rejected the completion: {rejection}"
                ));
            }
        }
        self.audit.record(AuditEvent {
            kind: "completion_check",
            tool: Some("task.complete".into()),
            verification_result: Some(if failures.is_empty() { "passed".into() } else { "failed".into() }),
            detail: json!({"summary": summary, "evidence": assessment.evidence, "failures": failures}),
            ..Default::default()
        })?;
        let did_work = record
            .steps
            .iter()
            .any(|s| s.status == StepStatus::Succeeded);
        match completion::decide(
            &failures,
            direct_answer,
            did_work,
            self.completion_rejections,
            MAX_COMPLETION_REJECTIONS,
        ) {
            completion::Verdict::Accept(basis) => {
                let unverified = basis == completion::Basis::Unverified;
                let summary = if unverified {
                    let note = if self.rt.config.agent.language.starts_with("vi") {
                        "Lưu ý: chưa kiểm chứng được đầy đủ kết quả"
                    } else {
                        "Note: the result could not be fully verified"
                    };
                    // The warning leads: the model's own claim ("I drew it") must not be what the person reads first.
                    format!(
                        "⚠ {note} ({}).\n\n{summary}",
                        clip_text(&failures.join("; "), 400)
                    )
                } else {
                    summary
                };
                record.final_summary = Some(summary);
                record.completion_basis = Some(basis.as_str().to_string());
                record.unverified_checks = if unverified {
                    failures.clone()
                } else {
                    Vec::new()
                };
                if let Some(artifacts) = action.args.get("artifacts").and_then(Value::as_array) {
                    record
                        .artifacts
                        .extend(artifacts.iter().filter_map(Value::as_str).map(String::from));
                }
                let procedure: Vec<String> = record
                    .steps
                    .iter()
                    .filter(|s| s.status == StepStatus::Succeeded)
                    .map(|s| s.tool.clone())
                    .collect();
                self.rt
                    .memory
                    .record_procedure(&record.goal, &procedure.join(" -> "))?;
                let reason = if unverified {
                    "completed; some completion checks could not be confirmed"
                } else {
                    "all completion checks passed"
                };
                self.finish(record, TaskStatus::Completed, reason)?;
                Ok(true)
            }
            completion::Verdict::Reject => {
                // Rejections are bounded by MAX_COMPLETION_REJECTIONS, not by the consecutive-failure limit, so
                // finished work is not thrown away because its checks were phrased badly.
                self.completion_rejections += 1;
                self.notices.push(format!(
                    "task.complete was REJECTED because these checks failed: {}. The task is not done; fix it or adjust the checks to what the user actually asked.",
                    failures.join(" | ")
                ));
                self.set_status(
                    record,
                    TaskStatus::Planning,
                    Some("completion checks failed"),
                )?;
                Ok(false)
            }
        }
    }
}

/// Runs completion checks through the policy and the worker (the loop's side of `completion::CheckExecutor`).
struct WorkerChecks<'x> {
    rt: &'x mut Runtime,
    task_id: String,
}

#[async_trait::async_trait]
impl completion::CheckExecutor for WorkerChecks<'_> {
    fn catalog(&self) -> &ToolCatalog {
        &self.rt.catalog
    }

    async fn run(&mut self, spec: &ToolSpec, args: Value) -> Result<completion::CheckOutcome> {
        use completion::CheckOutcome;
        let decision = self.rt.policy.evaluate(spec, &args);
        if decision.verdict == Verdict::Deny {
            return Ok(CheckOutcome::Denied);
        }
        let worker = self.rt.worker().await?;
        let call = ToolCall::new(
            &self.task_id,
            "verify",
            &spec.name,
            decision.args,
            Duration::from_millis(spec.timeout_ms),
        );
        Ok(match worker.call(&call).await {
            Ok(response) if response.is_ok() => {
                CheckOutcome::Data(response.result.map(|r| r.data).unwrap_or(Value::Null))
            }
            Ok(response) => {
                CheckOutcome::ToolError(response.error.map(|e| e.to_string()).unwrap_or_default())
            }
            Err(err) => CheckOutcome::NotRun(err.to_string()),
        })
    }
}

#[cfg(test)]
mod tests {
    fn failed_step(observation: &str) -> crate::state::StepRecord {
        let mut s: crate::state::StepRecord = serde_json::from_value(serde_json::json!({
            "step_id": "s003", "index": 3, "started_at": "2026-10-14T00:00:00Z", "finished_at": null, "thought": "",
            "tool": "python.exec", "args": {}, "input_hash": "", "status": "failed", "verification": null,
            "observation": observation, "error": null, "retry_count": 0, "duration_ms": 1, "backend": "", "provider": "",
            "artifacts": []
        })).expect("step record");
        s.observation = observation.to_string();
        s
    }

    #[test]
    fn identical_checks_are_called_out_on_every_third_repeat() {
        let mut step = failed_step("file exists (10 bytes)");
        step.status = crate::state::StepStatus::Succeeded;
        step.tool = "filesystem.stat".into();
        step.input_hash = "h1".into();
        let mut history = Vec::new();
        for n in 1..=9 {
            let notice = super::repeated_check_notice(&history, &step);
            assert_eq!(notice.is_some(), n % 3 == 0, "repeat {n}: {notice:?}");
            if n >= 6 && n % 3 == 0 {
                assert!(
                    notice.unwrap().contains("MUST be task.complete"),
                    "repeat {n} must force a decision"
                );
            }
            history.push(step.clone());
        }
        let mut other = step.clone();
        other.input_hash = "h2".into();
        assert!(super::repeated_check_notice(&history, &other).is_none());
    }

    #[test]
    fn clarification_options_are_cleaned_and_never_contain_the_models_own_other() {
        let args = serde_json::json!({"question": "q", "options": ["Báo cáo tuần", "  Báo   cáo tuần ", "Khác", "Báo cáo tháng", "", 7], "multi": true});
        let (options, multi) = super::clarification_options(&args);
        assert_eq!(options, vec!["Báo cáo tuần", "Báo cáo tháng"]);
        assert!(multi);
        let (options, multi) = super::clarification_options(
            &serde_json::json!({"question": "q", "options": "not a list", "multi": true}),
        );
        assert!(
            options.is_empty() && !multi,
            "no usable list: free text only"
        );
        let many: Vec<String> = (0..20).map(|i| format!("o{i}")).collect();
        assert_eq!(
            super::clarification_options(&serde_json::json!({"options": many}))
                .0
                .len(),
            8
        );
    }

    #[test]
    fn the_answer_text_is_read_out_of_a_partly_received_plan() {
        let none = super::partial_summary;
        assert_eq!(
            none(r#"{"thought":"x","actions":[{"tool":"shell.exec","args":{"summary":"nope"}}]}"#),
            None,
            "only task.complete streams"
        );
        assert_eq!(
            none(r#"{"thought":"t","actions":[{"tool":"task.complete","args":{"summ"#),
            None
        );
        assert_eq!(
            none(r#"{"actions":[{"tool":"task.complete","args":{"summary":"Xin ch"#).as_deref(),
            Some("Xin ch")
        );
        assert_eq!(
            none(r#"{"actions":[{"tool":"task.complete","args":{"summary":"Dòng 1\nDòng \"2\" và \u00e9 đ"#).as_deref(),
            Some("Dòng 1\nDòng \"2\" và é đ")
        );
        assert_eq!(
            none(r#"{"actions":[{"tool":"task.complete","args":{"summary":"cắt giữa \u00"#)
                .as_deref(),
            Some("cắt giữa ")
        );
        assert_eq!(
            none(r#"{"actions":[{"args":{"summary":"Xong."},"tool":"task.complete"}]}"#).as_deref(),
            Some("Xong.")
        );
        assert_eq!(
            none(r#"{"thought":"t","actions":[{"args":{"summary":"Đang viết dở"#).as_deref(),
            Some("Đang viết dở"),
            "args before tool"
        );
        assert_eq!(
            none(
                r#"{"actions":[{"args":{"summary":"Một tool khác"},"tool":"office.word_write"}]}"#
            ),
            None
        );
    }

    #[test]
    fn asking_permission_to_retry_or_to_do_more_is_recognised() {
        assert!(super::is_retry_permission(
            "Bạn có muốn tôi thử lại với nội dung tối giản hơn không?"
        ));
        assert!(super::is_retry_permission("Do you want me to try again?"));
        assert!(!super::is_retry_permission("Bạn muốn xóa file nào?"));
        assert!(super::is_closing_question("Tôi đã đưa dữ liệu vào Word. Bạn có muốn tôi gửi email thông báo hay thực hiện thêm thao tác nào khác không?", true));
        assert!(
            !super::is_closing_question("Bạn muốn tôi làm thêm bản PDF hay Excel?", false),
            "before any work it is a real question"
        );
        assert!(!super::is_closing_question(
            "Bạn muốn lưu báo cáo ở thư mục nào?",
            true
        ));
    }

    #[test]
    fn closing_pleasantries_are_not_questions() {
        assert!(super::is_closing_question("Tôi đang mở file 'qr-code (1).png' cho bạn. Bạn có cần tôi hỗ trợ gì thêm với file này không?", false));
        assert!(super::is_closing_question(
            "Do you need anything else?",
            false
        ));
        assert!(!super::is_closing_question(
            "Bạn muốn xóa những file nào trong Downloads?",
            true
        ));
    }

    #[test]
    fn failures_are_coached_to_change_approach_or_report_a_block() {
        let blocked = failed_step("Programmatic access to the VBA project is not trusted. Trust access to the VBA project object model");
        let n = super::failure_coaching(1, &blocked).expect("block notice");
        assert!(
            n.contains("blocks it") && n.contains("task.ask_user"),
            "{n}"
        );
        let plain = failed_step("KeyError: 'x'");
        assert!(super::failure_coaching(1, &plain).is_none());
        let n = super::failure_coaching(2, &plain).expect("second failure");
        assert!(n.contains("DIFFERENT"), "{n}");
        assert!(super::failure_coaching(4, &plain).is_none());
    }

    #[test]
    fn requests_load_the_tool_groups_they_mention() {
        let g = super::groups_for("Vẽ mặt bằng nhà cấp 4 trong AutoCAD");
        assert!(g.contains(&"autocad".to_string()) && !g.contains(&"browser".to_string()));
        let g = super::groups_for("Mở Notepad, gõ dòng chữ rồi lưu");
        assert!(g.contains(&"desktop".to_string()));
        let g = super::groups_for("vẽ doraemon bằng paint");
        assert!(
            g.contains(&"paint".to_string()) && !g.contains(&"autocad".to_string()),
            "{g:?}"
        );
        let g = super::groups_for("vẽ con mèo trong PowerPoint");
        assert!(
            g.contains(&"art".to_string()) && g.contains(&"office".to_string()),
            "{g:?}"
        );
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
        assert_eq!(
            normalize_completion(&json!({"summary": "hi"})),
            json!({"summary": "hi"})
        );
    }
}
