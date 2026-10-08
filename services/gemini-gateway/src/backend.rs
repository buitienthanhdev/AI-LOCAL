//! The two Gemini Web implementations, each run as a separate process and reached over its
//! OpenAI-compatible local HTTP API.
//!
//! * primary — gemini-web2api-go (https://github.com/zexadev/gemini-web2api-go, MIT): `/v1/chat/completions`,
//!   images as `image_url` data URLs, liveness `GET /` -> {"status": "ok"}, account state from the admin API.
//! * fallback — WebAI-to-API (https://github.com/Amm1rr/WebAI-to-API, MIT): the stateless endpoint (the
//!   caller owns the history); images must be `{"type": "file"}` parts (`image_url` is rejected with 422);
//!   sampling parameters are rejected with 400 so none are sent; errors use FastAPI's `{"detail": ...}`;
//!   503 means its Gemini session is not ready.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures::StreamExt;
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::attachments::AttachmentStore;
use crate::errors::{BackendError, ErrorKind};
use crate::types::{BackendState, Capabilities, ChatRequest, Message, ModelInfo, Part, StreamDelta};

/// Upstream error bodies are truncated before they are logged or returned.
pub const MAX_ERROR_BODY_CHARS: usize = 500;
const PRIMARY_PROBE_TIMEOUT: Duration = Duration::from_secs(5);
const FALLBACK_PROBE_TIMEOUT: Duration = Duration::from_secs(10);
/// Phrases in primary 400 responses that mean "this backend cannot do it", not "your request is wrong".
const CAPABILITY_MARKERS: [&str; 6] =
    ["needs a google account cookie", "requires login", "unknown model", "not available", "model not found", "unsupported model"];

#[derive(Debug, Clone)]
pub struct ProbeResult {
    pub state: BackendState,
    pub detail: String,
    pub capabilities: Capabilities,
}

pub enum Kind {
    Web2ApiGo { api_key: Option<String>, admin_token: Option<String> },
    WebAiToApi { discovered_default: Mutex<Option<String>> },
}

#[derive(Debug, Clone)]
pub struct Reply {
    pub content: String,
    pub reasoning: Option<String>,
    pub model: String,
    pub usage: Option<Value>,
}

pub struct Backend {
    pub name: String,
    pub base_url: String,
    pub default_model: String,
    kind: Kind,
    attachments: Arc<AttachmentStore>,
    client: reqwest::Client,
}

fn truncate(text: &str) -> String {
    text.chars().take(MAX_ERROR_BODY_CHARS).collect()
}

impl Backend {
    pub fn new(
        name: &str,
        base_url: &str,
        default_model: &str,
        kind: Kind,
        attachments: Arc<AttachmentStore>,
        connect_timeout: Duration,
    ) -> Self {
        // no_proxy: never route localhost traffic through HTTP(S)_PROXY.
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(connect_timeout)
            .build()
            .expect("HTTP client with static settings");
        Self {
            name: name.to_string(),
            base_url: base_url.trim_end_matches('/').to_string(),
            default_model: default_model.to_string(),
            kind,
            attachments,
            client,
        }
    }

    pub fn primary(name: &str, base_url: &str, default_model: &str, store: Arc<AttachmentStore>, connect: Duration,
                   api_key: Option<String>, admin_token: Option<String>) -> Self {
        Self::new(name, base_url, default_model, Kind::Web2ApiGo { api_key, admin_token }, store, connect)
    }

    pub fn fallback(name: &str, base_url: &str, default_model: &str, store: Arc<AttachmentStore>, connect: Duration) -> Self {
        Self::new(name, base_url, default_model, Kind::WebAiToApi { discovered_default: Mutex::new(None) }, store, connect)
    }

    pub fn kind_name(&self) -> &'static str {
        match self.kind {
            Kind::Web2ApiGo { .. } => "gemini_web2api_go",
            Kind::WebAiToApi { .. } => "webai_to_api",
        }
    }

    fn chat_path(&self) -> &'static str {
        match self.kind {
            Kind::Web2ApiGo { .. } => "/v1/chat/completions",
            Kind::WebAiToApi { .. } => "/v1/stateless/chat/completions",
        }
    }

    fn authorize(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.kind {
            Kind::Web2ApiGo { api_key: Some(key), .. } => builder.bearer_auth(key),
            _ => builder,
        }
    }

    pub fn resolve_model(&self, requested: &str) -> String {
        if !requested.is_empty() && requested != "auto" {
            return requested.to_string();
        }
        match &self.kind {
            Kind::Web2ApiGo { .. } => self.default_model.clone(),
            Kind::WebAiToApi { discovered_default } => {
                if !self.default_model.is_empty() {
                    self.default_model.clone()
                } else {
                    discovered_default.lock().expect("model lock").clone().unwrap_or_default()
                }
            }
        }
    }

    fn image_part(&self, attachment_id: &str) -> Result<Value, BackendError> {
        let attachment = self
            .attachments
            .get(attachment_id)
            .map_err(|e| BackendError::new(ErrorKind::ClientError, e))?;
        Ok(match self.kind {
            Kind::Web2ApiGo { .. } => json!({"type": "image_url", "image_url": {"url": attachment.data_url()}}),
            Kind::WebAiToApi { .. } => {
                let ext = match attachment.mime_type.as_str() {
                    "image/jpeg" => "jpg",
                    "image/webp" => "webp",
                    "image/gif" => "gif",
                    _ => "png",
                };
                json!({"type": "file", "file": {"filename": format!("{}.{ext}", attachment.attachment_id), "file_data": attachment.data_url()}})
            }
        })
    }

    fn payload(&self, request: &ChatRequest, model: &str, stream: bool) -> Result<Value, BackendError> {
        let mut messages = Vec::with_capacity(request.messages.len());
        for message in &request.messages {
            messages.push(self.message(message)?);
        }
        let mut payload = json!({"messages": messages, "stream": stream});
        if !model.is_empty() {
            payload["model"] = json!(model);
        }
        Ok(payload)
    }

    fn message(&self, message: &Message) -> Result<Value, BackendError> {
        if !message.has_images() {
            return Ok(json!({"role": message.role, "content": message.text()}));
        }
        let mut content = Vec::new();
        for part in &message.parts {
            content.push(match part {
                Part::Text(text) => json!({"type": "text", "text": text}),
                Part::Image(id) => self.image_part(id)?,
            });
        }
        Ok(json!({"role": message.role, "content": content}))
    }

    pub fn classify(&self, status: u16, body: &str) -> BackendError {
        let parsed: Value = serde_json::from_str(body).unwrap_or(Value::Null);
        let name = &self.name;
        match self.kind {
            Kind::Web2ApiGo { .. } => {
                let error = parsed.get("error").cloned().unwrap_or(Value::Null);
                let message = error.get("message").and_then(Value::as_str).map(String::from).unwrap_or_else(|| body.to_string());
                let code = error.get("code").and_then(Value::as_str).unwrap_or("");
                let lowered = message.to_lowercase();
                match status {
                    429 => BackendError::with_status(ErrorKind::RateLimited, format!("{name}: rate limited: {message}"), status),
                    401 | 403 => BackendError::with_status(ErrorKind::Config, format!("{name}: local API key rejected"), status),
                    400 if code == "context_length_exceeded" => {
                        BackendError::with_status(ErrorKind::ClientError, format!("{name}: prompt too long: {message}"), status)
                    }
                    400 if CAPABILITY_MARKERS.iter().any(|m| lowered.contains(m)) => {
                        BackendError::with_status(ErrorKind::Capability, format!("{name}: {message}"), status)
                    }
                    400..=499 => BackendError::with_status(ErrorKind::ClientError, format!("{name}: {message}"), status),
                    _ => BackendError::with_status(ErrorKind::BackendDown, format!("{name}: upstream error {status}: {message}"), status),
                }
            }
            Kind::WebAiToApi { .. } => {
                let message = match parsed.get("detail") {
                    Some(Value::String(s)) => s.clone(),
                    Some(other) => other.to_string(),
                    None => body.to_string(),
                };
                match status {
                    429 => BackendError::with_status(ErrorKind::RateLimited, format!("{name}: rate limited: {message}"), status),
                    503 => BackendError::with_status(ErrorKind::BackendDown, format!("{name}: Gemini session not ready: {message}"), status),
                    404 if message.to_lowercase().contains("model") => {
                        BackendError::with_status(ErrorKind::Capability, format!("{name}: {message}"), status)
                    }
                    400 | 422 => BackendError::with_status(ErrorKind::ClientError, format!("{name}: {message}"), status),
                    401 | 403 => BackendError::with_status(ErrorKind::Config, format!("{name}: {message}"), status),
                    _ => BackendError::with_status(ErrorKind::BackendDown, format!("{name}: upstream error {status}: {message}"), status),
                }
            }
        }
    }

    fn transport_error(&self, error: &reqwest::Error) -> BackendError {
        if error.is_timeout() {
            return BackendError::new(ErrorKind::BackendDown, format!("{}: request timed out", self.name));
        }
        BackendError::new(ErrorKind::BackendDown, format!("{}: {error}", self.name))
    }

    async fn post(&self, request: &ChatRequest, stream: bool, timeout: Duration) -> Result<reqwest::Response, BackendError> {
        let model = self.resolve_model(&request.model);
        let payload = self.payload(request, &model, stream)?;
        let response = self
            .authorize(self.client.post(format!("{}{}", self.base_url, self.chat_path())))
            .timeout(timeout)
            .json(&payload)
            .send()
            .await
            .map_err(|e| self.transport_error(&e))?;
        let status = response.status().as_u16();
        if status >= 400 {
            let body = response.text().await.unwrap_or_default();
            return Err(self.classify(status, &truncate(&body)));
        }
        Ok(response)
    }

    pub async fn chat(&self, request: &ChatRequest, timeout: Duration) -> Result<Reply, BackendError> {
        let model = self.resolve_model(&request.model);
        let response = self.post(request, false, timeout).await?;
        let text = response.text().await.map_err(|e| self.transport_error(&e))?;
        let data: Value = serde_json::from_str(&text)
            .map_err(|_| BackendError::new(ErrorKind::Transient, format!("{}: response is not JSON", self.name)))?;
        let choice = data
            .pointer("/choices/0/message")
            .ok_or_else(|| BackendError::new(ErrorKind::Transient, format!("{}: unexpected response shape", self.name)))?;
        let content = choice.get("content").and_then(Value::as_str).unwrap_or("").to_string();
        if content.trim().is_empty() {
            return Err(BackendError::new(ErrorKind::Transient, format!("{}: empty reply from Gemini", self.name)));
        }
        Ok(Reply {
            content,
            reasoning: choice.get("reasoning_content").and_then(Value::as_str).map(String::from),
            model: data.get("model").and_then(Value::as_str).filter(|m| !m.is_empty()).map(String::from).unwrap_or(model),
            usage: data.get("usage").cloned().filter(|u| !u.is_null()),
        })
    }

    /// Stream deltas into `tx`. Returns whether anything was emitted alongside the outcome, so the caller
    /// knows whether failing over is still safe.
    pub async fn stream(&self, request: &ChatRequest, timeout: Duration, tx: &mpsc::Sender<StreamDelta>) -> (bool, Result<(), BackendError>) {
        let mut emitted = false;
        let result = async {
            let response = self.post(request, true, timeout).await?;
            let mut body = response.bytes_stream();
            let mut buffer: Vec<u8> = Vec::new();
            while let Some(chunk) = body.next().await {
                buffer.extend_from_slice(&chunk.map_err(|e| self.transport_error(&e))?);
                while let Some(pos) = buffer.iter().position(|&b| b == b'\n') {
                    let line: Vec<u8> = buffer.drain(..=pos).collect();
                    let line = String::from_utf8_lossy(&line).trim().to_string();
                    let Some(data) = line.strip_prefix("data:") else { continue };
                    let data = data.trim();
                    if data == "[DONE]" {
                        if !emitted {
                            return Err(BackendError::new(ErrorKind::Transient, format!("{}: empty streamed reply", self.name)));
                        }
                        return Ok(());
                    }
                    let event: Value = serde_json::from_str(data)
                        .map_err(|_| BackendError::new(ErrorKind::Transient, format!("{}: malformed SSE event", self.name)))?;
                    if let Some(error) = event.get("error") {
                        return Err(BackendError::new(ErrorKind::Transient, format!("{}: {}", self.name, truncate(&error.to_string()))));
                    }
                    for choice in event.get("choices").and_then(Value::as_array).into_iter().flatten() {
                        let delta = choice.get("delta").cloned().unwrap_or(Value::Null);
                        let text = delta.get("content").and_then(Value::as_str).unwrap_or("").to_string();
                        let reasoning = delta.get("reasoning_content").and_then(Value::as_str).map(String::from);
                        if !text.is_empty() || reasoning.is_some() {
                            emitted = true;
                            if tx.send(StreamDelta { content: text, reasoning }).await.is_err() {
                                return Ok(()); // the caller went away
                            }
                        }
                    }
                }
            }
            Err(BackendError::new(ErrorKind::Transient, format!("{}: stream ended without [DONE]", self.name)))
        }
        .await;
        (emitted, result)
    }

    async fn get_json(&self, path: &str, timeout: Duration, bearer: Option<&str>) -> Result<(u16, Value), BackendError> {
        let mut builder = self.client.get(format!("{}{path}", self.base_url)).timeout(timeout);
        builder = match bearer {
            Some(token) => builder.bearer_auth(token),
            None => self.authorize(builder),
        };
        let response = builder.send().await.map_err(|e| self.transport_error(&e))?;
        let status = response.status().as_u16();
        let text = response.text().await.map_err(|e| self.transport_error(&e))?;
        let value = if text.is_empty() {
            Value::Null
        } else {
            serde_json::from_str(&text).unwrap_or_else(|_| Value::String(truncate(&text)))
        };
        Ok((status, value))
    }

    /// Whether an enabled, healthy Google account cookie is configured on the primary.
    /// Uses its admin API (`GET /admin/api/cookies` -> {"items", ...}); None when it cannot be asked.
    async fn has_account_cookie(&self, admin_token: Option<&str>) -> Option<bool> {
        let token = admin_token?;
        let (status, data) = self.get_json("/admin/api/cookies", PRIMARY_PROBE_TIMEOUT, Some(token)).await.ok()?;
        if status != 200 {
            return None;
        }
        let items = data.get("items")?.as_array()?;
        Some(items.iter().any(|item| {
            item.get("status").and_then(Value::as_str) == Some("enabled") && item.get("health").and_then(Value::as_str) != Some("dead")
        }))
    }

    pub async fn probe(&self) -> Result<ProbeResult, BackendError> {
        match &self.kind {
            Kind::Web2ApiGo { admin_token, .. } => {
                let (status, body) = self.get_json("/", PRIMARY_PROBE_TIMEOUT, None).await?;
                if status != 200 || body.get("status").and_then(Value::as_str) != Some("ok") {
                    return Ok(ProbeResult {
                        state: BackendState::Unavailable,
                        detail: format!("liveness returned {status}"),
                        capabilities: Capabilities::NONE,
                    });
                }
                let has_cookie = self.has_account_cookie(admin_token.as_deref()).await;
                let capabilities = Capabilities { chat: true, images: has_cookie == Some(true) };
                let version = body.get("version").and_then(Value::as_str).unwrap_or("?");
                Ok(match has_cookie {
                    Some(false) => ProbeResult {
                        state: BackendState::Degraded,
                        detail: format!("v{version}: anonymous mode (no Google account cookie): text only"),
                        capabilities,
                    },
                    None => ProbeResult { state: BackendState::Ready, detail: format!("v{version}: account status unknown"), capabilities },
                    Some(true) => ProbeResult { state: BackendState::Ready, detail: format!("v{version}: account cookie present"), capabilities },
                })
            }
            Kind::WebAiToApi { .. } => {
                let (status, _) = self.get_json("/health", FALLBACK_PROBE_TIMEOUT, None).await?;
                if status != 200 {
                    return Ok(ProbeResult {
                        state: BackendState::Unavailable,
                        detail: format!("liveness returned {status}"),
                        capabilities: Capabilities::NONE,
                    });
                }
                Ok(match self.list_models().await {
                    Err(e) => ProbeResult {
                        state: BackendState::Degraded,
                        detail: format!("alive but not signed in to Gemini: {}", e.message),
                        capabilities: Capabilities::NONE,
                    },
                    Ok(models) if models.is_empty() => ProbeResult {
                        state: BackendState::Degraded,
                        detail: "alive but reports no models (sign-in required)".into(),
                        capabilities: Capabilities::NONE,
                    },
                    Ok(models) => ProbeResult {
                        state: BackendState::Ready,
                        detail: format!("{} models available", models.len()),
                        capabilities: Capabilities { chat: true, images: true },
                    },
                })
            }
        }
    }

    pub async fn list_models(&self) -> Result<Vec<ModelInfo>, BackendError> {
        let (path, timeout) = match self.kind {
            Kind::Web2ApiGo { .. } => ("/v1/models", PRIMARY_PROBE_TIMEOUT),
            Kind::WebAiToApi { .. } => ("/v1/stateless/models", FALLBACK_PROBE_TIMEOUT),
        };
        let (status, body) = self.get_json(path, timeout, None).await?;
        if status != 200 {
            let text = match &body {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            return Err(self.classify(status, &text));
        }
        let items = match &body {
            Value::Array(_) => Some(&body),
            other => other.get("data"),
        };
        let models: Vec<ModelInfo> = items
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|m| {
                let id = m.get("id")?;
                let id = id.as_str().map(String::from).unwrap_or_else(|| id.to_string());
                (!id.is_empty()).then(|| ModelInfo {
                    id,
                    backend: self.name.clone(),
                    description: m.get("description").and_then(Value::as_str).unwrap_or("").to_string(),
                })
            })
            .collect();
        if let Kind::WebAiToApi { discovered_default } = &self.kind {
            let mut slot = discovered_default.lock().expect("model lock");
            if slot.is_none() {
                *slot = models.first().map(|m| m.id.clone());
            }
        }
        Ok(models)
    }
}
