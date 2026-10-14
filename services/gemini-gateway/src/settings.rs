//! The `[gateway]` configuration section (configs/config.example.toml).

use std::time::Duration;

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct GatewaySettings {
    pub listen_host: String,
    pub listen_port: u16,
    pub health_interval_ms: u64,
    pub connect_timeout_ms: u64,
    pub request_timeout_ms: u64,
    pub api_key_secret: String,
    pub attachment_max_bytes: usize,
    pub attachment_total_bytes: usize,
    pub attachment_ttl_ms: u64,
    /// Which brain is tried first: route names in order ("api", "primary", "fallback", "cloud",
    /// "local"); unlisted routes follow in their default order.
    #[serde(default)]
    pub brain_order: Vec<String>,
    pub retry: RetrySettings,
    pub circuit_breaker: BreakerCfg,
    pub primary: PrimaryCfg,
    pub fallback: FallbackCfg,
    #[serde(default)]
    pub local: LocalCfg,
    /// A large model on Ollama's cloud, reached through the local Ollama daemon (signed in with `ollama signin`).
    #[serde(default)]
    pub cloud: CloudCfg,
    #[serde(default)]
    pub api: ApiCfg,
    /// Gemini web app through the gemini_webapi library (services/gemini-webapi): works anonymously, uses cookies
    /// when a cookies file exists.
    #[serde(default)]
    pub webapi: WebApiCfg,
    /// Gemini Web through a dedicated Chrome profile (services/chrome-gemini). Off unless enabled.
    #[serde(default)]
    pub chrome_gemini: ChromeGeminiCfg,
}

/// The Chrome Gemini provider. Disabled by default; enabling it adds a route named "chrome_gemini" that
/// is tried only where `brain_order` places it (unlisted routes come last), so it never changes the
/// default routing by itself.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct ChromeGeminiCfg {
    pub enabled: bool,
    pub base_url: String,
    /// The gateway starts and supervises the adapter process (which in turn is the only owner of the profile).
    pub manage_process: bool,
    /// Python with lga_worker and aiohttp installed (the python-agent venv).
    pub python: String,
    pub source_dir: String,
    /// Dedicated profile; never the user's own Chrome profile.
    pub profile_dir: String,
    /// JSON file of page selectors with `"verified": true`; without it no prompt is ever sent.
    pub selectors: String,
    /// "" = detect from the registry / standard install folders.
    pub chrome_path: String,
    pub startup_timeout_ms: u64,
    pub attempt_timeout_ms: u64,
    /// On a normal stop, how long an exchange in flight may still finish before it is ended as
    /// uncertain (never re-sent) and the adapter's own Chrome is closed.
    pub shutdown_grace_ms: u64,
    /// The model label the Gemini page must show, exactly, for a prompt to be sent. Locked by default to the one
    /// model verified to answer on this account (3.1 Pro refused plain questions; see docs). Empty = no check
    /// (discouraged). The adapter reads the label from the page; it never selects a model itself.
    pub require_model: String,
}

impl Default for ChromeGeminiCfg {
    fn default() -> Self {
        Self {
            enabled: false,
            base_url: "http://127.0.0.1:8795".into(),
            manage_process: true,
            python: "services/python-agent/.venv/Scripts/python.exe".into(),
            source_dir: "services/chrome-gemini".into(),
            profile_dir: "runtime/gemini-profile".into(),
            selectors: "configs/chrome-gemini.selectors.json".into(),
            chrome_path: String::new(),
            startup_timeout_ms: 30_000,
            attempt_timeout_ms: 150_000,
            shutdown_grace_ms: 10_000,
            require_model: "Gemini Flash-Lite".into(),
        }
    }
}

/// Gemini web app via `gemini_webapi` (services/gemini-webapi/server.py), served as a local OpenAI-style server.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct WebApiCfg {
    pub enabled: bool,
    pub base_url: String,
    pub manage_process: bool,
    /// Python of the venv that has gemini-webapi and aiohttp (scripts/setup-webapi.ps1).
    pub python: String,
    pub script: String,
    /// Optional JSON file {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}; anonymous when absent.
    pub cookies_file: String,
    pub startup_timeout_ms: u64,
    pub attempt_timeout_ms: u64,
}

impl Default for WebApiCfg {
    fn default() -> Self {
        Self {
            enabled: false,
            base_url: "http://127.0.0.1:8084".into(),
            manage_process: true,
            python: "runtime/gateway/gemini-webapi/venv/Scripts/python.exe".into(),
            script: "services/gemini-webapi/server.py".into(),
            cookies_file: "runtime/gateway/gemini-webapi/cookies.json".into(),
            startup_timeout_ms: 90_000,
            attempt_timeout_ms: 160_000,
        }
    }
}

/// The official Gemini API (optional). Used first when the user has saved a key: it is the fastest and
/// most reliable brain; Gemini Web and the local model remain as fallbacks.
#[derive(Debug, Clone, Deserialize)]
pub struct ApiCfg {
    pub enabled: bool,
    pub base_url: String,
    /// "auto": the newest Flash model the key can use.
    pub model: String,
    pub api_key_secret: String,
    pub attempt_timeout_ms: u64,
}

impl Default for ApiCfg {
    fn default() -> Self {
        Self {
            enabled: true,
            base_url: "https://generativelanguage.googleapis.com/v1beta/openai".into(),
            model: "auto".into(),
            api_key_secret: "LGA_GEMINI_API_KEY".into(),
            attempt_timeout_ms: 60_000,
        }
    }
}

/// Last-resort local model through Ollama (used when both Gemini Web backends cannot answer).
#[derive(Debug, Clone, Deserialize)]
pub struct LocalCfg {
    pub enabled: bool,
    pub base_url: String,
    pub model: String,
    pub num_ctx: u32,
    /// ollama.exe used to start the server when it is not running ("" = find it on PATH or in the
    /// default install folder).
    #[serde(default)]
    pub ollama: String,
}

impl Default for LocalCfg {
    fn default() -> Self {
        Self { enabled: false, base_url: "http://127.0.0.1:11434".into(), model: "gemma4:12b".into(), num_ctx: 16384, ollama: String::new() }
    }
}

/// Ollama cloud model (e.g. "gpt-oss:120b-cloud"): far larger than anything the local GPU can hold. Prompts
/// and screenshots leave the PC for Ollama's servers.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct CloudCfg {
    pub enabled: bool,
    pub base_url: String,
    pub model: String,
    /// Reasoning before answering (models that support it).
    pub thinking: bool,
    pub attempt_timeout_ms: u64,
}

impl Default for CloudCfg {
    fn default() -> Self {
        Self { enabled: false, base_url: "http://127.0.0.1:11434".into(), model: "gpt-oss:120b-cloud".into(), thinking: true, attempt_timeout_ms: 120_000 }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct RetrySettings {
    pub max_attempts: u32,
    pub initial_backoff_ms: u64,
    pub max_backoff_ms: u64,
    pub multiplier: f64,
    pub jitter: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BreakerCfg {
    pub failure_threshold: u32,
    pub open_duration_ms: u64,
    pub max_open_duration_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PrimaryCfg {
    pub base_url: String,
    pub manage_process: bool,
    pub binary: String,
    pub work_dir: String,
    pub api_key_secret: String,
    pub admin_token_secret: String,
    pub default_model: String,
    pub startup_timeout_ms: u64,
    #[serde(default = "default_attempt_timeout_ms")]
    pub attempt_timeout_ms: u64,
}

fn default_attempt_timeout_ms() -> u64 {
    75_000
}

#[derive(Debug, Clone, Deserialize)]
pub struct FallbackCfg {
    pub base_url: String,
    pub manage_process: bool,
    pub python: String,
    pub source_dir: String,
    pub work_dir: String,
    pub default_model: String,
    pub startup_timeout_ms: u64,
    #[serde(default = "default_attempt_timeout_ms")]
    pub attempt_timeout_ms: u64,
}

pub fn ms(value: u64) -> Duration {
    Duration::from_millis(value)
}

