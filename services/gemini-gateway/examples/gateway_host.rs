//! Phase H1 test host: a REAL `Gateway` (routing, breaker, health loop, `restart_if_dead`, `ensure_process`)
//! with exactly one route, the Chrome Gemini adapter, started through `ManagedProcess`. Nothing listens
//! and nothing is routed; it exists so a test driver can kill the adapter and watch the gateway react.
//!
//!   gateway_host <python> <source_dir> <python_agent_dir> <port> <profile> <logs> <selectors> <chrome>
//!
//! Commands on stdin: `health` (JSON of the route's health + process status), `chat` (one request through
//! the gateway; the test selectors make the adapter fail before any page input), `quit` (gateway.close()).

use std::collections::HashMap;
use std::io::BufRead;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use lga_core::redact::Redactor;
use lga_gateway::attachments::AttachmentStore;
use lga_gateway::backend::Backend;
use lga_gateway::gateway::{Gateway, Route};
use lga_gateway::resilience::{BackoffPolicy, BreakerSettings, CircuitBreaker};
use lga_gateway::supervisor::{KillOnCloseJob, ManagedProcess, ProcessSpec};
use lga_gateway::types::{ChatRequest, Message, Part};

#[tokio::main]
async fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    assert!(a.len() == 8 || a.len() == 9, "usage: gateway_host <python> <source_dir> <python_agent_dir> <port> <profile> <logs> <selectors> <chrome> [gemini_url]");
    let source = PathBuf::from(&a[1]);
    let shutdown_token = std::env::var("LGA_HOST_SHUTDOWN_TOKEN").unwrap_or_else(|_| "host-test-token".into());
    let chat_token = std::env::var("LGA_HOST_CHAT_TOKEN").unwrap_or_else(|_| "host-chat-token".into());
    let mut spec = ProcessSpec {
        name: "chrome_gemini_h1".into(),
        argv: vec![
            a[0].clone(), "-m".into(), "chrome_gemini".into(), "--port".into(), a[3].clone(), "--profile-dir".into(), a[4].clone(),
            "--log-dir".into(), a[5].clone(), "--selectors".into(), a[6].clone(), "--chrome".into(), a[7].clone(),
            "--shutdown-grace-s".into(), "1".into(),
        ],
        cwd: source.clone(),
        env: HashMap::from([
            ("PYTHONPATH".into(), format!("{};{}", source.display(), a[2])),
            ("PYTHONIOENCODING".into(), "utf-8".into()),
            ("LGA_ADAPTER_SHUTDOWN_TOKEN".into(), shutdown_token.clone()),
            ("LGA_ADAPTER_CHAT_TOKEN".into(), chat_token.clone()),
        ]),
        log_path: Some(PathBuf::from(&a[5]).join("adapter.log")),
    };
    if let Ok(model) = std::env::var("LGA_HOST_REQUIRE_MODEL") {
        spec.argv.extend(["--require-model".into(), model]); // H4B: send only on this exact model label
    }
    if let Some(url) = a.get(8) {
        spec.argv.extend(["--gemini-url".into(), url.clone()]); // tests only: a loopback stand-in page
    }
    let job = Arc::new(KillOnCloseJob::new().expect("job"));
    let process = ManagedProcess::new(spec, Arc::clone(&job), Arc::new(Redactor::new()))
        .with_graceful_shutdown(format!("http://127.0.0.1:{}/shutdown", a[3]), Duration::from_secs(25), Some(shutdown_token.clone()));
    let store = Arc::new(AttachmentStore::new(1_000_000, 5_000_000, Duration::from_secs(600)));
    let backend = Backend::chrome_gemini("chrome_gemini", &format!("http://127.0.0.1:{}", a[3]), Arc::clone(&store), Duration::from_secs(2))
        .with_adapter_token(Some(chat_token.clone()));
    let breaker = BreakerSettings { failure_threshold: 5, open_duration: Duration::from_secs(5), max_open_duration: Duration::from_secs(30) };
    let route = Route::new(backend, CircuitBreaker::new(breaker), Some(process), Duration::from_secs(30));
    let gateway = Gateway::new(
        vec![route],
        store,
        BackoffPolicy { max_attempts: 1, initial: Duration::from_millis(10), max: Duration::from_millis(20), multiplier: 2.0, jitter: 0.0 },
        Duration::from_secs(20),
        Duration::from_secs(1), // health loop every second so a restart is observed quickly
    );
    gateway.start().await;
    println!("{{\"event\":\"ready\",\"host_pid\":{}}}", std::process::id());
    let _keep_job_alive = job;
    let stdin = std::io::stdin();
    let mut lines = stdin.lock().lines();
    while let Some(Ok(line)) = lines.next() {
        match line.trim() {
            "health" => {
                let health = gateway.health(false).await;
                println!("{}", serde_json::to_string(&health).unwrap());
            }
            command if command == "chat" || command.starts_with("chat ") => {
                // `chat <timeout_ms> <prompt...>`; bare `chat` keeps the old fixed probe.
                let mut words = command.splitn(3, ' ').skip(1);
                let timeout_ms: u64 = words.next().and_then(|w| w.parse().ok()).unwrap_or(15_000).min(600_000); // test host cap: 600 s
                let prompt = words.next().unwrap_or("h1 probe").to_string();
                let request = ChatRequest {
                    messages: vec![Message { role: "user".into(), parts: vec![Part::Text(prompt)] }],
                    model: "auto".into(),
                    timeout: Some(Duration::from_millis(timeout_ms)),
                    metadata: HashMap::new(),
                    json: false,
                    schema: None,
                    tools: None,
                };
                let outcome = match gateway.chat(&request).await {
                    Ok(r) => serde_json::json!({"ok": true, "backend": r.backend, "content": r.content, "model": r.model,
                                                "usage": r.usage, "attempts": r.attempts.len()}),
                    Err(e) => serde_json::json!({"ok": false, "kind": e.kind.as_str(), "message": e.message,
                                                 "attempts": e.attempts.len(), "attempt_kinds": e.attempts.iter().map(|a| a.error_kind.clone()).collect::<Vec<_>>()}),
                };
                println!("{outcome}");
            }
            "quit" => {
                gateway.close();
                println!("{{\"event\":\"quit\"}}");
                return;
            }
            _ => println!("{{\"error\":\"unknown command\"}}"),
        }
    }
}
