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
use crate::planner::{parse_reply, system_prompt, user_turn, PlannerAction, TurnContext};
use crate::policy::{Policy, Verdict};
use crate::redact::Redactor;
use crate::retry::Backoff;
use crate::secrets::resolve_secret;
use crate::state::{StepRecord, StepStatus, TaskRecord, TaskStatus, TaskStore};
use crate::tools::{ToolCatalog, ToolSpec, VerificationMode};
use crate::worker::{ToolCall, ToolResponse, WorkerClient, WorkerError};

/// Elements requested for the automatic observation after an unverified desktop action.
const OBSERVE_MAX_ELEMENTS: u64 = 80;
const OBSERVE_TIMEOUT: Duration = Duration::from_secs(30);
const MIN_TOOL_TIMEOUT: Duration = Duration::from_secs(1);

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
}

fn now_ms(started: Instant) -> u64 {
    started.elapsed().as_millis() as u64
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

            let Some(action) = self.plan_next(record).await? else { continue };
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
                "task.complete" => {
                    if self.verify_completion(record, &action).await? {
                        return Ok(());
                    }
                }
                "task.fail" => {
                    let reason = action.args.get("reason").and_then(Value::as_str).unwrap_or("planner gave up").to_string();
                    record.final_summary = Some(reason.clone());
                    return self.finish(record, TaskStatus::Failed, &reason);
                }
                "task.ask_user" => {
                    let question = action.args.get("question").and_then(Value::as_str).unwrap_or_default().to_string();
                    self.set_status(record, TaskStatus::NeedsUser, Some(&question))?;
                    match self.ui.ask(&question) {
                        Some(answer) => {
                            self.notices.push(format!("You asked: {question} The user answered: {answer}"));
                            record.pending_question = None;
                            self.set_status(record, TaskStatus::Planning, None)?;
                        }
                        None => {
                            record.pending_question = Some(question);
                            self.save(record)?;
                            return Ok(()); // paused: `agent resume <id>` continues after the user answers
                        }
                    }
                }
                _ => {
                    let outcome = self.execute_step(record, &action).await?;
                    self.after_step(record, outcome)?;
                    if record.status.is_terminal() {
                        return Ok(());
                    }
                }
            }
        }
    }

    /// Ask the planner for the next action. Returns None when the reply was unusable (counted as a failure).
    async fn plan_next(&mut self, record: &mut TaskRecord) -> Result<Option<PlannerAction>> {
        let agent_cfg = self.rt.config.agent.clone();
        let llm_cfg = self.rt.config.llm.clone();
        let max_recalled = self.rt.config.memory.max_recalled_items;
        let available: Vec<String> = self.rt.worker().await?.tools().to_vec();
        let system = system_prompt(&self.rt.catalog.render_for_prompt(&available), self.rt.policy.mode(), &agent_cfg.language);
        let recall = self.rt.memory.recall(&record.goal, max_recalled)?.render();
        let mut attachments = Vec::new();
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
            });
            let mut user = ChatMessage::text("user", turn);
            user.content.extend(attachments.iter().map(|id| Part::Image { attachment_id: id.clone() }));
            let request = ChatRequest {
                messages: vec![ChatMessage::text("system", system.clone()), user],
                model: llm_cfg.model.clone(),
                timeout: Duration::from_millis(llm_cfg.request_timeout_ms),
                metadata: HashMap::from([("task_id".into(), record.task_id.clone())]),
            };
            let reply = self.chat_with_retry(record, request).await?;
            match parse_reply(&reply) {
                Ok(action) => {
                    self.notices.clear();
                    self.latest_screenshots.clear();
                    return Ok(Some(action));
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
                    if !err.retryable() || attempt >= self.backoff.max_attempts {
                        return Err(anyhow!("planner unavailable: {err}"));
                    }
                    let delay = self.backoff.delay(attempt);
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
        StepOutcome { record: step, observation_for_planner: None, screenshot: None }
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
                let err = ToolErrorInfo::new(ErrorCode::PolicyDenied, decision.reasons.join("; "), false);
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
                    return Ok(StepOutcome { record: step, observation_for_planner: None, screenshot: None });
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
                    if !spec.idempotent || attempt >= self.backoff.max_attempts {
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

        let needs_observation = matches!(step.verification.as_deref(), Some("deferred"))
            || step.status == StepStatus::Failed && (spec.category == "desktop" || spec.category == "browser");
        let observation = if needs_observation && !self.cancel.is_cancelled() {
            self.set_status(record, if step.status == StepStatus::Failed { TaskStatus::Recovering } else { TaskStatus::Observing }, None)?;
            self.observe(record, spec).await
        } else {
            None
        };
        Ok(StepOutcome { record: step, observation_for_planner: observation, screenshot })
    }

    /// OBSERVE: capture the current UI so the planner can judge an unverified or failed action.
    async fn observe(&mut self, record: &TaskRecord, spec: &ToolSpec) -> Option<String> {
        let (tool, args) = if spec.category == "browser" {
            ("browser.get_state", json!({}))
        } else {
            ("desktop.inspect_ui", json!({"max_elements": OBSERVE_MAX_ELEMENTS}))
        };
        let worker = self.rt.worker().await.ok()?;
        if !worker.tools().iter().any(|t| t == tool) {
            return None;
        }
        let call = ToolCall::new(&record.task_id, "observe", tool, args, OBSERVE_TIMEOUT);
        match worker.call(&call).await {
            Ok(response) if response.is_ok() => response.result.map(|r| format!("(automatic {tool}) {}", r.observation)),
            Ok(response) => response.error.map(|e| format!("(automatic {tool} failed: {e})")),
            Err(err) => Some(format!("(automatic {tool} failed: {err})")),
        }
    }

    fn after_step(&mut self, record: &mut TaskRecord, outcome: StepOutcome) -> Result<()> {
        let step = outcome.record;
        self.audit.step(&step)?;
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

    /// VERIFY: run the planner's completion checks; complete only if all pass.
    async fn verify_completion(&mut self, record: &mut TaskRecord, action: &PlannerAction) -> Result<bool> {
        let violations = self.rt.catalog.validate_args("task.complete", &action.args);
        if !violations.is_empty() {
            self.notices.push(format!("task.complete arguments are invalid: {}", violations.join("; ")));
            record.consecutive_failures += 1;
            return Ok(false);
        }
        self.set_status(record, TaskStatus::Verifying, None)?;
        let checks = action.args["checks"].as_array().cloned().unwrap_or_default();
        let mut failures = Vec::new();
        let mut evidence = Vec::new();
        let state_changing: Vec<String> = record
            .steps
            .iter()
            .filter(|s| s.status == StepStatus::Succeeded)
            .filter(|s| self.rt.catalog.get(&s.tool).is_some_and(|spec| spec.changes_state()))
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
            let args = check["args"].clone();
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
        self.audit.record(AuditEvent {
            kind: "completion_check",
            tool: Some("task.complete".into()),
            verification_result: Some(if failures.is_empty() { "passed".into() } else { "failed".into() }),
            detail: json!({"summary": summary, "evidence": evidence, "failures": failures}),
            ..Default::default()
        })?;
        if failures.is_empty() {
            record.final_summary = Some(summary);
            if let Some(artifacts) = action.args.get("artifacts").and_then(Value::as_array) {
                record.artifacts.extend(artifacts.iter().filter_map(Value::as_str).map(String::from));
            }
            let procedure: Vec<String> = record.steps.iter().filter(|s| s.status == StepStatus::Succeeded).map(|s| s.tool.clone()).collect();
            self.rt.memory.record_procedure(&record.goal, &procedure.join(" -> "))?;
            self.finish(record, TaskStatus::Completed, "all completion checks passed")?;
            return Ok(true);
        }
        record.consecutive_failures += 1;
        self.notices.push(format!(
            "task.complete was REJECTED because these checks failed: {}. The task is not done; fix it or adjust the checks to what the user actually asked.",
            failures.join(" | ")
        ));
        self.set_status(record, TaskStatus::Planning, Some("completion checks failed"))?;
        Ok(false)
    }
}
