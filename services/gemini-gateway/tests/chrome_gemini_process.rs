//! Phase E4 smoke test: the gateway's ManagedProcess starts the real adapter, which answers /health and is
//! stopped again. Isolated: its own job object, a free loopback port, a scratch profile directory and a
//! selector file that does not exist, so the adapter fails closed and never starts or touches any Chrome.
//!
//! Needs the python-agent venv, so it is ignored by default:
//!   cargo test -p lga-gateway --test chrome_gemini_process -- --ignored

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use lga_core::redact::Redactor;
use lga_gateway::supervisor::{KillOnCloseJob, ManagedProcess, ProcessSpec};
use serde_json::Value;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap()
}

#[tokio::test]
#[ignore = "needs the python-agent venv; run explicitly"]
async fn managed_adapter_starts_answers_health_fails_closed_and_stops() {
    let root = root();
    let python = root.join("services/python-agent/.venv/Scripts/python.exe");
    assert!(python.is_file(), "python-agent venv missing");
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.local_addr().unwrap().port()
    };
    let scratch = std::env::temp_dir().join(format!("lga-e4-{port}"));
    let source = root.join("services/chrome-gemini");
    let spec = ProcessSpec {
        name: "chrome_gemini_e4".into(),
        argv: vec![
            python.to_string_lossy().into_owned(),
            "-m".into(),
            "chrome_gemini".into(),
            "--port".into(),
            port.to_string(),
            "--profile-dir".into(),
            scratch.join("profile").to_string_lossy().into_owned(),
            "--log-dir".into(),
            scratch.join("logs").to_string_lossy().into_owned(),
            "--selectors".into(),
            scratch.join("no-such-selectors.json").to_string_lossy().into_owned(),
        ],
        cwd: source.clone(),
        env: HashMap::from([
            ("PYTHONPATH".into(), format!("{};{}", source.display(), root.join("services/python-agent").display())),
            ("PYTHONIOENCODING".into(), "utf-8".into()),
            ("LGA_ADAPTER_SHUTDOWN_TOKEN".into(), "process-test-token".into()),
            ("LGA_ADAPTER_CHAT_TOKEN".into(), "process-chat-token".into()),
        ]),
        log_path: Some(scratch.join("adapter.log")),
    };
    let job = Arc::new(KillOnCloseJob::new().unwrap());
    let process = ManagedProcess::new(spec, job, Arc::new(Redactor::new()))
        .with_graceful_shutdown(format!("http://127.0.0.1:{port}/shutdown"), Duration::from_secs(15), Some("process-test-token".into()));
    process.start().unwrap();

    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    let health: Value = loop {
        assert!(process.is_running(), "adapter exited during startup (code {:?})", process.last_exit_code());
        if let Ok(response) = client.get(format!("http://127.0.0.1:{port}/health")).send().await {
            break response.json().await.unwrap();
        }
        assert!(Instant::now() < deadline, "adapter did not answer /health within 30s");
        tokio::time::sleep(Duration::from_millis(250)).await;
    };
    assert_eq!(health["state"], "degraded");
    assert_eq!(health["error_class"], "selectors_unverified");
    assert_eq!(health["capabilities"]["images"], false);

    // A chat must be refused before anything is sent.
    let chat: Value = client
        .post(format!("http://127.0.0.1:{port}/v1/chat"))
        .bearer_auth("process-chat-token")
        .json(&serde_json::json!({"prompt": "not sent", "transaction_id": "e4", "timeout_s": 5}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(chat["status"], "NOT_SUBMITTED");
    assert_eq!(chat["error"]["class"], "selectors_unverified");

    let stopping = Instant::now();
    process.stop();
    // The adapter ends itself when asked; being terminated would take the full wait only if it hung.
    assert!(stopping.elapsed() < Duration::from_secs(10), "graceful stop took {:?}", stopping.elapsed());
    assert_eq!(process.last_exit_code(), Some(0), "adapter should exit by itself, not be terminated");
    assert!(!process.is_running());
    assert!(client.get(format!("http://127.0.0.1:{port}/health")).send().await.is_err(), "adapter still answering after stop");
    // Only the adapter's own lock file may exist: no Chrome was started and no browser profile was created.
    let entries: Vec<String> = std::fs::read_dir(scratch.join("profile"))
        .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect())
        .unwrap_or_default();
    assert!(entries.iter().all(|n| n == "chrome-gemini-adapter.lock"), "unexpected profile contents: {entries:?}");
    let _ = std::fs::remove_dir_all(&scratch);
}


/// H2: the spawn spec shared by the extra process tests.
fn adapter_spec(port: u16, scratch: &std::path::Path, token: &str) -> ProcessSpec {
    let root = root();
    let source = root.join("services/chrome-gemini");
    ProcessSpec {
        name: "chrome_gemini_h2".into(),
        argv: vec![
            root.join("services/python-agent/.venv/Scripts/python.exe").to_string_lossy().into_owned(),
            "-m".into(),
            "chrome_gemini".into(),
            "--port".into(),
            port.to_string(),
            "--profile-dir".into(),
            scratch.join("profile").to_string_lossy().into_owned(),
            "--log-dir".into(),
            scratch.join("logs").to_string_lossy().into_owned(),
            "--selectors".into(),
            scratch.join("no-such-selectors.json").to_string_lossy().into_owned(),
            "--shutdown-grace-s".into(),
            "1".into(),
        ],
        cwd: source.clone(),
        env: HashMap::from([
            ("PYTHONPATH".into(), format!("{};{}", source.display(), root.join("services/python-agent").display())),
            ("PYTHONIOENCODING".into(), "utf-8".into()),
            ("LGA_ADAPTER_SHUTDOWN_TOKEN".into(), token.into()),
            ("LGA_ADAPTER_CHAT_TOKEN".into(), "process-chat-token".into()),
        ]),
        log_path: Some(scratch.join("adapter.log")),
    }
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

async fn wait_health(client: &reqwest::Client, port: u16) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while client.get(format!("http://127.0.0.1:{port}/health")).send().await.is_err() {
        assert!(Instant::now() < deadline, "adapter did not answer /health within 30s");
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

#[tokio::test]
#[ignore = "needs the python-agent venv; run explicitly"]
async fn shutdown_with_the_wrong_token_is_refused_and_stop_falls_back_to_terminate_quickly() {
    let port = free_port();
    let scratch = std::env::temp_dir().join(format!("lga-h2a-{port}"));
    let job = Arc::new(KillOnCloseJob::new().unwrap());
    // The gateway side holds the WRONG token: the adapter must refuse, and stop() must not wait out `wait`.
    let process = ManagedProcess::new(adapter_spec(port, &scratch, "right-token"), job, Arc::new(Redactor::new()))
        .with_graceful_shutdown(format!("http://127.0.0.1:{port}/shutdown"), Duration::from_secs(20), Some("wrong-token".into()));
    process.start().unwrap();
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    wait_health(&client, port).await;

    // Directly: no header, wrong header -> 403, adapter keeps serving.
    for request in [client.post(format!("http://127.0.0.1:{port}/shutdown")),
                    client.post(format!("http://127.0.0.1:{port}/shutdown")).bearer_auth("nope")] {
        assert_eq!(request.send().await.unwrap().status(), 403);
    }
    let health: Value = client.get(format!("http://127.0.0.1:{port}/health")).send().await.unwrap().json().await.unwrap();
    assert_ne!(health["state"], "shutting_down", "a refused shutdown must not change the adapter");

    let started = Instant::now();
    process.stop();
    assert!(started.elapsed() < Duration::from_secs(10), "stop waited {:?} for a refusing adapter", started.elapsed());
    assert!(!process.is_running());
    assert_ne!(process.last_exit_code(), Some(0), "it was terminated, not stopped by itself");
    assert!(client.get(format!("http://127.0.0.1:{port}/health")).send().await.is_err());
    let _ = std::fs::remove_dir_all(&scratch);
}

#[tokio::test]
#[ignore = "needs the python-agent venv; run explicitly"]
async fn gateway_stop_after_the_adapter_already_shut_itself_down_is_clean() {
    let port = free_port();
    let scratch = std::env::temp_dir().join(format!("lga-h2b-{port}"));
    let job = Arc::new(KillOnCloseJob::new().unwrap());
    let process = ManagedProcess::new(adapter_spec(port, &scratch, "right-token"), job, Arc::new(Redactor::new()))
        .with_graceful_shutdown(format!("http://127.0.0.1:{port}/shutdown"), Duration::from_secs(20), Some("right-token".into()));
    process.start().unwrap();
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    wait_health(&client, port).await;

    // Something with the token (here: the test) stops the adapter first; it answers, then exits by itself.
    let reply: Value = client
        .post(format!("http://127.0.0.1:{port}/shutdown"))
        .bearer_auth("right-token")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(reply["shutdown"], true);
    let deadline = Instant::now() + Duration::from_secs(10);
    while process.is_running() {
        assert!(Instant::now() < deadline, "adapter did not exit after a completed shutdown");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(process.last_exit_code(), Some(0));
    assert!(client.get(format!("http://127.0.0.1:{port}/health")).send().await.is_err(), "listener left behind");

    // The gateway then stops normally: nothing is running, nothing is killed, nothing panics.
    let started = Instant::now();
    process.stop();
    assert!(started.elapsed() < Duration::from_secs(5));
    assert!(!process.is_running());
    let _ = std::fs::remove_dir_all(&scratch);
}
