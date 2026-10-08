//! Integration tests of the Rust <-> Python worker IPC against the real worker process.
//! Skipped (with a message) when the worker venv has not been created (`uv sync` in services/python-agent).

use std::path::PathBuf;
use std::time::Duration;

use lga_core::config::Config;
use lga_core::error::ErrorCode;
use lga_core::redact::Redactor;
use lga_core::worker::{ToolCall, WorkerClient, WorkerError};
use serde_json::json;

fn config() -> Option<Config> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().ok()?;
    let config = Config::load_from(&root).ok()?;
    if !config.path(&config.worker.python).is_file() {
        eprintln!("skipping: worker venv missing");
        return None;
    }
    Some(config)
}

#[tokio::test]
async fn worker_round_trip_verification_deadline_and_crash() {
    let Some(config) = config() else { return };
    let worker = WorkerClient::spawn(&config, Redactor::new()).await.expect("worker starts");
    assert!(worker.tools().iter().any(|t| t == "filesystem.write"));
    worker.ping(Duration::from_secs(5)).await.expect("ping");

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("ipc.txt");
    let write = ToolCall::new("rust-it", "s1", "filesystem.write",
        json!({"path": path.to_string_lossy(), "content": "from rust"}), Duration::from_secs(30));
    let response = worker.call(&write).await.expect("response");
    assert!(response.is_ok(), "{response:?}");
    assert_eq!(response.result.as_ref().unwrap().verification.status, "passed");
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "from rust");

    let slow = ToolCall::new("rust-it", "s2", "shell.exec",
        json!({"command": "Start-Sleep 30", "timeout_ms": 60000}), Duration::from_secs(2));
    let response = worker.call(&slow).await.expect("response");
    assert_eq!(response.error.as_ref().unwrap().code, ErrorCode::Timeout);
    assert!(response.metrics.duration_ms < 15_000);

    let invalid = ToolCall::new("rust-it", "s3", "filesystem.read", json!({"path": "relative.txt"}), Duration::from_secs(10));
    assert_eq!(worker.call(&invalid).await.unwrap().error.unwrap().code, ErrorCode::InvalidArgument);

    worker.kill().await;
    let after = ToolCall::new("rust-it", "s4", "filesystem.stat", json!({"path": path.to_string_lossy()}), Duration::from_secs(5));
    assert!(matches!(worker.call(&after).await, Err(WorkerError::Died)));
}
