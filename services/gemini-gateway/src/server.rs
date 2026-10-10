//! HTTP API of the gateway (loopback only). This is the provider the agent core and the worker talk to.
//!
//!   GET  /health               no auth; {"status": "ok" | "degraded" | "down"}
//!   GET  /v1/health            detailed backend health (?refresh=1 probes now)
//!   GET  /v1/models            models of all reachable backends
//!   POST /v1/upload            {"path": "..."} or {"data_base64": "...", "filename", "mime_type"}
//!   POST /v1/chat              ChatRequest JSON -> ChatResult JSON
//!   POST /v1/chat/stream       same body; Server-Sent Events: meta / delta / done / error
//!   POST /v1/admin/shutdown    stop the gateway and its managed backends
//!
//! Chat request body:
//!   {"messages": [{"role": "system|user|assistant",
//!                  "content": "text" | [{"type": "text", "text": "..."}, {"type": "image", "attachment_id": "att_..."}]}],
//!    "model": "auto", "timeout_ms": 120000, "metadata": {"task_id": "...", "step_id": "..."}}

use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, Query, Request, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::Engine;
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};

use crate::errors::{ErrorKind, GatewayError};
use crate::gateway::Gateway;
use crate::types::{Attempt, BackendState, ChatRequest, Message, Part};

/// Delay between answering the shutdown request and stopping, so the reply reaches the caller.
const SHUTDOWN_DELAY: Duration = Duration::from_millis(200);
const VALID_ROLES: [&str; 3] = ["system", "user", "assistant"];

#[derive(Clone)]
pub struct AppState {
    pub gateway: Arc<Gateway>,
    pub api_key: Arc<String>,
    pub stop: Arc<Notify>,
}

fn error(status: StatusCode, kind: &str, message: &str, attempts: &[Attempt]) -> Response {
    (status, Json(json!({"error": {"kind": kind, "message": message, "attempts": attempts}}))).into_response()
}

fn gateway_error(e: &GatewayError) -> Response {
    let status = if e.kind == ErrorKind::ClientError { StatusCode::BAD_REQUEST } else { StatusCode::SERVICE_UNAVAILABLE };
    error(status, e.kind.as_str(), &e.message, &e.attempts)
}

fn client_error(message: &str) -> Response {
    error(StatusCode::BAD_REQUEST, "client_error", message, &[])
}

/// Constant-time comparison of the supplied bearer token with the gateway API key.
fn token_matches(supplied: &str, expected: &str) -> bool {
    let (a, b) = (supplied.as_bytes(), expected.as_bytes());
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

async fn auth_and_access_log(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let started = Instant::now();
    let (method, path) = (request.method().clone(), request.uri().path().to_string());
    if path.starts_with("/v1/") {
        let supplied = request
            .headers()
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .unwrap_or("");
        if !token_matches(supplied, &state.api_key) {
            return error(StatusCode::UNAUTHORIZED, "unauthorized", "missing or invalid gateway API key", &[]);
        }
    }
    let response = next.run(request).await;
    crate::log::info(&format!("{method} {path} -> {} in {}ms", response.status().as_u16(), started.elapsed().as_millis()));
    response
}

pub fn parse_chat_request(body: &Value) -> Result<ChatRequest, String> {
    let object = body.as_object().ok_or("body must be a JSON object")?;
    let raw_messages = object.get("messages").and_then(Value::as_array).filter(|m| !m.is_empty()).ok_or("messages must be a non-empty list")?;
    let mut messages = Vec::with_capacity(raw_messages.len());
    for (index, raw) in raw_messages.iter().enumerate() {
        let role = raw.get("role").and_then(Value::as_str).filter(|r| VALID_ROLES.contains(r));
        let Some(role) = role else {
            return Err(format!("messages[{index}] needs a role in [\"assistant\", \"system\", \"user\"]"));
        };
        let parts = match raw.get("content") {
            Some(Value::String(text)) => vec![Part::Text(text.clone())],
            Some(Value::Array(items)) => {
                let mut parts = Vec::with_capacity(items.len());
                for part in items {
                    if !part.is_object() {
                        return Err(format!("messages[{index}].content items must be objects"));
                    }
                    match (part.get("type").and_then(Value::as_str), part.get("text"), part.get("attachment_id")) {
                        (Some("text"), Some(Value::String(text)), _) => parts.push(Part::Text(text.clone())),
                        (Some("image"), _, Some(Value::String(id))) => parts.push(Part::Image(id.clone())),
                        _ => return Err(format!("messages[{index}] has an unsupported content part")),
                    }
                }
                parts
            }
            _ => return Err(format!("messages[{index}].content must be a string or a list")),
        };
        messages.push(Message { role: role.to_string(), parts });
    }
    let timeout = match object.get("timeout_ms") {
        None | Some(Value::Null) => None,
        Some(v) => match v.as_u64().filter(|&n| n > 0) {
            Some(ms) => Some(Duration::from_millis(ms)),
            None => return Err("timeout_ms must be a positive integer".into()),
        },
    };
    let metadata = match object.get("metadata") {
        None | Some(Value::Null) => HashMap::new(),
        Some(Value::Object(map)) => map
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().map(String::from).unwrap_or_else(|| v.to_string())))
            .collect(),
        Some(_) => return Err("metadata must be an object".into()),
    };
    let model = object.get("model").and_then(Value::as_str).filter(|m| !m.is_empty()).unwrap_or("auto").to_string();
    let json = object.get("response_format").and_then(Value::as_str) == Some("json");
    let schema = object.get("response_schema").filter(|s| s.is_object()).cloned();
    let tools = object.get("tools").and_then(Value::as_array).filter(|t| !t.is_empty()).cloned();
    Ok(ChatRequest { messages, model, timeout, metadata, json, schema, tools })
}

fn parse_json(body: &Bytes) -> Result<Value, String> {
    serde_json::from_slice(body).map_err(|_| "body must be JSON".to_string())
}

fn parse_chat_body(body: &Bytes) -> Result<ChatRequest, String> {
    parse_chat_request(&parse_json(body)?)
}

async fn handle_health(State(state): State<AppState>) -> Response {
    let health = state.gateway.health(false).await;
    let ready = health.iter().filter(|h| h.state == BackendState::Ready).count();
    let status = if ready > 0 {
        if ready == health.len() { "ok" } else { "degraded" }
    } else if health.iter().any(|h| h.state == BackendState::Degraded) {
        "degraded"
    } else {
        "down"
    };
    Json(json!({"status": status})).into_response()
}

async fn handle_health_detail(State(state): State<AppState>, Query(query): Query<HashMap<String, String>>) -> Response {
    let refresh = matches!(query.get("refresh").map(String::as_str), Some("1" | "true"));
    Json(json!({"backends": state.gateway.health(refresh).await})).into_response()
}

async fn handle_models(State(state): State<AppState>) -> Response {
    Json(json!({"models": state.gateway.list_models().await})).into_response()
}

async fn handle_upload(State(state): State<AppState>, body: Bytes) -> Response {
    let body = match parse_json(&body) {
        Ok(v) => v,
        Err(e) => return client_error(&e),
    };
    let store = Arc::clone(&state.gateway.attachments);
    let result = if let Some(path) = body.get("path").and_then(Value::as_str) {
        let path = std::path::PathBuf::from(path);
        tokio::task::spawn_blocking(move || store.put_file(&path)).await.unwrap_or_else(|e| Err(e.to_string()))
    } else if let Some(data) = body.get("data_base64").and_then(Value::as_str) {
        match base64::engine::general_purpose::STANDARD.decode(data) {
            Ok(bytes) => store.put(
                bytes,
                body.get("filename").and_then(Value::as_str).unwrap_or("image.png"),
                body.get("mime_type").and_then(Value::as_str),
            ),
            Err(e) => Err(e.to_string()),
        }
    } else {
        return client_error("provide path or data_base64");
    };
    match result {
        Ok(a) => Json(json!({"attachment_id": a.attachment_id, "mime_type": a.mime_type, "size": a.data.len()})).into_response(),
        Err(e) => client_error(&e),
    }
}

async fn handle_chat(State(state): State<AppState>, body: Bytes) -> Response {
    let request = match parse_chat_body(&body) {
        Ok(r) => r,
        Err(e) => return client_error(&e),
    };
    match state.gateway.chat(&request).await {
        Ok(result) => Json(result).into_response(),
        Err(e) => gateway_error(&e),
    }
}

fn sse(event: &str, data: Value) -> Bytes {
    Bytes::from(format!("event: {event}\ndata: {data}\n\n"))
}

async fn handle_chat_stream(State(state): State<AppState>, body: Bytes) -> Response {
    let request = match parse_chat_body(&body) {
        Ok(r) => r,
        Err(e) => return client_error(&e),
    };
    let (out_tx, out_rx) = mpsc::channel::<Bytes>(64);
    let gateway = Arc::clone(&state.gateway);
    tokio::spawn(async move {
        let (tx, mut rx) = mpsc::channel(64);
        let run = { async move { gateway.stream(&request, tx).await } };
        let forward_tx = out_tx.clone();
        let forward = async move {
            let mut current: Option<String> = None;
            while let Some((backend, delta)) = rx.recv().await {
                if current.as_deref() != Some(backend.as_str()) {
                    let _ = forward_tx.send(sse("meta", json!({"backend": backend}))).await;
                    current = Some(backend);
                }
                let _ = forward_tx.send(sse("delta", json!({"content": delta.content, "reasoning": delta.reasoning}))).await;
            }
            current
        };
        let (result, current) = tokio::join!(run, forward);
        let last = match result {
            Ok(()) => sse("done", json!({"backend": current})),
            Err(e) => sse("error", json!({"kind": e.kind.as_str(), "message": e.message, "attempts": e.attempts})),
        };
        let _ = out_tx.send(last).await;
    });
    let stream = futures::stream::unfold(out_rx, |mut rx| async move { rx.recv().await.map(|b| (Ok::<_, Infallible>(b), rx)) });
    let mut headers = HeaderMap::new();
    headers.insert(header::CONTENT_TYPE, "text/event-stream".parse().expect("static header"));
    headers.insert(header::CACHE_CONTROL, "no-cache".parse().expect("static header"));
    (headers, Body::from_stream(stream)).into_response()
}

async fn handle_shutdown(State(state): State<AppState>) -> Response {
    let stop = Arc::clone(&state.stop);
    tokio::spawn(async move {
        tokio::time::sleep(SHUTDOWN_DELAY).await;
        stop.notify_one();
    });
    Json(json!({"stopping": true})).into_response()
}

pub fn router(state: AppState, body_limit: usize) -> Router {
    Router::new()
        .route("/health", get(handle_health))
        .route("/v1/health", get(handle_health_detail))
        .route("/v1/models", get(handle_models))
        .route("/v1/upload", post(handle_upload))
        .route("/v1/chat", post(handle_chat))
        .route("/v1/chat/stream", post(handle_chat_stream))
        .route("/v1/admin/shutdown", post(handle_shutdown))
        .layer(middleware::from_fn_with_state(state.clone(), auth_and_access_log))
        .layer(DefaultBodyLimit::max(body_limit))
        .with_state(state)
}
