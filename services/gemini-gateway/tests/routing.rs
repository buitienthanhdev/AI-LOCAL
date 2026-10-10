//! Gateway routing tests against fake backends that mimic the real upstream response shapes.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use lga_gateway::attachments::AttachmentStore;
use lga_gateway::backend::Backend;
use lga_gateway::errors::ErrorKind;
use lga_gateway::gateway::{Gateway, Route};
use lga_gateway::resilience::{BackoffPolicy, BreakerSettings, BreakerState, CircuitBreaker};
use lga_gateway::server::{router, AppState};
use lga_gateway::types::{ChatRequest, Message, Part};
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};

const PNG: &[u8] = b"\x89PNG\r\n\x1a\n00000000000000000000000000000000";

#[derive(Default)]
struct Behaviour {
    reply: String,
    no_done: bool,
    /// Error response for the first `fail_times` calls (None = every call).
    fail: Option<(u16, Value)>,
    fail_times: Option<u32>,
    calls: Vec<Value>,
}

type Shared = Arc<Mutex<Behaviour>>;

fn behaviour(reply: &str) -> Shared {
    Arc::new(Mutex::new(Behaviour { reply: reply.into(), ..Default::default() }))
}

async fn fake_chat(State(b): State<Shared>, body: Bytes) -> Response {
    let payload: Value = serde_json::from_slice(&body).unwrap();
    let mut b = b.lock().unwrap();
    b.calls.push(payload.clone());
    let failure = match (&b.fail, b.fail_times) {
        (Some(_), Some(0)) => None,
        (Some(f), Some(n)) => {
            let f = f.clone();
            b.fail_times = Some(n - 1);
            Some(f)
        }
        (Some(f), None) => Some(f.clone()),
        (None, _) => None,
    };
    if let Some((status, body)) = failure {
        return (StatusCode::from_u16(status).unwrap(), Json(body)).into_response();
    }
    if payload["stream"] == true {
        let mut out = String::new();
        for token in b.reply.split(' ').filter(|t| !t.is_empty()) {
            out.push_str(&format!("data: {}\n\n", json!({"choices": [{"delta": {"content": format!("{token} ")}}]})));
        }
        if !b.no_done {
            out.push_str("data: [DONE]\n\n");
        }
        return ([("content-type", "text/event-stream")], Body::from(out)).into_response();
    }
    Json(json!({"model": "fake", "choices": [{"message": {"role": "assistant", "content": b.reply}}], "usage": {}})).into_response()
}

async fn serve(app: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

async fn fake_primary(b: Shared) -> String {
    serve(
        Router::new()
            .route("/", get(|| async { Json(json!({"status": "ok", "version": "test"})) }))
            .route("/v1/models", get(|| async { Json(json!({"data": [{"id": "gemini-3.6-flash"}]})) }))
            .route("/v1/chat/completions", post(fake_chat))
            .with_state(b),
    )
    .await
}

async fn fake_fallback(b: Shared) -> String {
    serve(
        Router::new()
            .route("/health", get(|| async { StatusCode::OK }))
            .route("/v1/stateless/models", get(|| async { Json(json!({"data": [{"id": "gemini-3-flash"}]})) }))
            .route("/v1/stateless/chat/completions", post(fake_chat))
            .with_state(b),
    )
    .await
}

fn gateway(primary_url: &str, fallback_url: &str, threshold: u32) -> Arc<Gateway> {
    let store = Arc::new(AttachmentStore::new(1_000_000, 5_000_000, Duration::from_secs(600)));
    let settings = BreakerSettings { failure_threshold: threshold, open_duration: Duration::from_secs(60), max_open_duration: Duration::from_secs(600) };
    let connect = Duration::from_secs(2);
    let primary = Backend::primary("primary", primary_url, "gemini-3.6-flash", Arc::clone(&store), connect, Some("k".into()), None);
    let fallback = Backend::fallback("fallback", fallback_url, "", Arc::clone(&store), connect);
    Gateway::new(
        vec![
            Route::new(primary, CircuitBreaker::new(settings), None, Duration::from_secs(5)),
            Route::new(fallback, CircuitBreaker::new(settings), None, Duration::from_secs(5)),
        ],
        store,
        BackoffPolicy { max_attempts: 2, initial: Duration::from_millis(10), max: Duration::from_millis(20), multiplier: 2.0, jitter: 0.0 },
        Duration::from_secs(10),
        Duration::from_secs(3600),
    )
}

fn request(parts: Vec<Part>) -> ChatRequest {
    ChatRequest {
        messages: vec![
            Message { role: "system".into(), parts: vec![Part::Text("be brief".into())] },
            Message { role: "user".into(), parts },
        ],
        model: "auto".into(),
        timeout: None,
        metadata: HashMap::new(),
        json: false,
        schema: None,
        tools: None,
    }
}

fn ping() -> ChatRequest {
    request(vec![Part::Text("ping".into())])
}

#[tokio::test]
async fn primary_serves_when_healthy() {
    let (p, f) = (behaviour("from primary"), behaviour("from fallback"));
    let gw = gateway(&fake_primary(p).await, &fake_fallback(f.clone()).await, 2);
    let result = gw.chat(&ping()).await.unwrap();
    assert_eq!((result.backend.as_str(), result.content.as_str()), ("primary", "from primary"));
    assert!(f.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn failover_when_primary_down_and_breaker_opens() {
    let gw = gateway("http://127.0.0.1:9", &fake_fallback(behaviour("from fallback")).await, 2); // port 9: nothing listens
    for _ in 0..3 {
        assert_eq!(gw.chat(&ping()).await.unwrap().backend, "fallback");
    }
    assert_eq!(gw.routes[0].breaker_state(), BreakerState::Open);
    // With the breaker open the primary is skipped without a connection attempt.
    let result = gw.chat(&ping()).await.unwrap();
    assert_eq!(result.attempts[0].error_kind.as_deref(), Some("breaker_open"));
}

#[tokio::test]
async fn client_error_is_not_failed_over() {
    let p = behaviour("");
    p.lock().unwrap().fail = Some((400, json!({"error": {"message": "too long", "type": "invalid_request_error", "code": "context_length_exceeded"}})));
    let f = behaviour("x");
    let gw = gateway(&fake_primary(p).await, &fake_fallback(f.clone()).await, 2);
    let error = gw.chat(&ping()).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::ClientError);
    assert!(f.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn rate_limit_is_retried_on_same_backend() {
    let p = behaviour("after retry");
    {
        let mut b = p.lock().unwrap();
        b.fail = Some((429, json!({"error": {"message": "ip_slot_full", "type": "rate_limit_exceeded"}})));
        b.fail_times = Some(1);
    }
    let f = behaviour("x");
    let gw = gateway(&fake_primary(p.clone()).await, &fake_fallback(f.clone()).await, 2);
    let result = gw.chat(&ping()).await.unwrap();
    assert_eq!((result.backend.as_str(), result.content.as_str()), ("primary", "after retry"));
    assert_eq!(p.lock().unwrap().calls.len(), 2);
    assert!(f.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn capability_error_fails_over_with_image_parts_converted() {
    let p = behaviour("");
    p.lock().unwrap().fail = Some((400, json!({"error": {"message": "image input needs a Google account cookie", "type": "invalid_request_error"}})));
    let f = behaviour("saw image");
    let gw = gateway(&fake_primary(p.clone()).await, &fake_fallback(f.clone()).await, 2);
    let attachment = gw.attachments.put(PNG.to_vec(), "s.png", None).unwrap();
    let result = gw.chat(&request(vec![Part::Text("what is this".into()), Part::Image(attachment.attachment_id)])).await.unwrap();
    assert_eq!(result.backend, "fallback");
    let primary_parts = p.lock().unwrap().calls[0]["messages"][1]["content"].clone();
    let fallback_call = f.lock().unwrap().calls[0].clone();
    assert_eq!(primary_parts[1]["type"], "image_url");
    assert_eq!(fallback_call["messages"][1]["content"][1]["type"], "file");
    assert!(fallback_call["messages"][1]["content"][1]["file"]["file_data"].as_str().unwrap().starts_with("data:image/png;base64,"));
    assert!(fallback_call.get("temperature").is_none());
    // Capability mismatches do not count against the primary's breaker.
    assert_eq!(gw.routes[0].breaker_state(), BreakerState::Closed);
}

#[tokio::test]
async fn unknown_attachment_is_a_client_error() {
    let gw = gateway(&fake_primary(behaviour("x")).await, &fake_fallback(behaviour("x")).await, 2);
    let error = gw.chat(&request(vec![Part::Image("att_missing".into())])).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::ClientError);
}

async fn collect(gw: &Gateway, req: &ChatRequest) -> (Vec<(String, String)>, bool) {
    let (tx, mut rx) = mpsc::channel(64);
    let result = gw.stream(req, tx).await;
    let mut chunks = Vec::new();
    while let Ok((backend, delta)) = rx.try_recv() {
        chunks.push((backend, delta.content));
    }
    (chunks, result.is_ok())
}

#[tokio::test]
async fn stream_fails_over_only_before_first_token() {
    let p = behaviour("x");
    p.lock().unwrap().no_done = true;
    let gw = gateway(&fake_primary(p.clone()).await, &fake_fallback(behaviour("hello there")).await, 5);
    // The primary emits a token and then breaks: that cannot be spliced, so the error surfaces.
    let (_, ok) = collect(&gw, &ping()).await;
    assert!(!ok);
    p.lock().unwrap().reply = String::new(); // now the primary breaks before emitting anything -> failover is safe
    let (chunks, ok) = collect(&gw, &ping()).await;
    assert!(ok);
    assert!(chunks.iter().all(|(b, _)| b == "fallback"));
    assert_eq!(chunks.iter().map(|(_, c)| c.as_str()).collect::<String>().trim(), "hello there");
}

#[tokio::test]
async fn http_api_requires_key_and_serves_chat() {
    let gw = gateway(&fake_primary(behaviour("api ok")).await, "http://127.0.0.1:9", 2);
    let state = AppState { gateway: gw, api_key: Arc::new("secret-key-123".into()), stop: Arc::new(Notify::new()) };
    let base = serve(router(state, 10_000_000)).await;
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    assert_eq!(client.get(format!("{base}/health")).send().await.unwrap().status(), 200);
    let body = json!({"messages": [{"role": "user", "content": "hi"}]});
    assert_eq!(client.post(format!("{base}/v1/chat")).json(&body).send().await.unwrap().status(), 401);
    let ok = client.post(format!("{base}/v1/chat")).bearer_auth("secret-key-123").json(&body).send().await.unwrap();
    assert_eq!(ok.status(), 200);
    let reply: Value = ok.json().await.unwrap();
    assert_eq!((reply["content"].as_str(), reply["backend"].as_str()), (Some("api ok"), Some("primary")));
    let bad = client.post(format!("{base}/v1/chat")).bearer_auth("secret-key-123").json(&json!({"messages": []})).send().await.unwrap();
    assert_eq!(bad.status(), 400);
    let upload = client
        .post(format!("{base}/v1/upload"))
        .bearer_auth("secret-key-123")
        .json(&json!({"data_base64": "iVBORw0KGgoAAAA=", "filename": "x.png"}))
        .send()
        .await
        .unwrap();
    assert_eq!(upload.status(), 200);
    let stream = client.post(format!("{base}/v1/chat/stream")).bearer_auth("secret-key-123").json(&body).send().await.unwrap();
    let text = stream.text().await.unwrap();
    assert!(text.contains("event: meta") && text.contains("event: delta") && text.contains("event: done"), "{text}");
}

async fn fake_ollama(calls: Arc<Mutex<Vec<Value>>>) -> String {
    async fn chat(State(calls): State<Arc<Mutex<Vec<Value>>>>, body: Bytes) -> Response {
        let payload: Value = serde_json::from_slice(&body).unwrap();
        calls.lock().unwrap().push(payload.clone());
        if payload["stream"] == true {
            let lines = "{\"message\":{\"content\":\"lo\"},\"done\":false}\n{\"message\":{\"content\":\"cal\"},\"done\":false}\n{\"message\":{\"content\":\"\"},\"done\":true}\n";
            return Body::from(lines).into_response();
        }
        Json(json!({"model": "gemma4:12b", "message": {"role": "assistant", "content": "local answer"}, "prompt_eval_count": 10, "eval_count": 2})).into_response()
    }
    serve(
        Router::new()
            .route("/api/tags", get(|| async { Json(json!({"models": [{"name": "gemma4:12b"}]})) }))
            .route("/api/chat", post(chat))
            .with_state(calls),
    )
    .await
}

#[tokio::test]
async fn local_model_serves_when_gemini_is_down() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let local_url = fake_ollama(Arc::clone(&calls)).await;
    let store = Arc::new(AttachmentStore::new(1_000_000, 5_000_000, Duration::from_secs(600)));
    let settings = BreakerSettings { failure_threshold: 2, open_duration: Duration::from_secs(60), max_open_duration: Duration::from_secs(600) };
    let connect = Duration::from_secs(2);
    let gw = Gateway::new(
        vec![
            Route::new(Backend::primary("primary", "http://127.0.0.1:9", "m", Arc::clone(&store), connect, None, None), CircuitBreaker::new(settings), None, Duration::from_secs(5)),
            Route::new(Backend::fallback("fallback", "http://127.0.0.1:9", "", Arc::clone(&store), connect), CircuitBreaker::new(settings), None, Duration::from_secs(5)),
            Route::new(Backend::local("local", &local_url, "gemma4:12b", 16384, Arc::clone(&store), connect), CircuitBreaker::new(settings), None, Duration::from_secs(5)),
        ],
        Arc::clone(&store),
        BackoffPolicy { max_attempts: 1, initial: Duration::from_millis(10), max: Duration::from_millis(20), multiplier: 2.0, jitter: 0.0 },
        Duration::from_secs(10),
        Duration::from_secs(3600),
    );
    let attachment = store.put(PNG.to_vec(), "s.png", None).unwrap();
    let result = gw.chat(&request(vec![Part::Text("hi".into()), Part::Image(attachment.attachment_id)])).await.unwrap();
    assert_eq!((result.backend.as_str(), result.content.as_str()), ("local", "local answer"));
    let sent = calls.lock().unwrap()[0].clone();
    assert_eq!(sent["options"]["num_ctx"], 16384);
    assert_eq!(sent["model"], "gemma4:12b");
    assert!(sent["messages"][1]["images"][0].as_str().unwrap().starts_with("iVBOR"), "raw base64, no data: prefix");
    let (chunks, ok) = collect(&gw, &ping()).await;
    assert!(ok);
    assert_eq!(chunks.iter().map(|(_, c)| c.as_str()).collect::<String>(), "local");
    let health = gw.health(true).await;
    assert_eq!(health[2].state, lga_gateway::types::BackendState::Ready);
}
