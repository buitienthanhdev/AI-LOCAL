//! The Chrome Gemini provider through the gateway, against a fake adapter that speaks the transaction
//! contract. No Chrome and no Gemini: these tests pin routing behaviour around a browser exchange that
//! must never be repeated blindly.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

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
use lga_gateway::types::{BackendState, ChatRequest, Message, Part};
use serde_json::{json, Value};

const PNG: &[u8] = b"\x89PNG\r\n\x1a\n00000000000000000000000000000000";

#[derive(Clone)]
enum Script {
    Reply(Value),
    Raw(StatusCode, &'static str),
    Hang,
}

struct Adapter {
    script: Script,
    health: Value,
    calls: Vec<Value>,
}

type Shared = Arc<Mutex<Adapter>>;

fn adapter(script: Script) -> Shared {
    Arc::new(Mutex::new(Adapter {
        script,
        health: json!({"state": "ready", "detail": "signed in", "model_verified": "Pro", "capabilities": {"text": true, "images": false}}),
        calls: vec![],
    }))
}

fn transaction(status: &str, content: &str, error_class: Option<&str>) -> Value {
    json!({
        "transaction_id": "t",
        "status": status,
        "content": content,
        "error": error_class.map(|c| json!({"class": c, "message": "scripted"})),
        "metadata": {"provider": "chrome_gemini", "model_verified": "Pro", "signals": {}}
    })
}

async fn chat(State(a): State<Shared>, Json(body): Json<Value>) -> Response {
    let script = {
        let mut a = a.lock().unwrap();
        a.calls.push(body);
        a.script.clone()
    };
    match script {
        Script::Reply(v) => Json(v).into_response(),
        Script::Raw(status, text) => (status, text).into_response(),
        Script::Hang => {
            tokio::time::sleep(Duration::from_secs(30)).await;
            StatusCode::OK.into_response()
        }
    }
}

async fn health(State(a): State<Shared>) -> Json<Value> {
    Json(a.lock().unwrap().health.clone())
}

async fn serve(app: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

async fn fake_adapter(a: Shared) -> String {
    serve(Router::new().route("/health", get(health)).route("/v1/chat", post(chat)).with_state(a)).await
}

/// A second brain, to see whether the gateway fails over.
async fn fake_other(calls: Arc<Mutex<u32>>) -> String {
    serve(
        Router::new()
            .route("/v1/models", get(|| async { Json(json!({"data": [{"id": "other-model"}]})) }))
            .route(
                "/v1/chat/completions",
                post(move || {
                    let calls = Arc::clone(&calls);
                    async move {
                        *calls.lock().unwrap() += 1;
                        Json(json!({"model": "other-model", "choices": [{"message": {"role": "assistant", "content": "from other"}}]}))
                    }
                }),
            ),
    )
    .await
}

fn gateway(adapter_url: &str, other_url: Option<&str>) -> Arc<Gateway> {
    let store = Arc::new(AttachmentStore::new(1_000_000, 5_000_000, Duration::from_secs(600)));
    let breaker = BreakerSettings { failure_threshold: 2, open_duration: Duration::from_secs(60), max_open_duration: Duration::from_secs(600) };
    let connect = Duration::from_secs(2);
    let mut routes = vec![Route::new(
        Backend::chrome_gemini("chrome_gemini", adapter_url, Arc::clone(&store), connect),
        CircuitBreaker::new(breaker),
        None,
        Duration::from_secs(5),
    )];
    if let Some(url) = other_url {
        routes.push(Route::new(Backend::openai_server("other", url, "other-model", Arc::clone(&store), connect), CircuitBreaker::new(breaker), None, Duration::from_secs(5)));
    }
    Gateway::new(
        routes,
        store,
        BackoffPolicy { max_attempts: 2, initial: Duration::from_millis(10), max: Duration::from_millis(20), multiplier: 2.0, jitter: 0.0 },
        Duration::from_secs(10),
        Duration::from_secs(3600),
    )
}

fn request(parts: Vec<Part>) -> ChatRequest {
    ChatRequest {
        messages: vec![Message { role: "user".into(), parts }],
        model: "auto".into(),
        timeout: Some(Duration::from_secs(8)),
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
async fn completed_transaction_is_returned_with_verified_model_and_metadata() {
    let a = adapter(Script::Reply(transaction("COMPLETED", "xin chào", None)));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    let result = gw.chat(&ping()).await.unwrap();
    assert_eq!((result.backend.as_str(), result.content.as_str(), result.model.as_str()), ("chrome_gemini", "xin chào", "Pro"));
    assert_eq!(result.usage.unwrap()["transaction"]["status"], "Completed");
    let calls = a.lock().unwrap().calls.clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0]["prompt"], "ping");
    assert!(calls[0]["transaction_id"].as_str().is_some_and(|id| id.len() == 32));
}

#[tokio::test]
async fn uncertain_submission_is_never_retried_but_may_fail_over() {
    let a = adapter(Script::Reply(transaction("SUBMISSION_UNCERTAIN", "", Some("connection_lost"))));
    let other_calls = Arc::new(Mutex::new(0));
    let gw = gateway(&fake_adapter(a.clone()).await, Some(&fake_other(other_calls.clone()).await));
    let result = gw.chat(&ping()).await.unwrap();
    assert_eq!(result.backend, "other");
    assert_eq!(a.lock().unwrap().calls.len(), 1, "the browser adapter must see the prompt exactly once");
    assert_eq!(result.attempts[0].error_kind.as_deref(), Some("submission_uncertain"));
    // The adapter answered correctly; its breaker is not held against it.
    assert_eq!(gw.routes[0].breaker_state(), BreakerState::Closed);
}

#[tokio::test]
async fn last_observed_state_reaches_the_attempt_log_while_the_kind_stays_uncertain() {
    let mut body = transaction("GENERATING", "", Some("incomplete"));
    body["metadata"]["signals"] = json!({"last_observed_state": "GENERATING", "submit_calls": 1});
    let a = adapter(Script::Reply(body));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    let error = gw.chat(&ping()).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::SubmissionUncertain, "the observation must not change the contract");
    assert_eq!(error.attempts[0].error_kind.as_deref(), Some("submission_uncertain"));
    assert_eq!(error.attempts[0].last_observed_state.as_deref(), Some("GENERATING"));
    // The attempt log names the transaction, so a late answer can be linked to it afterwards.
    let message = error.attempts[0].message.as_deref().unwrap_or("");
    assert!(message.contains("transaction t ") && message.contains("last_observed_state=GENERATING"), "{message}");
    assert_eq!(a.lock().unwrap().calls.len(), 1, "still exactly one call: observing is not a reason to resend");
}

#[tokio::test]
async fn generating_without_proof_of_completion_is_an_error_and_one_call() {
    let a = adapter(Script::Reply(transaction("GENERATING", "", Some("incomplete"))));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    let error = gw.chat(&ping()).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::SubmissionUncertain);
    assert_eq!(a.lock().unwrap().calls.len(), 1);
}

#[tokio::test]
async fn failed_transaction_is_not_retried() {
    let a = adapter(Script::Reply(transaction("FAILED", "", Some("page_error"))));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    assert_eq!(gw.chat(&ping()).await.unwrap_err().kind, ErrorKind::SubmissionUncertain);
    assert_eq!(a.lock().unwrap().calls.len(), 1);
}

#[tokio::test]
async fn busy_before_sending_may_be_retried_with_a_new_transaction() {
    let a = adapter(Script::Reply(transaction("NOT_SUBMITTED", "", Some("busy"))));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    assert_eq!(gw.chat(&ping()).await.unwrap_err().kind, ErrorKind::RateLimited);
    let calls = a.lock().unwrap().calls.clone();
    assert_eq!(calls.len(), 2, "NOT_SUBMITTED is safe to retry");
    assert_ne!(calls[0]["transaction_id"], calls[1]["transaction_id"]);
}

#[tokio::test]
async fn unreadable_verdict_counts_as_uncertain() {
    let a = adapter(Script::Raw(StatusCode::OK, "<html>not json</html>"));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    assert_eq!(gw.chat(&ping()).await.unwrap_err().kind, ErrorKind::SubmissionUncertain);
    assert_eq!(a.lock().unwrap().calls.len(), 1);
}

#[tokio::test]
async fn adapter_server_error_is_uncertain_not_retried() {
    let a = adapter(Script::Raw(StatusCode::INTERNAL_SERVER_ERROR, "boom"));
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    assert_eq!(gw.chat(&ping()).await.unwrap_err().kind, ErrorKind::SubmissionUncertain);
    assert_eq!(a.lock().unwrap().calls.len(), 1);
}

#[tokio::test]
async fn no_verdict_before_the_deadline_is_uncertain_and_not_retried() {
    let a = adapter(Script::Hang);
    let gw = gateway(&fake_adapter(a.clone()).await, None);
    let mut req = ping();
    req.timeout = Some(Duration::from_millis(1500));
    let error = gw.chat(&req).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::SubmissionUncertain);
    assert_eq!(a.lock().unwrap().calls.len(), 1);
}

#[tokio::test]
async fn unreachable_adapter_is_backend_down_and_fails_over() {
    let other_calls = Arc::new(Mutex::new(0));
    let gw = gateway("http://127.0.0.1:9", Some(&fake_other(other_calls.clone()).await)); // nothing listens on 9
    let result = gw.chat(&ping()).await.unwrap();
    assert_eq!(result.backend, "other");
    assert_eq!(result.attempts[0].error_kind.as_deref(), Some("backend_down"));
}

#[tokio::test]
async fn images_are_refused_before_the_adapter_is_called() {
    let a = adapter(Script::Reply(transaction("COMPLETED", "should not happen", None)));
    let other_calls = Arc::new(Mutex::new(0));
    let gw = gateway(&fake_adapter(a.clone()).await, Some(&fake_other(other_calls.clone()).await));
    let attachment = gw.attachments.put(PNG.to_vec(), "s.png", None).unwrap();
    let result = gw.chat(&request(vec![Part::Text("what is this".into()), Part::Image(attachment.attachment_id)])).await.unwrap();
    assert_eq!(result.backend, "other");
    assert!(a.lock().unwrap().calls.is_empty(), "nothing may be sent without the image");
    assert_eq!(result.attempts[0].error_kind.as_deref(), Some("capability"));
}

#[tokio::test]
async fn health_reflects_the_adapter_and_never_invents_a_model() {
    let a = adapter(Script::Reply(transaction("COMPLETED", "x", None)));
    let url = fake_adapter(a.clone()).await;
    let gw = gateway(&url, None);
    let report = gw.health(true).await;
    assert_eq!(report[0].state, BackendState::Ready);
    assert_eq!(report[0].kind, "chrome_gemini");
    assert!(!report[0].capabilities.images);
    assert_eq!(gw.list_models().await[0].id, "Pro");

    a.lock().unwrap().health = json!({"state": "degraded", "detail": "Gemini is not signed in", "capabilities": {"images": false}});
    let report = gw.health(true).await;
    assert_eq!(report[0].state, BackendState::Degraded);
    assert!(!report[0].capabilities.chat);
    assert!(gw.list_models().await.is_empty());
}

/// The repository's own configuration must leave the new provider off and the routing untouched.
#[test]
fn shipped_configuration_keeps_chrome_gemini_off_and_routing_unchanged() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let config = lga_core::config::Config::load_from(&root).unwrap();
    let settings: lga_gateway::settings::GatewaySettings = config.section("gateway").unwrap();
    assert!(!settings.chrome_gemini.enabled);
    assert_eq!(settings.chrome_gemini.profile_dir, "runtime/gemini-profile");
    assert_eq!(settings.chrome_gemini.require_model, "Gemini Flash-Lite", "the model lock must be on by default");
    assert!(!settings.brain_order.iter().any(|n| n == "chrome_gemini"));
}


/// H3: the adapter requires a bearer token on /v1/chat; the gateway must send exactly the one it was given,
/// and a refusal must read as "nothing was sent" (config), never as an uncertain submission.
async fn token_checking_adapter(expected: &'static str, seen: Arc<Mutex<Vec<Option<String>>>>) -> String {
    use axum::http::HeaderMap;
    serve(Router::new().route(
        "/v1/chat",
        post(move |headers: HeaderMap, Json(_body): Json<Value>| {
            let seen = Arc::clone(&seen);
            async move {
                let got = headers.get("authorization").and_then(|v| v.to_str().ok()).map(str::to_string);
                seen.lock().unwrap().push(got.clone());
                if got.as_deref() == Some(expected) {
                    Json(transaction("COMPLETED", "ok", None)).into_response()
                } else {
                    (StatusCode::FORBIDDEN, Json(json!({"error": "forbidden"}))).into_response()
                }
            }
        }),
    ))
    .await
}

fn gateway_with_token(url: &str, token: Option<&str>) -> Arc<Gateway> {
    let store = Arc::new(AttachmentStore::new(1_000_000, 5_000_000, Duration::from_secs(600)));
    let breaker = BreakerSettings { failure_threshold: 5, open_duration: Duration::from_secs(60), max_open_duration: Duration::from_secs(600) };
    let backend = Backend::chrome_gemini("chrome_gemini", url, Arc::clone(&store), Duration::from_secs(2)).with_adapter_token(token.map(str::to_string));
    Gateway::new(
        vec![Route::new(backend, CircuitBreaker::new(breaker), None, Duration::from_secs(5))],
        store,
        BackoffPolicy { max_attempts: 2, initial: Duration::from_millis(10), max: Duration::from_millis(20), multiplier: 2.0, jitter: 0.0 },
        Duration::from_secs(10),
        Duration::from_secs(3600),
    )
}

#[tokio::test]
async fn gateway_sends_its_adapter_token_as_a_bearer_header() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let url = token_checking_adapter("Bearer per-start-token", seen.clone()).await;
    let result = gateway_with_token(&url, Some("per-start-token")).chat(&ping()).await.unwrap();
    assert_eq!(result.content, "ok");
    assert_eq!(seen.lock().unwrap().as_slice(), [Some("Bearer per-start-token".to_string())]);
}

#[tokio::test]
async fn a_refused_token_is_config_error_and_the_prompt_is_not_retried() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let url = token_checking_adapter("Bearer the-real-one", seen.clone()).await;
    let error = gateway_with_token(&url, Some("stale-or-wrong")).chat(&ping()).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::Config, "403 means the adapter never looked at the prompt");
    assert_eq!(seen.lock().unwrap().len(), 1, "config errors are not retried on the same backend");
    let missing = gateway_with_token(&url, None).chat(&ping()).await.unwrap_err();
    assert_eq!(missing.kind, ErrorKind::Config);
}
