//! `agent doctor`: one explicit PASS / WARN / FAIL line per subsystem.

use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::agent::Runtime;
use crate::llm::{ChatProvider, GatewayProvider};

const IPC_PING_TIMEOUT: Duration = Duration::from_secs(5);
/// A worker round trip slower than this is reported as WARN.
const IPC_SLOW_MS: u128 = 200;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Status {
    Pass,
    Warn,
    Fail,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Pass => "PASS",
            Status::Warn => "WARN",
            Status::Fail => "FAIL",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Check {
    pub name: &'static str,
    pub status: Status,
    pub detail: String,
}

fn check(name: &'static str, status: Status, detail: impl Into<String>) -> Check {
    Check { name, status, detail: detail.into() }
}

fn writable(dir: &std::path::Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let probe = dir.join(format!(".doctor-{}", std::process::id()));
    std::fs::write(&probe, b"ok").map_err(|e| e.to_string())?;
    std::fs::remove_file(&probe).map_err(|e| e.to_string())
}

async fn tool_data(rt: &mut Runtime, tool: &str, args: Value) -> Result<(Value, String), String> {
    match rt.call_tool("doctor", tool, args).await {
        Ok(response) if response.is_ok() => {
            let result = response.result.ok_or("empty result")?;
            Ok((result.data, result.observation))
        }
        Ok(response) => Err(response.error.map(|e| e.to_string()).unwrap_or_else(|| "unknown error".into())),
        Err(err) => Err(format!("{err:#}")),
    }
}

pub async fn run(rt: &mut Runtime, gateway: &GatewayProvider) -> Vec<Check> {
    let mut checks = Vec::new();
    let cfg = rt.config.clone();

    for (name, dir) in [("logs", cfg.logs_dir()), ("filesystem", cfg.artifacts_dir())] {
        checks.push(match writable(&dir) {
            Ok(()) => check(name, Status::Pass, format!("{} is writable", dir.display())),
            Err(e) => check(name, Status::Fail, format!("{}: {e}", dir.display())),
        });
    }
    checks.push(match (rt.memory.integrity_check(), writable(&cfg.tasks_dir())) {
        (Ok(result), Ok(())) if result == "ok" => {
            check("memory", Status::Pass, format!("SQLite {} ok; task store writable", cfg.memory.persistent_db))
        }
        (Ok(result), _) => check("memory", Status::Fail, format!("integrity_check: {result}")),
        (Err(e), _) => check("memory", Status::Fail, e.to_string()),
    });

    let contract_tools: Vec<String> = rt.catalog.all().filter(|s| !s.is_core_tool()).map(|s| s.name.clone()).collect();
    checks.push(check("rust-core", Status::Pass, format!("v{} - {} tools in contract, policy mode {}", env!("CARGO_PKG_VERSION"), contract_tools.len(), rt.policy.mode().as_str())));

    let started = Instant::now();
    let worker = match rt.worker().await {
        Ok(worker) => {
            let served = worker.tools().len();
            let missing: Vec<&String> = contract_tools.iter().filter(|t| !worker.tools().contains(t)).collect();
            let status = if missing.is_empty() { Status::Pass } else { Status::Warn };
            let detail = if missing.is_empty() {
                format!("pid {} ready in {} ms, serves all {served} tools", worker.pid(), started.elapsed().as_millis())
            } else {
                format!("pid {} serves {served} tools; not available: {}", worker.pid(), missing.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "))
            };
            checks.push(check("python-worker", status, detail));
            Some(worker)
        }
        Err(e) => {
            checks.push(check("python-worker", Status::Fail, format!("{e:#}")));
            None
        }
    };
    match &worker {
        Some(worker) => checks.push(match worker.ping(IPC_PING_TIMEOUT).await {
            Ok(rtt) if rtt.as_millis() <= IPC_SLOW_MS => check("ipc", Status::Pass, format!("stdio JSON-lines round trip {} ms", rtt.as_millis())),
            Ok(rtt) => check("ipc", Status::Warn, format!("round trip {} ms (slow)", rtt.as_millis())),
            Err(e) => check("ipc", Status::Fail, e.to_string()),
        }),
        None => checks.push(check("ipc", Status::Fail, "no worker")),
    }

    if worker.is_some() {
        checks.push(match tool_data(rt, "desktop.list_windows", json!({})).await {
            Ok((data, _)) => {
                let n = data["windows"].as_array().map(Vec::len).unwrap_or(0);
                check("windows-automation", Status::Pass, format!("UI Automation via Windows-Use: {n} windows visible"))
            }
            Err(e) => check("windows-automation", Status::Fail, e),
        });
        checks.push(match tool_data(rt, "shell.exec", json!({"command": "$PSVersionTable.PSVersion.ToString()", "timeout_ms": 30000})).await {
            Ok((data, _)) if data["exit_code"] == 0 => check("powershell", Status::Pass, format!("PowerShell {}", data["stdout"].as_str().unwrap_or("").trim())),
            Ok((data, _)) => check("powershell", Status::Fail, format!("exit code {}: {}", data["exit_code"], data["stderr"])),
            Err(e) => check("powershell", Status::Fail, e),
        });
        match tool_data(rt, "system.environment", json!({})).await {
            Ok((env, _)) => {
                checks.push(if env["chrome"]["found"] == true {
                    check("chrome", Status::Pass, format!("{} at {}", env["chrome"]["version"].as_str().unwrap_or("?"), env["chrome"]["path"].as_str().unwrap_or("?")))
                } else {
                    check("chrome", Status::Fail, "Chrome not found (set [browser] chrome_path)")
                });
                let installs = env["autocad"]["installations"].as_array().cloned().unwrap_or_default();
                checks.push(match (installs.first(), env["autocad"]["com_registered"] == true) {
                    (Some(first), true) => check("autocad", Status::Pass, format!(
                        "{} ({}) at {}; COM registered; {}",
                        first["release"].as_str().unwrap_or("?"),
                        first["version"].as_str().unwrap_or("?"),
                        first["location"].as_str().unwrap_or("?"),
                        if env["autocad"]["running"] == true { "running" } else { "not running" }
                    )),
                    (Some(_), false) => check("autocad", Status::Warn, format!("installed but {} is not registered for COM", cfg.autocad.prog_id)),
                    (None, _) => check("autocad", Status::Warn, "AutoCAD is not installed; autocad.* tools will fail"),
                });
                if env["user"]["is_admin"] == true {
                    checks.push(check("privileges", Status::Warn, "running elevated; the agent is designed to run with normal user rights"));
                } else {
                    checks.push(check("privileges", Status::Pass, "normal user rights (no elevation)"));
                }
            }
            Err(e) => checks.push(check("chrome", Status::Fail, format!("environment probe failed: {e}"))),
        }
        for (name, tool) in [("browser", "browser.status"), ("vision", "vision.status"), ("knowledge", "knowledge.status")] {
            let available = rt.worker().await.map(|w| w.tools().iter().any(|t| t == tool)).unwrap_or(false);
            checks.push(if !available {
                check(name, Status::Fail, format!("{tool} is not available in the worker"))
            } else {
                match tool_data(rt, tool, json!({})).await {
                    Ok((data, obs)) => check(name, if data["ready"] == true { Status::Pass } else { Status::Warn }, obs),
                    Err(e) => check(name, if name == "knowledge" { Status::Warn } else { Status::Fail }, e),
                }
            });
        }
    }

    if gateway.is_up().await {
        checks.push(match gateway.health().await {
            Ok(health) => {
                let backends = health["backends"].as_array().cloned().unwrap_or_default();
                let describe = |b: &Value| format!("{}={} ({})", b["name"].as_str().unwrap_or("?"), b["state"].as_str().unwrap_or("?"), b["detail"].as_str().unwrap_or(""));
                let chat_capable = backends.iter().any(|b| b["capabilities"]["chat"] == true);
                let all_ready = backends.iter().all(|b| b["state"] == "ready");
                let status = if all_ready { Status::Pass } else if chat_capable { Status::Warn } else { Status::Fail };
                check("gemini-gateway", status, backends.iter().map(describe).collect::<Vec<_>>().join("; "))
            }
            Err(e) => check("gemini-gateway", Status::Fail, e.to_string()),
        });
    } else {
        checks.push(check("gemini-gateway", Status::Fail, format!("not running at {} (start with scripts/start-gateway.ps1 or any `agent run`)", cfg.llm.gateway_url)));
    }
    checks
}
