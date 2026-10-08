//! `agent`: command-line interface of the Local Gemini Agent.

use std::io::{BufRead, IsTerminal, Write};
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};

use lga_core::agent::{Interaction, RunEvent, Runtime, TaskRunner};
use lga_core::cancel::{clear_pause, request_cancel, request_pause};
use lga_core::interaction::FileInteraction;
use lga_core::config::{Config, ExecutionMode};
use lga_core::doctor;
use lga_core::llm::{ChatProvider, GatewayProvider};
use lga_core::state::{new_task_id, LoadSource, StepStatus, TaskRecord, TaskStatus, TaskStore};

const OBSERVATION_PREVIEW_CHARS: usize = 220;

#[derive(Parser)]
#[command(name = "agent", version, about = "Local Gemini Agent: plans with Gemini, acts on Windows, verifies every step")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run a task described in natural language.
    Run {
        task: String,
        /// Execution mode: normal | trusted | full_access (default from config).
        #[arg(long)]
        mode: Option<String>,
        /// Approve actions the policy marks "confirm" without asking (never overrides "deny").
        #[arg(long)]
        yes: bool,
        /// Answer confirmations/questions through memory/tasks/<id>.request.json (used by apps/agent-ui).
        #[arg(long)]
        ui_channel: bool,
        /// Use this task id instead of generating one (lets a front-end know the id in advance).
        #[arg(long)]
        task_id: Option<String>,
    },
    /// Continue a stopped, failed or interrupted task from its saved state.
    Resume {
        task_id: String,
        #[arg(long)]
        yes: bool,
        #[arg(long)]
        ui_channel: bool,
    },
    /// Pause a running task before its next step.
    Pause { task_id: String },
    /// Let a paused task continue.
    Unpause { task_id: String },
    /// Show running and recent tasks, or one task in detail.
    Status { task_id: Option<String> },
    /// Request cancellation of a running task.
    Cancel { task_id: String },
    /// List past tasks.
    History {
        #[arg(long, default_value_t = 20)]
        limit: usize,
    },
    /// Check every subsystem and report PASS / WARN / FAIL.
    Doctor,
    /// List models offered by the Gemini gateway backends.
    Models,
    /// List the tools the planner can use.
    Tools {
        #[arg(long)]
        json: bool,
    },
    /// Capture the screen to artifacts/screenshots/.
    Screenshot,
    /// Manage the Gemini gateway process.
    Gateway {
        #[arg(value_parser = ["start", "stop", "status"])]
        action: String,
    },
}

struct ConsoleUi {
    auto_approve: bool,
    interactive: bool,
}

fn clip(text: &str, max: usize) -> String {
    let one_line = text.replace(['\r', '\n'], " ");
    if one_line.chars().count() <= max {
        one_line
    } else {
        format!("{}...", one_line.chars().take(max).collect::<String>())
    }
}

fn read_line(prompt: &str) -> Option<String> {
    print!("{prompt}");
    std::io::stdout().flush().ok()?;
    let mut line = String::new();
    std::io::stdin().lock().read_line(&mut line).ok()?;
    Some(line.trim().to_string())
}

impl Interaction for ConsoleUi {
    fn event(&self, event: RunEvent<'_>) {
        match event {
            RunEvent::Started(record) => {
                println!("Task {} [{}]: {}", record.task_id, record.mode.as_str(), record.goal);
            }
            RunEvent::Status(status, reason) => match reason {
                Some(reason) if status.is_terminal() || status == TaskStatus::NeedsUser => {
                    println!("== {}: {reason}", status.as_str())
                }
                _ => {}
            },
            RunEvent::Plan(plan) => {
                println!("Plan:");
                for (i, item) in plan.iter().enumerate() {
                    println!("  {}. {item}", i + 1);
                }
            }
            RunEvent::Thinking { step_id, thought, tool, args } => {
                if !thought.is_empty() {
                    println!("[{step_id}] {}", clip(thought, 300));
                }
                println!("[{step_id}] -> {tool} {}", clip(&args.to_string(), 200));
            }
            RunEvent::StepDone(step) => {
                let verdict = match (&step.status, &step.error) {
                    (_, Some(error)) => format!("{:?} {}: {}", step.status, error.code.as_str(), clip(&error.message, 200)),
                    (StepStatus::Succeeded, None) => format!("ok (verification {})", step.verification.as_deref().unwrap_or("n/a")),
                    (status, None) => format!("{status:?}"),
                };
                println!("[{}]    {verdict} in {} ms", step.step_id, step.duration_ms);
                if step.error.is_none() && !step.observation.is_empty() {
                    println!("[{}]    {}", step.step_id, clip(&step.observation, OBSERVATION_PREVIEW_CHARS));
                }
            }
            RunEvent::Notice(text) => println!("   ! {text}"),
        }
    }

    fn confirm(&self, tool: &str, args: &Value, reasons: &[String]) -> bool {
        if self.auto_approve {
            println!("   ! auto-approved {tool} ({})", reasons.join("; "));
            return true;
        }
        if !self.interactive {
            println!("   ! declined {tool}: needs confirmation and no terminal is attached ({})", reasons.join("; "));
            return false;
        }
        println!("   ? The agent wants to run {tool} {}", clip(&args.to_string(), 400));
        for reason in reasons {
            println!("     - {reason}");
        }
        matches!(read_line("     Allow? [y/N] ").as_deref(), Some("y" | "Y" | "yes"))
    }

    fn ask(&self, question: &str) -> Option<String> {
        if !self.interactive {
            return None;
        }
        println!("   ? {question}");
        read_line("     Your answer: ").filter(|a| !a.is_empty())
    }
}

async fn gateway(config: &Config, start: bool) -> Result<Arc<GatewayProvider>> {
    let provider = Arc::new(GatewayProvider::new(config)?);
    if start {
        provider.ensure_running(config).await?;
    }
    Ok(provider)
}

fn print_summary(record: &TaskRecord) {
    println!();
    println!("Task:     {}", record.task_id);
    println!("Status:   {}", record.status.as_str());
    println!("Steps:    {} ({} LLM calls, {} worker restarts)", record.steps.len(), record.llm_calls, record.worker_restarts);
    if let Some(summary) = &record.final_summary {
        println!("Result:   {summary}");
    }
    if let Some(reason) = &record.failure_reason {
        println!("Reason:   {reason}");
    }
    if let Some(question) = &record.pending_question {
        println!("Waiting:  {question}");
        println!("          answer it with: agent resume {}", record.task_id);
    }
    if let Some(escalation) = &record.escalation {
        println!("Escalation request (needs Administrator, not attempted):");
        println!("  {}", serde_json::to_string_pretty(escalation).unwrap_or_default().replace('\n', "\n  "));
    }
    let mut artifacts = record.artifacts.clone();
    artifacts.dedup();
    if !artifacts.is_empty() {
        println!("Artifacts:");
        for a in artifacts {
            println!("  {a}");
        }
    }
}

async fn run_task(config: Config, mut record: TaskRecord, yes: bool, ui_channel: bool) -> Result<ExitCode> {
    let llm = gateway(&config, true).await?;
    let tasks_dir = config.tasks_dir();
    let mut rt = Runtime::new(config, record.mode, llm)?;
    let console = ConsoleUi { auto_approve: yes, interactive: !ui_channel && std::io::stdin().is_terminal() };
    let ui: Box<dyn Interaction> = if ui_channel && !yes {
        Box::new(FileInteraction::new(&tasks_dir, &record.task_id, console))
    } else {
        Box::new(console)
    };
    let mut runner = TaskRunner::new(&mut rt, ui.as_ref(), &record)?;
    let token = runner.cancel_token();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            println!("\n   ! Ctrl+C: cancelling after the current action...");
            token.cancel();
        }
    });
    let result = runner.run(&mut record).await;
    drop(runner);
    rt.shutdown().await;
    print_summary(&record);
    result?;
    Ok(match record.status {
        TaskStatus::Completed => ExitCode::SUCCESS,
        TaskStatus::NeedsUser => ExitCode::from(3),
        TaskStatus::Cancelled => ExitCode::from(130),
        _ => ExitCode::FAILURE,
    })
}

fn store(config: &Config) -> TaskStore {
    TaskStore::new(config.tasks_dir(), config.logs_dir().join("tasks"))
}

fn print_task_line(record: &TaskRecord) {
    println!(
        "{}  {:<11} {:>3} steps  {}",
        record.task_id,
        record.status.as_str(),
        record.steps.len(),
        clip(&record.goal, 80)
    );
}

async fn main_inner() -> Result<ExitCode> {
    let cli = Cli::parse();
    let config = Config::load()?;
    match cli.command {
        Command::Run { task, mode, yes, ui_channel, task_id } => {
            let mode = match mode {
                Some(m) => ExecutionMode::parse(&m)?,
                None => config.agent.mode,
            };
            let task_id = match task_id {
                Some(id) if id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') && !id.is_empty() => id,
                Some(id) => bail!("invalid task id {id:?} (letters, digits and '-' only)"),
                None => new_task_id(),
            };
            if store(&config).load(&task_id).is_ok() {
                bail!("task {task_id} already exists; use `agent resume {task_id}`");
            }
            let record = TaskRecord::new(task_id, task, mode);
            run_task(config, record, yes, ui_channel).await
        }
        Command::Pause { task_id } => {
            let (record, _) = store(&config).load(&task_id)?;
            if record.status.is_terminal() {
                bail!("task {task_id} already {}", record.status.as_str());
            }
            request_pause(&config.tasks_dir(), &task_id)?;
            println!("Pause requested for {task_id}; it pauses before its next step.");
            Ok(ExitCode::SUCCESS)
        }
        Command::Unpause { task_id } => {
            clear_pause(&config.tasks_dir(), &task_id)?;
            println!("{task_id} may continue.");
            Ok(ExitCode::SUCCESS)
        }
        Command::Resume { task_id, yes, ui_channel } => {
            let (mut record, source) = store(&config).load(&task_id)?;
            match source {
                LoadSource::Primary => {}
                LoadSource::Backup => println!("   ! state file was unreadable; resumed from its backup copy"),
                LoadSource::AuditLog => println!("   ! state file and backup were unreadable; task rebuilt from the audit log"),
            }
            record.resume()?;
            println!("Resuming {} at step {} ({})", record.task_id, record.steps.len() + 1, record.goal);
            run_task(config, record, yes, ui_channel).await
        }
        Command::Status { task_id } => {
            let store = store(&config);
            match task_id {
                Some(id) => {
                    let (record, _) = store.load(&id)?;
                    for step in &record.steps {
                        let outcome = step.error.as_ref().map(|e| e.to_string()).unwrap_or_else(|| {
                            format!("ok, verification {}", step.verification.as_deref().unwrap_or("n/a"))
                        });
                        println!("{} {:<24} {:?} {}", step.step_id, step.tool, step.status, clip(&outcome, 120));
                    }
                    print_summary(&record);
                }
                None => {
                    let records = store.list()?;
                    let active: Vec<_> = records.iter().filter(|r| !r.status.is_terminal()).collect();
                    if active.is_empty() {
                        println!("No active tasks.");
                    } else {
                        println!("Active tasks:");
                        active.iter().for_each(|r| print_task_line(r));
                    }
                    if let Some(last) = records.iter().find(|r| r.status.is_terminal()) {
                        println!("Most recent finished task:");
                        print_task_line(last);
                    }
                }
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Cancel { task_id } => {
            let (record, _) = store(&config).load(&task_id)?;
            if record.status.is_terminal() {
                bail!("task {task_id} already {}", record.status.as_str());
            }
            let marker = request_cancel(&config.tasks_dir(), &task_id)?;
            println!("Cancellation requested for {task_id} ({}); the runner stops after the current action.", marker.display());
            Ok(ExitCode::SUCCESS)
        }
        Command::History { limit } => {
            for record in store(&config).list()?.iter().take(limit) {
                print_task_line(record);
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Doctor => {
            let llm = gateway(&config, false).await;
            let provider = match llm {
                Ok(p) => p,
                Err(e) => {
                    println!("FAIL  gemini-gateway       {e}");
                    return Ok(ExitCode::FAILURE);
                }
            };
            let mut rt = Runtime::new(config, ExecutionMode::FullAccess, provider.clone() as Arc<dyn ChatProvider>)?;
            let checks = doctor::run(&mut rt, &provider).await;
            rt.shutdown().await;
            for c in &checks {
                println!("{}  {:<20} {}", c.status.as_str(), c.name, c.detail);
            }
            let worst = checks.iter().map(|c| c.status).max().unwrap_or(doctor::Status::Pass);
            println!("\nOverall: {}", worst.as_str());
            Ok(if worst == doctor::Status::Fail { ExitCode::FAILURE } else { ExitCode::SUCCESS })
        }
        Command::Models => {
            let provider = gateway(&config, true).await?;
            for model in provider.list_models().await? {
                println!("{:<28} {:<10} {}", model.id, model.backend, model.description);
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Tools { json } => {
            let catalog = lga_core::tools::ToolCatalog::load(&config.path(&config.paths.tools_dir))?;
            if json {
                let all: Vec<&lga_core::tools::ToolSpec> = catalog.all().collect();
                println!("{}", serde_json::to_string_pretty(&all)?);
            } else {
                for spec in catalog.all() {
                    println!(
                        "{:<26} risk={:<8} verify={:<10} confirm={:<5} idempotent={:<5} {}",
                        spec.name,
                        format!("{:?}", spec.risk_level).to_lowercase(),
                        format!("{:?}", spec.verification).to_lowercase(),
                        spec.requires_confirmation,
                        spec.idempotent,
                        clip(&spec.description, 70)
                    );
                }
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Screenshot => {
            let provider = gateway(&config, false).await?;
            let mut rt = Runtime::new(config, ExecutionMode::Normal, provider)?;
            let response = rt.call_tool(&format!("cli-{}", new_task_id()), "desktop.screenshot", json!({})).await;
            rt.shutdown().await;
            let response = response?;
            let result = response.result.ok_or_else(|| anyhow!("{}", response.error.map(|e| e.to_string()).unwrap_or_default()))?;
            println!("{}", result.data["path"].as_str().unwrap_or_default());
            Ok(ExitCode::SUCCESS)
        }
        Command::Gateway { action } => {
            let provider = gateway(&config, false).await?;
            match action.as_str() {
                "start" => {
                    provider.ensure_running(&config).await?;
                    println!("gateway running at {}", config.llm.gateway_url);
                }
                "stop" => {
                    if provider.is_up().await {
                        provider.shutdown().await.context("stopping gateway")?;
                        println!("gateway stopping");
                    } else {
                        println!("gateway is not running");
                    }
                }
                _ => {
                    if provider.is_up().await {
                        println!("{}", serde_json::to_string_pretty(&provider.health().await?)?);
                    } else {
                        println!("gateway is not running at {}", config.llm.gateway_url);
                        return Ok(ExitCode::FAILURE);
                    }
                }
            }
            Ok(ExitCode::SUCCESS)
        }
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    match main_inner().await {
        Ok(code) => code,
        Err(err) => {
            eprintln!("error: {err:#}");
            ExitCode::FAILURE
        }
    }
}
