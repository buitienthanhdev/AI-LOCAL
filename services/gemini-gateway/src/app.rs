//! Builds the gateway from configuration.

use std::collections::HashMap;
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{anyhow, bail, Result};
use lga_core::config::Config;
use lga_core::redact::Redactor;
use lga_core::secrets::resolve_secret;

use crate::attachments::AttachmentStore;
use crate::backend::{Backend, Generation};
use crate::gateway::{Gateway, Route};
use crate::resilience::{BackoffPolicy, BreakerSettings, CircuitBreaker};
use crate::settings::{ms, GatewaySettings};
use crate::supervisor::{KillOnCloseJob, ManagedProcess, ProcessSpec};

/// Request bodies carry base64 images (4/3 expansion) plus prompt text.
const BODY_OVERHEAD_BYTES: usize = 4_000_000;

pub struct Assembly {
    pub gateway: Arc<Gateway>,
    pub api_key: String,
    pub host: String,
    pub port: u16,
    pub body_limit: usize,
    /// Kept alive for the process lifetime: closing it kills the managed backends.
    pub job: Arc<KillOnCloseJob>,
}

fn is_loopback(host: &str) -> bool {
    host == "localhost" || host.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

/// The explicit port of a loopback URL such as http://127.0.0.1:8083.
fn loopback_port(url: &str) -> Result<String> {
    let authority = url.strip_prefix("http://").ok_or_else(|| anyhow!("{url} must be an http:// URL"))?;
    let authority = authority.split('/').next().unwrap_or("");
    let (host, port) = authority.rsplit_once(':').ok_or_else(|| anyhow!("{url} must include an explicit port"))?;
    if !is_loopback(host) {
        bail!("{url} must bind/connect to a loopback address");
    }
    port.parse::<u16>().map_err(|_| anyhow!("{url} has an invalid port"))?;
    Ok(port.to_string())
}

/// ollama.exe: the configured path, else PATH, else the default per-user install folder.
fn find_ollama(configured: &str) -> Option<PathBuf> {
    if !configured.is_empty() {
        return Some(PathBuf::from(configured)).filter(|p| p.is_file());
    }
    lga_core::native::shell::which("ollama").or_else(|| {
        let local = std::env::var("LOCALAPPDATA").ok()?;
        Some(PathBuf::from(local).join("Programs").join("Ollama").join("ollama.exe")).filter(|p| p.is_file())
    })
}

/// `%NAME%` replaced by the environment variable (left as is when it is not set).
fn expand_env(value: &str) -> String {
    let mut out = String::new();
    let mut rest = value;
    while let Some(start) = rest.find('%') {
        let Some(len) = rest[start + 1..].find('%') else { break };
        let name = &rest[start + 1..start + 1 + len];
        out.push_str(&rest[..start]);
        match std::env::var(name) {
            Ok(v) if !name.is_empty() => out.push_str(&v),
            _ => out.push_str(&rest[start..start + len + 2]),
        }
        rest = &rest[start + len + 2..];
    }
    out.push_str(rest);
    out
}

fn text(path: PathBuf) -> String {
    path.to_string_lossy().into_owned()
}

/// Every configured secret is redacted from logs, including the backends' own output.
pub fn redactor(config: &Config) -> Redactor {
    let mut redactor = Redactor::new();
    for name in &config.observability.redact_env {
        redactor.add_literal(resolve_secret(&config.root, name).as_deref());
    }
    redactor
}

pub fn assemble(config: &Config, redactor: Arc<Redactor>) -> Result<Assembly> {
    let s: GatewaySettings = config.section("gateway")?;
    if !is_loopback(&s.listen_host) {
        bail!("gateway.listen_host must be a loopback address, got {:?}", s.listen_host);
    }
    let api_key = resolve_secret(&config.root, &s.api_key_secret)
        .ok_or_else(|| anyhow!("secret {} is not configured; run scripts/setup-gateway.ps1", s.api_key_secret))?;
    let store = Arc::new(AttachmentStore::new(s.attachment_max_bytes, s.attachment_total_bytes, ms(s.attachment_ttl_ms)));
    let breaker = BreakerSettings {
        failure_threshold: s.circuit_breaker.failure_threshold,
        open_duration: ms(s.circuit_breaker.open_duration_ms),
        max_open_duration: ms(s.circuit_breaker.max_open_duration_ms),
    };
    let retry = BackoffPolicy {
        max_attempts: s.retry.max_attempts.max(1),
        initial: ms(s.retry.initial_backoff_ms),
        max: ms(s.retry.max_backoff_ms),
        multiplier: s.retry.multiplier,
        jitter: s.retry.jitter,
    };
    let connect = ms(s.connect_timeout_ms);
    let job = Arc::new(KillOnCloseJob::new()?);
    let logs = config.logs_dir().join("gateway");

    // Primary: gemini-web2api-go.
    let p = &s.primary;
    let primary_key = resolve_secret(&config.root, &p.api_key_secret);
    let admin_token = resolve_secret(&config.root, &p.admin_token_secret);
    let primary_port = loopback_port(&p.base_url)?;
    let mut primary_process = None;
    if p.manage_process {
        let (Some(key), Some(token)) = (&primary_key, &admin_token) else {
            bail!("primary backend secrets are missing; run scripts/setup-gateway.ps1");
        };
        let work_dir = config.path(&p.work_dir);
        primary_process = Some(ManagedProcess::new(
            ProcessSpec {
                name: "primary".into(),
                argv: vec![
                    text(config.path(&p.binary)),
                    "--config".into(),
                    text(work_dir.join("config.json")),
                    "--port".into(),
                    primary_port,
                ],
                cwd: work_dir,
                env: HashMap::from([("API_KEY".into(), key.clone()), ("ADMIN_TOKEN".into(), token.clone())]),
                log_path: Some(logs.join("primary.log")),
            },
            Arc::clone(&job),
            Arc::clone(&redactor),
        ));
    }
    let primary = Backend::primary("primary", &p.base_url, &p.default_model, Arc::clone(&store), connect, primary_key, admin_token);

    // Fallback: WebAI-to-API.
    let f = &s.fallback;
    let fallback_port = loopback_port(&f.base_url)?;
    let mut fallback_process = None;
    if f.manage_process {
        let work_dir = config.path(&f.work_dir);
        let runtime_dir = work_dir.join("runtime");
        fallback_process = Some(ManagedProcess::new(
            ProcessSpec {
                name: "fallback".into(),
                argv: vec![
                    text(config.path(&f.python)),
                    text(config.path(&f.source_dir).join("src").join("run.py")),
                    "--host".into(),
                    "127.0.0.1".into(),
                    "--port".into(),
                    fallback_port,
                    "--disable-access-logs".into(),
                ],
                cwd: work_dir,
                env: HashMap::from([
                    ("RUNTIME_DIR".into(), text(runtime_dir.clone())),
                    ("AUTH_STATE_DIR".into(), text(runtime_dir.join("auth"))),
                    ("CONVERSATION_SNAPSHOT_DB".into(), text(runtime_dir.join("conversations.db"))),
                    ("PYTHONIOENCODING".into(), "utf-8".into()),
                ]),
                log_path: Some(logs.join("fallback.log")),
            },
            Arc::clone(&job),
            Arc::clone(&redactor),
        ));
    }
    let fallback = Backend::fallback("fallback", &f.base_url, &f.default_model, Arc::clone(&store), connect);

    let mut routes = Vec::new();
    // Official Gemini API first when the user saved a key (Settings in the chat window).
    if s.api.enabled {
        if let Some(key) = resolve_secret(&config.root, &s.api.api_key_secret) {
            let api = Backend::gemini_api("api", &s.api.base_url, &s.api.model, key, Arc::clone(&store), connect);
            routes.push(Route::new(api, CircuitBreaker::new(breaker), None, ms(0)).with_attempt_timeout(ms(s.api.attempt_timeout_ms)));
        }
    }
    routes.extend([
        Route::new(primary, CircuitBreaker::new(breaker), primary_process, ms(p.startup_timeout_ms)).with_attempt_timeout(ms(p.attempt_timeout_ms)),
        Route::new(fallback, CircuitBreaker::new(breaker), fallback_process, ms(f.startup_timeout_ms)).with_attempt_timeout(ms(f.attempt_timeout_ms)),
    ]);
    // A local OpenAI-compatible server the user runs (llama.cpp, LM Studio), when configured.
    if !s.local_server.base_url.trim().is_empty() {
        loopback_port(s.local_server.base_url.trim())?;
        let mut server = Backend::openai_server("local_server", s.local_server.base_url.trim(), &s.local_server.model, Arc::clone(&store), connect);
        server.generation = Generation {
            thinking: s.local_server.thinking,
            temperature: s.local_server.temperature,
            top_p: s.local_server.top_p,
            top_k: s.local_server.top_k,
        };
        server.release = s.local_server.release.clone();
        server.managed = !s.local_server.launch.is_empty();
        // Capped so a stuck local server still leaves time for the other brains.
        let mut route = Route::new(server, CircuitBreaker::new(breaker), None, ms(0))
            .with_attempt_timeout(ms(s.local_server.attempt_timeout_ms));
        if let Some((program, args)) = s.local_server.launch.split_first() {
            route = route.with_on_demand_start(PathBuf::from(expand_env(program)), args.iter().map(|a| expand_env(a)).collect());
        }
        routes.push(route);
    }
    // Local model: the agent keeps working when Gemini Web is down or Google throttles the account.
    let l = &s.local;
    if l.enabled {
        loopback_port(&l.base_url)?;
        let local = Backend::local("local", &l.base_url, &l.model, l.num_ctx, Arc::clone(&store), connect);
        let mut route = Route::new(local, CircuitBreaker::new(breaker), None, ms(0));
        if let Some(ollama) = find_ollama(&l.ollama) {
            route = route.with_on_demand_start(ollama, vec!["serve".into()]);
        }
        routes.push(route);
    }
    // A user-chosen order (e.g. the local llama.cpp model first); a stable sort keeps the rest as they are.
    routes.sort_by_key(|r| s.brain_order.iter().position(|n| *n == r.backend.name).unwrap_or(usize::MAX));
    let gateway = Gateway::new(routes, store, retry, ms(s.request_timeout_ms), ms(s.health_interval_ms));
    Ok(Assembly {
        gateway,
        api_key,
        host: s.listen_host,
        port: s.listen_port,
        body_limit: s.attachment_max_bytes * 2 + BODY_OVERHEAD_BYTES,
        job,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ports_must_be_loopback_and_explicit() {
        assert_eq!(loopback_port("http://127.0.0.1:8083").unwrap(), "8083");
        assert_eq!(loopback_port("http://localhost:6969/").unwrap(), "6969");
        assert!(loopback_port("http://10.0.0.5:8083").is_err());
        assert!(loopback_port("http://127.0.0.1").is_err());
    }

    #[test]
    fn env_variables_expand_in_launch_paths() {
        std::env::set_var("LGA_TEST_DIR", "C:/x");
        assert_eq!(expand_env("%LGA_TEST_DIR%/llama.exe"), "C:/x/llama.exe");
        assert_eq!(expand_env("%LGA_UNSET_VAR_123%/a"), "%LGA_UNSET_VAR_123%/a");
        assert_eq!(expand_env("50%"), "50%");
    }
}
