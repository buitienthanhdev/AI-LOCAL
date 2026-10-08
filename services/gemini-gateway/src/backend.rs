//! The two Gemini Web implementations, each run as a separate process and reached over its
//! OpenAI-compatible local HTTP API.
//!
//! * primary — gemini-web2api-go (https://github.com/zexadev/gemini-web2api-go, MIT): `/v1/chat/completions`,
//!   images as `image_url` data URLs, liveness `GET /` -> {"status": "ok"}, account state from the admin API.
//! * fallback — WebAI-to-API (https://github.com/Amm1rr/WebAI-to-API, MIT): the stateless endpoint (the
//!   caller owns the history); images must be `{"type": "file"}` parts (`image_url` is rejected with 422);
//!   sampling parameters are rejected with 400 so none are sent; errors use FastAPI's `{"detail": ...}`;
//!   503 means its Gemini session is not ready.
//! * local — Ollama's native `/api/chat` with a local model (default gemma4:12b, shared with Company
//!   Brain): the last resort when Gemini Web is down or Google blocks the account. The native API is used
//!   instead of `/v1` because only it accepts `num_ctx` (the OpenAI endpoint silently truncates prompts).

use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures::StreamExt;
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::attachments::AttachmentStore;
use crate::errors::{BackendError, ErrorKind};
use crate::types::{BackendState, Capabilities, ChatRequest, Message, ModelInfo, Part, StreamDelta};

/// Longest wait for one Gemini API model before trying the next candidate.
const API_MODEL_ATTEMPT: Duration = Duration::from_secs(25);
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
    Ollama { num_ctx: u32 },
    /// The official Gemini API through its OpenAI-compatible endpoint (optional; needs the user's key).
    /// `discovered_default` is the model that last answered (or the best candidate); `candidates` are the
    /// usable Flash models, best first, tried in turn when one is overloaded.
    GeminiApi { api_key: String, discovered_default: Mutex<Option<String>>, candidates: Mutex<Vec<String>> },
    /// A local OpenAI-compatible server (llama.cpp's llama-server, LM Studio, ...).
    OpenAiServer { discovered_default: Mutex<Option<String>> },
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

    pub fn gemini_api(name: &str, base_url: &str, model: &str, api_key: String, store: Arc<AttachmentStore>, connect: Duration) -> Self {
        // The API is remote: unlike the loopback backends it honours the system proxy settings.
        let mut backend = Self::new(name, base_url, model, Kind::GeminiApi { api_key, discovered_default: Mutex::new(None), candidates: Mutex::new(Vec::new()) }, store, connect);
        backend.client = reqwest::Client::builder().connect_timeout(connect).build().expect("HTTP client with static settings");
        backend
    }

    pub fn openai_server(name: &str, base_url: &str, model: &str, store: Arc<AttachmentStore>, connect: Duration) -> Self {
        Self::new(name, base_url, model, Kind::OpenAiServer { discovered_default: Mutex::new(None) }, store, connect)
    }

    pub fn local(name: &str, base_url: &str, model: &str, num_ctx: u32, store: Arc<AttachmentStore>, connect: Duration) -> Self {
        Self::new(name, base_url, model, Kind::Ollama { num_ctx }, store, connect)
    }

    pub fn kind_name(&self) -> &'static str {
        match self.kind {
            Kind::Web2ApiGo { .. } => "gemini_web2api_go",
            Kind::GeminiApi { .. } => "gemini_api",
            Kind::OpenAiServer { .. } => "openai_server",
            Kind::WebAiToApi { .. } => "webai_to_api",
            Kind::Ollama { .. } => "ollama",
        }
    }

    fn chat_path(&self) -> &'static str {
        match self.kind {
            Kind::Web2ApiGo { .. } => "/v1/chat/completions",
            Kind::GeminiApi { .. } => "/chat/completions",
            Kind::OpenAiServer { .. } => "/v1/chat/completions",
            Kind::WebAiToApi { .. } => "/v1/stateless/chat/completions",
            Kind::Ollama { .. } => "/api/chat",
        }
    }

    fn authorize(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.kind {
            Kind::Web2ApiGo { api_key: Some(key), .. } | Kind::GeminiApi { api_key: key, .. } => builder.bearer_auth(key),
            _ => builder,
        }
    }

    pub fn resolve_model(&self, requested: &str) -> String {
        if !requested.is_empty() && requested != "auto" {
            return requested.to_string();
        }
        match &self.kind {
            Kind::Web2ApiGo { .. } | Kind::Ollama { .. } => self.default_model.clone(),
            Kind::OpenAiServer { discovered_default } => {
                if !self.default_model.is_empty() && self.default_model != "auto" {
                    self.default_model.clone()
                } else {
                    discovered_default.lock().expect("model lock").clone().unwrap_or_default()
                }
            }
            Kind::GeminiApi { discovered_default, .. } => {
                if !self.default_model.is_empty() && self.default_model != "auto" {
                    self.default_model.clone()
                } else {
                    discovered_default.lock().expect("model lock").clone().unwrap_or_else(|| "gemini-flash-latest".into())
                }
            }
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
            Kind::Web2ApiGo { .. } | Kind::GeminiApi { .. } | Kind::OpenAiServer { .. } => {
                json!({"type": "image_url", "image_url": {"url": attachment.data_url()}})
            }
            Kind::Ollama { .. } => unreachable!("Ollama images are sent in the message's images field"),
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
        if let Kind::Ollama { num_ctx } = self.kind {
            // Same num_ctx as Company Brain's calls, so the shared model is not reloaded.
            payload["options"] = json!({"num_ctx": num_ctx});
            if request.json {
                payload["format"] = json!("json");
            }
        }
        if let (Kind::GeminiApi { .. } | Kind::OpenAiServer { .. }, true) = (&self.kind, request.json) {
            payload["response_format"] = json!({"type": "json_object"});
        }
        if !model.is_empty() {
            payload["model"] = json!(model);
        }
        Ok(payload)
    }

    fn message(&self, message: &Message) -> Result<Value, BackendError> {
        if let Kind::Ollama { .. } = self.kind {
            let mut images = Vec::new();
            for part in &message.parts {
                if let Part::Image(id) = part {
                    let attachment = self.attachments.get(id).map_err(|e| BackendError::new(ErrorKind::ClientError, e))?;
                    let url = attachment.data_url();
                    images.push(url.split_once(',').map_or(url.clone(), |(_, b64)| b64.to_string()));
                }
            }
            let mut value = json!({"role": message.role, "content": message.text()});
            if !images.is_empty() {
                value["images"] = json!(images);
            }
            return Ok(value);
        }
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
            Kind::GeminiApi { .. } | Kind::OpenAiServer { .. } => {
                // {"error": {...}} or [{"error": {...}}]
                let error = parsed.get("error").or_else(|| parsed.pointer("/0/error")).cloned().unwrap_or(Value::Null);
                let message = error.get("message").and_then(Value::as_str).map(String::from).unwrap_or_else(|| body.to_string());
                let lowered = message.to_lowercase();
                match status {
                    401 | 403 => BackendError::with_status(ErrorKind::Config, format!("{name}: API key rejected: {message}"), status),
                    400 if lowered.contains("api key") => BackendError::with_status(ErrorKind::Config, format!("{name}: API key rejected: {message}"), status),
                    429 => BackendError::with_status(ErrorKind::RateLimited, format!("{name}: quota or rate limit reached: {message}"), status),
                    // Anything else the API refuses may still work on another backend.
                    400..=499 => BackendError::with_status(ErrorKind::Capability, format!("{name}: {message}"), status),
                    _ => BackendError::with_status(ErrorKind::BackendDown, format!("{name}: error {status}: {message}"), status),
                }
            }
            Kind::Ollama { .. } => {
                let message = parsed.get("error").and_then(Value::as_str).map(String::from).unwrap_or_else(|| body.to_string());
                match status {
                    404 => BackendError::with_status(ErrorKind::Capability, format!("{name}: {message}"), status),
                    400 => BackendError::with_status(ErrorKind::ClientError, format!("{name}: {message}"), status),
                    429 | 503 => BackendError::with_status(ErrorKind::RateLimited, format!("{name}: busy: {message}"), status),
                    _ => BackendError::with_status(ErrorKind::BackendDown, format!("{name}: error {status}: {message}"), status),
                }
            }
            Kind::Web2ApiGo { .. } => {
                let error = parsed.get("error").cloned().unwrap_or(Value::Null);
                let message = error.get("message").and_then(Value::as_str).map(String::from).unwrap_or_else(|| body.to_string());
                let code = error.get("code").and_then(Value::as_str).unwrap_or("");
                let lowered = message.to_lowercase();
                if lowered.contains("google.com/sorry") || lowered.contains("upstream http 302") || lowered.contains("upstream http 405") {
                    // Google answers with its "unusual traffic" page: the account/IP is throttled for a while.
                    return BackendError::with_status(ErrorKind::BackendDown, format!("{name}: Google is temporarily blocking this account (unusual traffic); try again later"), status);
                }
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
        // reqwest's Display hides the cause ("error sending request"); the source chain names it.
        let mut detail = error.to_string();
        let mut source = std::error::Error::source(error);
        while let Some(cause) = source {
            detail.push_str(&format!(": {cause}"));
            source = cause.source();
        }
        BackendError::new(ErrorKind::BackendDown, format!("{}: {detail}", self.name))
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
        // Gemini API with model "auto": a model that is overloaded (503 "high demand"), rate limited or gone
        // is skipped for the next candidate; the one that answers becomes the default.
        if let Kind::GeminiApi { discovered_default, candidates, .. } = &self.kind {
            let auto = request.model.is_empty() || request.model == "auto";
            let mut order: Vec<String> = candidates.lock().expect("model lock").clone();
            if let Some(current) = discovered_default.lock().expect("model lock").clone() {
                order.retain(|m| *m != current);
                order.insert(0, current);
            }
            if auto && order.len() > 1 {
                let deadline = std::time::Instant::now() + timeout;
                let mut last = None;
                let tries = order.len().min(5);
                for (i, model) in order.into_iter().take(tries).enumerate() {
                    let remaining = deadline.saturating_duration_since(std::time::Instant::now());
                    if remaining.is_zero() {
                        break;
                    }
                    // While other models remain, one that hangs must not use up the whole budget.
                    let budget = if i + 1 < tries { remaining.min(API_MODEL_ATTEMPT) } else { remaining };
                    let mut attempt = request.clone();
                    attempt.model = model.clone();
                    match self.chat_once(&attempt, budget).await {
                        Ok(reply) => {
                            *discovered_default.lock().expect("model lock") = Some(model);
                            return Ok(reply);
                        }
                        Err(e) if matches!(e.status, Some(404 | 429 | 500 | 503)) || e.message.ends_with("request timed out") => {
                            last = Some(e)
                        }
                        Err(e) => return Err(e),
                    }
                }
                return Err(last.unwrap_or_else(|| BackendError::new(ErrorKind::BackendDown, format!("{}: request timed out", self.name))));
            }
        }
        self.chat_once(request, timeout).await
    }

    async fn chat_once(&self, request: &ChatRequest, timeout: Duration) -> Result<Reply, BackendError> {
        let model = self.resolve_model(&request.model);
        let response = self.post(request, false, timeout).await?;
        let text = response.text().await.map_err(|e| self.transport_error(&e))?;
        let data: Value = serde_json::from_str(&text)
            .map_err(|_| BackendError::new(ErrorKind::Transient, format!("{}: response is not JSON", self.name)))?;
        let pointer = if matches!(self.kind, Kind::Ollama { .. }) { "/message" } else { "/choices/0/message" };
        let choice = data
            .pointer(pointer)
            .ok_or_else(|| BackendError::new(ErrorKind::Transient, format!("{}: unexpected response shape", self.name)))?;
        let content = choice.get("content").and_then(Value::as_str).unwrap_or("").to_string();
        if content.trim().is_empty() {
            return Err(BackendError::new(ErrorKind::Transient, format!("{}: empty reply from the model", self.name)));
        }
        let usage = match self.kind {
            Kind::Ollama { .. } => Some(json!({"prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count")})),
            _ => data.get("usage").cloned().filter(|u| !u.is_null()),
        };
        Ok(Reply {
            content,
            reasoning: choice.get("reasoning_content").and_then(Value::as_str).map(String::from),
            model: data.get("model").and_then(Value::as_str).filter(|m| !m.is_empty()).map(String::from).unwrap_or(model),
            usage,
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
                    if let Kind::Ollama { .. } = self.kind {
                        // NDJSON: {"message": {"content": ...}, "done": bool}
                        if line.is_empty() {
                            continue;
                        }
                        let event: Value = serde_json::from_str(&line)
                            .map_err(|_| BackendError::new(ErrorKind::Transient, format!("{}: malformed stream line", self.name)))?;
                        if let Some(error) = event.get("error") {
                            return Err(BackendError::new(ErrorKind::Transient, format!("{}: {}", self.name, truncate(&error.to_string()))));
                        }
                        let text = event.pointer("/message/content").and_then(Value::as_str).unwrap_or("").to_string();
                        if !text.is_empty() {
                            emitted = true;
                            if tx.send(StreamDelta { content: text, reasoning: None }).await.is_err() {
                                return Ok(());
                            }
                        }
                        if event.get("done") == Some(&Value::Bool(true)) {
                            return if emitted { Ok(()) } else { Err(BackendError::new(ErrorKind::Transient, format!("{}: empty streamed reply", self.name))) };
                        }
                        continue;
                    }
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
            Kind::GeminiApi { .. } | Kind::OpenAiServer { .. } => match self.list_models().await {
                Ok(models) if !models.is_empty() => Ok(ProbeResult {
                    state: BackendState::Ready,
                    detail: format!("{}, model {}", if matches!(self.kind, Kind::GeminiApi { .. }) { "Gemini API" } else { "local server" }, self.resolve_model("auto")),
                    capabilities: Capabilities { chat: true, images: true },
                }),
                Ok(_) => Ok(ProbeResult { state: BackendState::Unavailable, detail: "Gemini API lists no models".into(), capabilities: Capabilities::NONE }),
                Err(e) => Ok(ProbeResult { state: BackendState::Unavailable, detail: e.message, capabilities: Capabilities::NONE }),
            },
            Kind::Ollama { .. } => {
                let models = self.list_models().await?;
                let wanted = self.default_model.as_str();
                let found = models.iter().any(|m| m.id == wanted || m.id == format!("{wanted}:latest"));
                Ok(if found {
                    ProbeResult { state: BackendState::Ready, detail: format!("local model {wanted}"), capabilities: Capabilities { chat: true, images: true } }
                } else {
                    ProbeResult {
                        state: BackendState::Unavailable,
                        detail: format!("Ollama is running but {wanted} is not installed (ollama pull {wanted})"),
                        capabilities: Capabilities::NONE,
                    }
                })
            }
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
            Kind::Ollama { .. } => ("/api/tags", PRIMARY_PROBE_TIMEOUT),
            Kind::Web2ApiGo { .. } => ("/v1/models", PRIMARY_PROBE_TIMEOUT),
            Kind::WebAiToApi { .. } => ("/v1/stateless/models", FALLBACK_PROBE_TIMEOUT),
            Kind::GeminiApi { .. } => ("/models", FALLBACK_PROBE_TIMEOUT),
            Kind::OpenAiServer { .. } => ("/v1/models", PRIMARY_PROBE_TIMEOUT),
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
            other => other.get("data").or_else(|| other.get("models")),
        };
        let models: Vec<ModelInfo> = items
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|m| {
                let id = m.get("id").or_else(|| m.get("name"))?;
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
        if let Kind::OpenAiServer { discovered_default } = &self.kind {
            let mut slot = discovered_default.lock().expect("model lock");
            if slot.is_none() {
                *slot = models.first().map(|m| m.id.clone());
            }
        }
        if let Kind::GeminiApi { discovered_default, candidates, .. } = &self.kind {
            let ranked = ranked_flash_models(models.iter().map(|m| m.id.as_str()));
            let mut slot = discovered_default.lock().expect("model lock");
            if slot.is_none() {
                *slot = ranked.first().cloned();
            }
            *candidates.lock().expect("model lock") = ranked;
        }
        Ok(models)
    }
}

/// General-purpose Flash models of a Gemini API model list, best first: newer versions first, and at equal
/// version a stable model before a preview (fast and generous on the free tier).
pub fn ranked_flash_models<'a>(ids: impl Iterator<Item = &'a str>) -> Vec<String> {
    const EXCLUDE: [&str; 13] = ["image", "tts", "audio", "live", "embedding", "thinking", "exp", "omni", "robotics",
                                 "computer-use", "transcribe", "customtools", "nano"];
    // Lite models come last: weaker planners, but still far better than none when the others are overloaded.
    let mut ranked: Vec<(bool, f64, bool, String)> = ids
        .map(|id| id.trim_start_matches("models/"))
        .filter(|id| id.starts_with("gemini-") && id.contains("flash") && !EXCLUDE.iter().any(|x| id.contains(x)))
        .map(|id| {
            let version: f64 = id["gemini-".len()..].split('-').next().and_then(|v| v.parse().ok()).unwrap_or(0.0);
            (!id.contains("lite"), version, !id.contains("preview"), id.to_string())
        })
        .collect();
    ranked.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.total_cmp(&a.1)).then(b.2.cmp(&a.2)));
    ranked.into_iter().map(|(_, _, _, id)| id).collect()
}

pub fn best_flash_model<'a>(ids: impl Iterator<Item = &'a str>) -> Option<String> {
    ranked_flash_models(ids).into_iter().next()
}

#[cfg(test)]
mod tests {
    use super::best_flash_model;

    #[test]
    fn picks_newest_stable_flash() {
        let ids = ["models/gemini-2.5-flash", "models/gemini-3.1-flash-preview-05", "models/gemini-3.1-flash", "models/gemini-3.1-flash-lite",
                   "models/gemini-3.5-pro", "models/gemini-3.1-flash-image", "models/text-embedding-004"];
        assert_eq!(best_flash_model(ids.into_iter()).as_deref(), Some("gemini-3.1-flash"));
        assert_eq!(super::ranked_flash_models(ids.into_iter()).last().map(String::as_str), Some("gemini-3.1-flash-lite"));
        assert_eq!(best_flash_model(["models/gemma-3"].into_iter()), None);
        let ranked = super::ranked_flash_models(["models/gemini-3.5-flash", "models/gemini-3.8-flash", "models/gemini-flash-latest",
                                                 "models/gemini-omni-1.1-flash", "models/gemini-3-flash-preview"].into_iter());
        assert_eq!(ranked, ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest"]);
    }
}
