//! `ChatProvider`: the only LLM interface the agent core depends on.
//!
//! Implementations: `GatewayProvider` (Gemini Web through services/gemini-gateway). Future providers
//! (Gemini API, Claude API, OpenAI API, Ollama, vLLM) implement the same trait; see
//! docs/architecture.md "Model routing". The core never talks to a Gemini backend directly.

use std::collections::HashMap;
use std::path::Path;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use thiserror::Error;
use tokio::sync::mpsc;

use crate::config::Config;
use crate::secrets::resolve_secret;

const HEALTH_TIMEOUT: Duration = Duration::from_secs(5);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Part {
    Text { text: String },
    Image { attachment_id: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: Vec<Part>,
}

impl ChatMessage {
    pub fn text(role: &str, text: impl Into<String>) -> Self {
        Self { role: role.into(), content: vec![Part::Text { text: text.into() }] }
    }
}

#[derive(Debug, Clone)]
pub struct ChatRequest {
    pub messages: Vec<ChatMessage>,
    pub model: String,
    pub timeout: Duration,
    pub metadata: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChatReply {
    pub content: String,
    #[serde(default)]
    pub reasoning: Option<String>,
    pub model: String,
    pub backend: String,
    pub latency_ms: u64,
    #[serde(default)]
    pub attempts: Vec<Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ModelInfo {
    pub id: String,
    pub backend: String,
    #[serde(default)]
    pub description: String,
}

#[derive(Debug, Clone)]
pub enum StreamEvent {
    Backend(String),
    Delta(String),
    Done,
}

#[derive(Debug, Error)]
pub enum LlmError {
    /// The request itself was rejected (too long, malformed). Retrying unchanged will not help.
    #[error("provider rejected the request: {0}")]
    Client(String),
    /// No backend could serve the request (all down / breakers open / auth missing).
    #[error("no LLM backend available: {0}")]
    Unavailable(String),
    #[error("LLM provider configuration error: {0}")]
    Config(String),
    #[error("LLM transport error: {0}")]
    Transport(String),
}

impl LlmError {
    pub fn retryable(&self) -> bool {
        matches!(self, LlmError::Unavailable(_) | LlmError::Transport(_))
    }
}

#[async_trait]
pub trait ChatProvider: Send + Sync {
    fn name(&self) -> &str;
    async fn chat(&self, request: ChatRequest) -> Result<ChatReply, LlmError>;
    async fn stream(&self, request: ChatRequest) -> Result<mpsc::Receiver<Result<StreamEvent, LlmError>>, LlmError>;
    async fn upload(&self, path: &Path) -> Result<String, LlmError>;
    async fn health(&self) -> Result<Value, LlmError>;
    async fn list_models(&self) -> Result<Vec<ModelInfo>, LlmError>;
}

pub struct GatewayProvider {
    client: reqwest::Client,
    base_url: String,
    api_key: String,
}

fn request_body(request: &ChatRequest) -> Value {
    json!({
        "messages": request.messages,
        "model": request.model,
        "timeout_ms": request.timeout.as_millis() as u64,
        "metadata": request.metadata,
    })
}

async fn error_from_response(response: reqwest::Response) -> LlmError {
    let status = response.status();
    let body: Value = response.json().await.unwrap_or(Value::Null);
    let message = body
        .pointer("/error/message")
        .and_then(Value::as_str)
        .map(String::from)
        .unwrap_or_else(|| format!("HTTP {status}"));
    let attempts = body.pointer("/error/attempts").map(|a| a.to_string()).unwrap_or_default();
    let detail = if attempts.is_empty() || attempts == "[]" { message } else { format!("{message}; attempts: {attempts}") };
    match status.as_u16() {
        400 => LlmError::Client(detail),
        401 | 403 => LlmError::Config(format!("gateway rejected the API key: {detail}")),
        _ => LlmError::Unavailable(detail),
    }
}

impl GatewayProvider {
    pub fn new(config: &Config) -> Result<Self, LlmError> {
        let api_key = resolve_secret(&config.root, &config.gateway.api_key_secret).ok_or_else(|| {
            LlmError::Config(format!(
                "secret {} is not configured; run scripts/setup-gateway.ps1",
                config.gateway.api_key_secret
            ))
        })?;
        // The gateway is on loopback: never route through a system or environment proxy.
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(HEALTH_TIMEOUT)
            .build()
            .map_err(|e| LlmError::Config(e.to_string()))?;
        Ok(Self { client, base_url: config.llm.gateway_url.trim_end_matches('/').to_string(), api_key })
    }

    pub fn api_key(&self) -> &str {
        &self.api_key
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }

    /// Unauthenticated liveness: true when the gateway process answers.
    pub async fn is_up(&self) -> bool {
        matches!(
            self.client.get(self.url("/health")).timeout(HEALTH_TIMEOUT).send().await,
            Ok(r) if r.status().is_success()
        )
    }

    /// Ask a running gateway to stop (it stops its managed backends too).
    pub async fn shutdown(&self) -> Result<(), LlmError> {
        let response = self
            .client
            .post(self.url("/v1/admin/shutdown"))
            .bearer_auth(&self.api_key)
            .timeout(HEALTH_TIMEOUT)
            .send()
            .await
            .map_err(|e| LlmError::Transport(e.to_string()))?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(error_from_response(response).await)
        }
    }

    /// Start the gateway as a detached background process if it is not running, then wait for it.
    pub async fn ensure_running(&self, config: &Config) -> Result<(), LlmError> {
        if self.is_up().await {
            return Ok(());
        }
        if !config.llm.gateway_autostart {
            return Err(LlmError::Unavailable(format!(
                "gateway at {} is not running and autostart is disabled (scripts/start-gateway.ps1)",
                self.base_url
            )));
        }
        spawn_gateway(config)?;
        let deadline = Instant::now() + Duration::from_millis(config.llm.gateway_startup_timeout_ms);
        while Instant::now() < deadline {
            if self.is_up().await {
                return Ok(());
            }
            tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
        }
        Err(LlmError::Unavailable(format!(
            "gateway did not become healthy within {} ms; see logs/gateway/",
            config.llm.gateway_startup_timeout_ms
        )))
    }
}

fn spawn_gateway(config: &Config) -> Result<(), LlmError> {
    let python = config.path(&config.gateway.python);
    if !python.is_file() {
        return Err(LlmError::Config(format!(
            "{} not found; run scripts/setup-gateway.ps1",
            python.display()
        )));
    }
    let root = config.root.to_string_lossy().into_owned();
    crate::process::spawn_detached(
        &python,
        &["-X", "utf8", "-m", "lga_gateway"],
        &config.root,
        &[("LGA_ROOT", root.as_str()), ("PYTHONIOENCODING", "utf-8")],
    )
    .map(|_| ())
    .map_err(|e| LlmError::Config(format!("starting gateway: {e}")))
}

#[async_trait]
impl ChatProvider for GatewayProvider {
    fn name(&self) -> &str {
        "gemini-gateway"
    }

    async fn chat(&self, request: ChatRequest) -> Result<ChatReply, LlmError> {
        let response = self
            .client
            .post(self.url("/v1/chat"))
            .bearer_auth(&self.api_key)
            .timeout(request.timeout + HEALTH_TIMEOUT)
            .json(&request_body(&request))
            .send()
            .await
            .map_err(|e| LlmError::Transport(e.to_string()))?;
        if !response.status().is_success() {
            return Err(error_from_response(response).await);
        }
        response.json::<ChatReply>().await.map_err(|e| LlmError::Transport(format!("bad gateway reply: {e}")))
    }

    async fn stream(&self, request: ChatRequest) -> Result<mpsc::Receiver<Result<StreamEvent, LlmError>>, LlmError> {
        let response = self
            .client
            .post(self.url("/v1/chat/stream"))
            .bearer_auth(&self.api_key)
            .timeout(request.timeout + HEALTH_TIMEOUT)
            .json(&request_body(&request))
            .send()
            .await
            .map_err(|e| LlmError::Transport(e.to_string()))?;
        if !response.status().is_success() {
            return Err(error_from_response(response).await);
        }
        let (tx, rx) = mpsc::channel(64);
        tokio::spawn(async move {
            let mut body = response.bytes_stream();
            let mut buffer = String::new();
            let mut event = String::new();
            while let Some(chunk) = body.next().await {
                let chunk = match chunk {
                    Ok(c) => c,
                    Err(e) => {
                        let _ = tx.send(Err(LlmError::Transport(e.to_string()))).await;
                        return;
                    }
                };
                buffer.push_str(&String::from_utf8_lossy(&chunk));
                while let Some(pos) = buffer.find('\n') {
                    let line = buffer[..pos].trim_end_matches('\r').to_string();
                    buffer.drain(..=pos);
                    if let Some(name) = line.strip_prefix("event: ") {
                        event = name.to_string();
                    } else if let Some(data) = line.strip_prefix("data: ") {
                        let payload: Value = serde_json::from_str(data).unwrap_or(Value::Null);
                        let item = match event.as_str() {
                            "meta" => Ok(StreamEvent::Backend(
                                payload.get("backend").and_then(Value::as_str).unwrap_or_default().into(),
                            )),
                            "delta" => Ok(StreamEvent::Delta(
                                payload.get("content").and_then(Value::as_str).unwrap_or_default().into(),
                            )),
                            "done" => Ok(StreamEvent::Done),
                            "error" => Err(LlmError::Unavailable(
                                payload.get("message").and_then(Value::as_str).unwrap_or("stream error").into(),
                            )),
                            _ => continue,
                        };
                        if tx.send(item).await.is_err() {
                            return;
                        }
                    }
                }
            }
        });
        Ok(rx)
    }

    async fn upload(&self, path: &Path) -> Result<String, LlmError> {
        let response = self
            .client
            .post(self.url("/v1/upload"))
            .bearer_auth(&self.api_key)
            .timeout(Duration::from_secs(60))
            .json(&json!({"path": path.to_string_lossy()}))
            .send()
            .await
            .map_err(|e| LlmError::Transport(e.to_string()))?;
        if !response.status().is_success() {
            return Err(error_from_response(response).await);
        }
        let body: Value = response.json().await.map_err(|e| LlmError::Transport(e.to_string()))?;
        body.get("attachment_id")
            .and_then(Value::as_str)
            .map(String::from)
            .ok_or_else(|| LlmError::Transport("upload reply without attachment_id".into()))
    }

    async fn health(&self) -> Result<Value, LlmError> {
        let response = self
            .client
            .get(self.url("/v1/health?refresh=1"))
            .bearer_auth(&self.api_key)
            .timeout(Duration::from_secs(60))
            .send()
            .await
            .map_err(|e| LlmError::Transport(e.to_string()))?;
        if !response.status().is_success() {
            return Err(error_from_response(response).await);
        }
        response.json().await.map_err(|e| LlmError::Transport(e.to_string()))
    }

    async fn list_models(&self) -> Result<Vec<ModelInfo>, LlmError> {
        let response = self
            .client
            .get(self.url("/v1/models"))
            .bearer_auth(&self.api_key)
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .map_err(|e| LlmError::Transport(e.to_string()))?;
        if !response.status().is_success() {
            return Err(error_from_response(response).await);
        }
        let body: Value = response.json().await.map_err(|e| LlmError::Transport(e.to_string()))?;
        serde_json::from_value(body.get("models").cloned().unwrap_or(Value::Array(vec![])))
            .map_err(|e| LlmError::Transport(e.to_string()))
    }
}
