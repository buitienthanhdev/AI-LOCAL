//! TEST 6 against the real, running gateway (`cargo test -p lga-gateway --test live -- --ignored`):
//! kill the primary backend, check that failover is attempted and that the primary is restarted and
//! serves again.

use std::path::Path;
use std::time::{Duration, Instant};

use lga_core::config::Config;
use lga_core::secrets::resolve_secret;
use serde_json::{json, Value};

struct Client {
    http: reqwest::Client,
    base: String,
    key: String,
}

impl Client {
    fn new() -> Self {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let config = Config::load_from(&root).unwrap();
        let key = resolve_secret(&root, &config.gateway.api_key_secret).expect("gateway API key");
        let http = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(200)).build().unwrap();
        Self { http, base: config.llm.gateway_url.trim_end_matches('/').to_string(), key }
    }

    async fn get(&self, path: &str) -> Value {
        self.http.get(format!("{}{path}", self.base)).bearer_auth(&self.key).send().await.unwrap().json().await.unwrap()
    }

    async fn chat(&self, text: &str) -> (u16, Value) {
        let response = self
            .http
            .post(format!("{}/v1/chat", self.base))
            .bearer_auth(&self.key)
            .json(&json!({"messages": [{"role": "user", "content": text}]}))
            .send()
            .await
            .unwrap();
        (response.status().as_u16(), response.json().await.unwrap())
    }

    async fn primary_pid(&self) -> Option<u64> {
        let health = self.get("/v1/health").await;
        health["backends"].as_array()?.iter().find(|b| b["name"] == "primary")?["process"]["pid"].as_u64()
    }
}

#[tokio::test]
#[ignore]
async fn killed_primary_is_restarted_and_serves_again() {
    let client = Client::new();
    let before = client.primary_pid().await.expect("primary is managed and running");
    std::process::Command::new("taskkill").args(["/F", "/PID", &before.to_string()]).output().unwrap();
    tokio::time::sleep(Duration::from_secs(1)).await;

    let (status, body) = client.chat("Say OK").await;
    if status != 200 {
        // The fallback was tried after the primary failed (it may itself be unable to answer).
        let attempts = body["error"]["attempts"].as_array().unwrap();
        assert_eq!(attempts[0]["backend"], "primary");
        assert!(attempts.iter().any(|a| a["backend"] == "fallback"), "{body}");
    }

    let deadline = Instant::now() + Duration::from_secs(60);
    let after = loop {
        match client.primary_pid().await {
            Some(pid) if pid != before => break pid,
            _ if Instant::now() > deadline => panic!("primary was not restarted"),
            _ => tokio::time::sleep(Duration::from_secs(1)).await,
        }
    };
    println!("primary restarted: pid {before} -> {after}");
    let (status, body) = client.chat("Say OK").await;
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["backend"], "primary");
}
