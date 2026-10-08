//! Primary/fallback routing with circuit breakers, bounded retries and health monitoring.
//!
//! Routing for every request:
//!   for backend in [primary, fallback]:
//!       skip if its breaker is open
//!       try up to `retry.max_attempts` times (only for RATE_LIMITED / TRANSIENT errors, with backoff)
//!       CLIENT_ERROR  -> stop and return the error (another backend would reject it too)
//!       other errors  -> record on the breaker (except CAPABILITY) and fail over
//! All attempts share one deadline; nothing retries forever.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::json;
use tokio::sync::mpsc;

use crate::attachments::AttachmentStore;
use crate::backend::Backend;
use crate::errors::{BackendError, ErrorKind, GatewayError};
use crate::resilience::{BackoffPolicy, BreakerState, CircuitBreaker};
use crate::supervisor::ManagedProcess;
use crate::types::{Attempt, BackendHealth, BackendState, Capabilities, ChatRequest, ChatResult, ModelInfo, Part, StreamDelta};

const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(500);

pub struct Route {
    pub backend: Backend,
    pub breaker: Mutex<CircuitBreaker>,
    pub process: Option<ManagedProcess>,
    pub startup_timeout: Duration,
    /// Upper bound for one attempt on this backend, so a hanging Gemini Web call still leaves time for
    /// the next backend (None: the request's remaining time).
    pub attempt_timeout: Option<Duration>,
    /// A server that is started only when a request needs it and it is not running (the local Ollama,
    /// which the user may have quit). Not supervised: it keeps running after the gateway exits.
    on_demand: Option<OnDemand>,
    health: Mutex<Option<BackendHealth>>,
    restart_lock: tokio::sync::Mutex<()>,
}

impl Route {
    pub fn new(backend: Backend, breaker: CircuitBreaker, process: Option<ManagedProcess>, startup_timeout: Duration) -> Self {
        Self {
            backend,
            breaker: Mutex::new(breaker),
            process,
            startup_timeout,
            attempt_timeout: None,
            on_demand: None,
            health: Mutex::new(None),
            restart_lock: tokio::sync::Mutex::new(()),
        }
    }

    pub fn with_on_demand_start(mut self, program: std::path::PathBuf, args: Vec<String>) -> Self {
        self.on_demand = Some(OnDemand { program, args, last_start: Mutex::new(None) });
        self
    }

    pub fn with_attempt_timeout(mut self, timeout: Duration) -> Self {
        self.attempt_timeout = Some(timeout).filter(|t| !t.is_zero());
        self
    }

    fn attempt_budget(&self, remaining: Duration) -> Duration {
        self.attempt_timeout.map_or(remaining, |t| t.min(remaining))
    }

    pub fn breaker_state(&self) -> BreakerState {
        self.breaker.lock().expect("breaker lock").state()
    }

    fn breaker(&self) -> std::sync::MutexGuard<'_, CircuitBreaker> {
        self.breaker.lock().expect("breaker lock")
    }
}

pub struct OnDemand {
    program: std::path::PathBuf,
    args: Vec<String>,
    last_start: Mutex<Option<Instant>>,
}

/// Start attempts of an on-demand server are at least this far apart.
const ON_DEMAND_RETRY: Duration = Duration::from_secs(60);
/// How long a request waits for an on-demand server to come up.
const ON_DEMAND_STARTUP: Duration = Duration::from_secs(45);

/// Start the route's on-demand server when it is down, and wait until it answers. Returns quickly when
/// the server is already up or was started less than `ON_DEMAND_RETRY` ago.
async fn ensure_on_demand(route: &Route) {
    let Some(on_demand) = &route.on_demand else { return };
    if matches!(route.backend.probe().await, Ok(p) if p.state != BackendState::Unavailable) {
        return;
    }
    {
        let mut last = on_demand.last_start.lock().expect("on-demand lock");
        if last.is_some_and(|t| t.elapsed() < ON_DEMAND_RETRY) {
            return;
        }
        *last = Some(Instant::now());
    }
    let name = &route.backend.name;
    if let Err(e) = crate::supervisor::spawn_independent(&on_demand.program, &on_demand.args) {
        crate::log::error(&format!("cannot start {name} server {}: {e}", on_demand.program.display()));
        return;
    }
    crate::log::info(&format!("started {name} server {} on demand", on_demand.program.display()));
    let deadline = Instant::now() + ON_DEMAND_STARTUP;
    while Instant::now() < deadline {
        tokio::time::sleep(STARTUP_POLL_INTERVAL).await;
        if matches!(route.backend.probe().await, Ok(p) if p.state != BackendState::Unavailable) {
            route.breaker().record_success();
            return;
        }
    }
}

pub struct Gateway {
    pub routes: Vec<Arc<Route>>,
    pub attachments: Arc<AttachmentStore>,
    retry: BackoffPolicy,
    request_timeout: Duration,
    health_interval: Duration,
}

fn millis(since: Instant) -> u64 {
    since.elapsed().as_millis() as u64
}

fn failed(backend: &str, error: &BackendError, started: Instant) -> Attempt {
    Attempt {
        backend: backend.into(),
        ok: false,
        error_kind: Some(error.kind.as_str().into()),
        message: Some(error.message.clone()),
        duration_ms: millis(started),
        status: error.status,
    }
}

impl Gateway {
    pub fn new(routes: Vec<Route>, attachments: Arc<AttachmentStore>, retry: BackoffPolicy, request_timeout: Duration, health_interval: Duration) -> Arc<Self> {
        assert!(!routes.is_empty(), "at least one backend route is required");
        Arc::new(Self { routes: routes.into_iter().map(Arc::new).collect(), attachments, retry, request_timeout, health_interval })
    }

    // ---------------------------------------------------------------------------------------------
    // lifecycle
    // ---------------------------------------------------------------------------------------------

    pub async fn start(self: &Arc<Self>) {
        futures::future::join_all(self.routes.iter().map(|r| ensure_process(Arc::clone(r)))).await;
        self.health(true).await;
        let gateway = Arc::clone(self);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(gateway.health_interval).await;
                gateway.health(true).await;
            }
        });
    }

    pub fn close(&self) {
        for route in &self.routes {
            if let Some(process) = &route.process {
                process.stop();
            }
        }
    }

    /// A managed backend that died is restarted right away instead of at the next health tick.
    fn restart_if_dead(&self, route: &Arc<Route>, error: &BackendError) {
        if error.kind == ErrorKind::BackendDown && route.process.as_ref().is_some_and(|p| !p.is_running()) {
            tokio::spawn(ensure_process(Arc::clone(route)));
        }
    }

    async fn probe(route: Arc<Route>) -> BackendHealth {
        if route.process.as_ref().is_some_and(|p| !p.is_running()) {
            ensure_process(Arc::clone(&route)).await;
        }
        let (state, detail, capabilities) = match route.backend.probe().await {
            Ok(p) => (p.state, p.detail, p.capabilities),
            Err(e) => (BackendState::Unavailable, e.message, Capabilities::NONE),
        };
        {
            let mut breaker = route.breaker();
            if state == BackendState::Unavailable && breaker.state() == BreakerState::Closed && route.on_demand.is_none() {
                breaker.record_failure();
            }
        }
        let health = BackendHealth {
            name: route.backend.name.clone(),
            kind: route.backend.kind_name().into(),
            state,
            detail,
            capabilities,
            breaker: route.breaker_state().as_str().into(),
            checked_at: SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0),
            process: route.process.as_ref().map(ManagedProcess::status).unwrap_or_else(|| json!({"managed": false})),
        };
        *route.health.lock().expect("health lock") = Some(health.clone());
        health
    }

    // ---------------------------------------------------------------------------------------------
    // provider API
    // ---------------------------------------------------------------------------------------------

    pub async fn health(&self, refresh: bool) -> Vec<BackendHealth> {
        let missing = self.routes.iter().any(|r| r.health.lock().expect("health lock").is_none());
        if refresh || missing {
            return futures::future::join_all(self.routes.iter().map(|r| Self::probe(Arc::clone(r)))).await;
        }
        self.routes
            .iter()
            .filter_map(|r| {
                let mut health = r.health.lock().expect("health lock").clone()?;
                health.breaker = r.breaker_state().as_str().into();
                Some(health)
            })
            .collect()
    }

    pub async fn list_models(&self) -> Vec<ModelInfo> {
        let mut models = Vec::new();
        for route in &self.routes {
            match route.backend.list_models().await {
                Ok(found) => models.extend(found),
                Err(e) => crate::log::warn(&format!("listing models of {} failed: {}", route.backend.name, e.message)),
            }
        }
        models
    }

    fn check_attachments(&self, request: &ChatRequest) -> Result<(), GatewayError> {
        for message in &request.messages {
            for part in &message.parts {
                if let Part::Image(id) = part {
                    self.attachments.get(id).map_err(|e| GatewayError::new(ErrorKind::ClientError, e, vec![]))?;
                }
            }
        }
        Ok(())
    }

    fn deadline(&self, request: &ChatRequest) -> Instant {
        Instant::now() + request.timeout.unwrap_or(self.request_timeout)
    }

    /// Book-keeping after a failed call on `route`. Returns the error for the caller when it must not fail over.
    fn on_failure(&self, route: &Arc<Route>, error: &BackendError, attempts: &[Attempt]) -> Option<GatewayError> {
        if error.kind == ErrorKind::ClientError {
            route.breaker().record_success(); // the backend answered; it is healthy
            return Some(GatewayError::new(ErrorKind::ClientError, error.message.clone(), attempts.to_vec()));
        }
        if error.kind.penalises_breaker() {
            route.breaker().record_failure();
        } else {
            route.breaker().release_probe();
        }
        self.restart_if_dead(route, error);
        None
    }

    pub async fn chat(&self, request: &ChatRequest) -> Result<ChatResult, GatewayError> {
        self.check_attachments(request)?;
        let deadline = self.deadline(request);
        let started = Instant::now();
        let mut attempts: Vec<Attempt> = Vec::new();
        let mut last_error: Option<BackendError> = None;
        for route in &self.routes {
            let name = route.backend.name.clone();
            ensure_on_demand(route).await;
            {
                let mut breaker = route.breaker();
                if !breaker.allow() {
                    let wait = breaker.until_probe_at(Instant::now()).as_secs();
                    attempts.push(Attempt {
                        backend: name,
                        ok: false,
                        error_kind: Some("breaker_open".into()),
                        message: Some(format!("circuit open for {wait}s more")),
                        duration_ms: 0,
                        status: None,
                    });
                    continue;
                }
            }
            for attempt_no in 1..=self.retry.max_attempts {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(GatewayError::new(ErrorKind::BackendDown, "request deadline exceeded", attempts));
                }
                let t0 = Instant::now();
                match route.backend.chat(request, route.attempt_budget(remaining)).await {
                    Ok(reply) => {
                        route.breaker().record_success();
                        attempts.push(Attempt { backend: name.clone(), ok: true, error_kind: None, message: None, duration_ms: millis(t0), status: Some(200) });
                        return Ok(ChatResult {
                            content: reply.content,
                            reasoning: reply.reasoning,
                            model: reply.model,
                            backend: name,
                            usage: reply.usage,
                            latency_ms: millis(started),
                            attempts,
                        });
                    }
                    Err(error) => {
                        attempts.push(failed(&name, &error, t0));
                        let task = request.metadata.get("task_id").map(String::as_str).unwrap_or("-");
                        crate::log::warn(&format!("{name} attempt {attempt_no} failed (task {task}): {}", error.message));
                        if error.kind == ErrorKind::ClientError {
                            return Err(self.on_failure(route, &error, &attempts).expect("client errors are returned"));
                        }
                        if error.kind.retry_same_backend() && attempt_no < self.retry.max_attempts {
                            let delay = self.retry.delay(attempt_no);
                            if Instant::now() + delay < deadline {
                                tokio::time::sleep(delay).await;
                                continue;
                            }
                        }
                        self.on_failure(route, &error, &attempts);
                        last_error = Some(error);
                        break;
                    }
                }
            }
        }
        let kind = last_error.map_or(ErrorKind::BackendDown, |e| e.kind);
        Err(GatewayError::new(kind, "no Gemini backend could serve the request", attempts))
    }

    /// Stream `(backend, delta)` into `tx`. Fails over only before the first token: once part of an
    /// answer reached the caller, switching backends would splice two answers.
    pub async fn stream(&self, request: &ChatRequest, tx: mpsc::Sender<(String, StreamDelta)>) -> Result<(), GatewayError> {
        self.check_attachments(request)?;
        let deadline = self.deadline(request);
        let mut attempts: Vec<Attempt> = Vec::new();
        let mut last_error: Option<BackendError> = None;
        for route in &self.routes {
            let name = route.backend.name.clone();
            ensure_on_demand(route).await;
            if !route.breaker().allow() {
                attempts.push(Attempt {
                    backend: name,
                    ok: false,
                    error_kind: Some("breaker_open".into()),
                    message: Some("circuit open".into()),
                    duration_ms: 0,
                    status: None,
                });
                continue;
            }
            let t0 = Instant::now();
            let (inner_tx, mut inner_rx) = mpsc::channel::<StreamDelta>(64);
            let forward_tx = tx.clone();
            let forward_name = name.clone();
            let forward = tokio::spawn(async move {
                while let Some(delta) = inner_rx.recv().await {
                    if forward_tx.send((forward_name.clone(), delta)).await.is_err() {
                        break;
                    }
                }
            });
            let budget = route.attempt_budget(deadline.saturating_duration_since(Instant::now()));
            let (emitted, result) = route.backend.stream(request, budget, &inner_tx).await;
            drop(inner_tx);
            let _ = forward.await;
            match result {
                Ok(()) => {
                    route.breaker().record_success();
                    return Ok(());
                }
                Err(error) => {
                    attempts.push(failed(&name, &error, t0));
                    if let Some(client_error) = self.on_failure(route, &error, &attempts) {
                        return Err(client_error);
                    }
                    if emitted {
                        return Err(GatewayError::new(error.kind, format!("stream from {name} broke mid-answer: {}", error.message), attempts));
                    }
                    last_error = Some(error);
                }
            }
        }
        let kind = last_error.map_or(ErrorKind::BackendDown, |e| e.kind);
        Err(GatewayError::new(kind, "no Gemini backend could serve the stream", attempts))
    }
}

/// Start (or restart) a managed backend whose process is not running, then wait until it answers.
/// A backend already running outside the gateway is adopted.
pub async fn ensure_process(route: Arc<Route>) {
    let Some(process) = &route.process else { return };
    if process.is_running() {
        return;
    }
    let _guard = route.restart_lock.lock().await;
    if process.is_running() {
        return;
    }
    let name = &route.backend.name;
    if matches!(route.backend.probe().await, Ok(p) if p.state != BackendState::Unavailable) {
        crate::log::info(&format!("{name} already running outside the gateway; adopting it"));
        return;
    }
    if let Err(e) = process.start() {
        crate::log::error(&format!("cannot start {name}: {e:#}"));
        route.breaker().force_open();
        return;
    }
    let deadline = Instant::now() + route.startup_timeout;
    while Instant::now() < deadline {
        if !process.is_running() {
            crate::log::error(&format!("{name} exited during startup (code {:?})", process.last_exit_code()));
            route.breaker().force_open();
            return;
        }
        if let Ok(probe) = route.backend.probe().await {
            if probe.state != BackendState::Unavailable {
                crate::log::info(&format!("{name} is up: {}", probe.detail));
                // Freshly (re)started and answering: the failures that tripped the breaker are history.
                route.breaker().record_success();
                return;
            }
        }
        tokio::time::sleep(STARTUP_POLL_INTERVAL).await;
    }
    crate::log::error(&format!("{name} did not become reachable within {}s", route.startup_timeout.as_secs()));
}
