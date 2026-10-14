//! Phase G test host: plays the gateway's part for lifecycle tests, with the gateway's own supervisor types.
//!
//! It creates the same `KillOnCloseJob` the gateway creates, starts the Chrome Gemini adapter through the
//! same `ManagedProcess`, and obeys commands on stdin so a test driver can reproduce a normal shutdown
//! (`quit`), an adapter-only stop (`stop`), a restart (`start`) or a crash (the driver terminates this
//! process). It is never the production gateway and listens on nothing.
//!
//!   cargo run -p lga-gateway --example lifecycle_host -- <python> <source_dir> <python_agent_dir> <port> <profile> <logs> <selectors> <chrome>
//!
//! Commands: `status`, `start`, `stop`, `quit`. Every reply is one line of JSON.

use std::collections::HashMap;
use std::io::BufRead;
use std::path::PathBuf;
use std::sync::Arc;

use lga_core::redact::Redactor;
use lga_gateway::supervisor::{KillOnCloseJob, ManagedProcess, ProcessSpec};

fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    assert!(a.len() == 8, "usage: lifecycle_host <python> <source_dir> <python_agent_dir> <port> <profile> <logs> <selectors> <chrome>");
    let source = PathBuf::from(&a[1]);
    let spec = ProcessSpec {
        name: "chrome_gemini_g".into(),
        argv: vec![
            a[0].clone(), "-m".into(), "chrome_gemini".into(), "--port".into(), a[3].clone(), "--profile-dir".into(), a[4].clone(),
            "--log-dir".into(), a[5].clone(), "--selectors".into(), a[6].clone(), "--chrome".into(), a[7].clone(),
        ],
        cwd: source.clone(),
        env: HashMap::from([
            ("PYTHONPATH".into(), format!("{};{}", source.display(), a[2])),
            ("PYTHONIOENCODING".into(), "utf-8".into()),
            ("LGA_ADAPTER_SHUTDOWN_TOKEN".into(), "host-test-token".into()),
            ("LGA_ADAPTER_CHAT_TOKEN".into(), "host-chat-token".into()),
        ]),
        log_path: Some(PathBuf::from(&a[5]).join("adapter.log")),
    };
    let job = Arc::new(KillOnCloseJob::new().expect("job"));
    let mut process = ManagedProcess::new(spec, Arc::clone(&job), Arc::new(Redactor::new()));
    if std::env::var("LGA_GRACEFUL").is_ok_and(|v| v == "1") {
        process = process.with_graceful_shutdown(format!("http://127.0.0.1:{}/shutdown", a[3]), std::time::Duration::from_secs(15), Some("host-test-token".into()));
    }
    process.start().expect("adapter start");
    println!("{{\"event\":\"ready\",\"host_pid\":{}}}", std::process::id());
    for line in std::io::stdin().lock().lines() {
        match line.unwrap_or_default().trim() {
            "status" => println!("{}", process.status()),
            "start" => println!("{{\"start\":{}}}", process.start().is_ok()),
            "stop" => {
                process.stop();
                println!("{{\"stopped\":true,\"running\":{}}}", process.is_running());
            }
            "quit" => {
                // What lga-gateway's main does on a normal shutdown: stop the backends, then drop the job.
                process.stop();
                drop(job);
                println!("{{\"event\":\"quit\"}}");
                return;
            }
            _ => println!("{{\"error\":\"unknown command\"}}"),
        }
    }
}
