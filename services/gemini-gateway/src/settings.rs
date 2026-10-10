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
    /// Which brain is tried first: route names in order ("api", "primary", "fallback", "local_server",
    /// "local"); unlisted routes follow in their default order.
    #[serde(default)]
    pub brain_order: Vec<String>,
    pub retry: RetrySettings,
    pub circuit_breaker: BreakerCfg,
    pub primary: PrimaryCfg,
    pub fallback: FallbackCfg,
    #[serde(default)]
    pub local: LocalCfg,
    #[serde(default)]
    pub api: ApiCfg,
    #[serde(default)]
    pub local_server: LocalServerCfg,
}

/// A local OpenAI-compatible server (llama.cpp llama-server, LM Studio). Tried before Ollama when set.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct LocalServerCfg {
    /// e.g. "http://127.0.0.1:8081"; empty = not used.
    pub base_url: String,
    /// "" or "auto": the first model the server lists.
    #[serde(default)]
    pub model: String,
    /// Program and arguments that start this server when it is down (%VAR% expanded), e.g. llama.cpp's
    /// server with a fixed context, every layer on the GPU and one slot. Empty: the user runs it.
    #[serde(default)]
    pub launch: Vec<String>,
    /// Longest wait for one answer before the next brain is tried.
    #[serde(default = "default_local_server_attempt_ms")]
    pub attempt_timeout_ms: u64,
    /// Generation settings sent with every request (unset: the server's own defaults). A request can
    /// override them through its metadata (thinking, temperature, top_p, top_k), which benchmarks use.
    #[serde(default)]
    pub thinking: bool,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub top_p: Option<f64>,
    #[serde(default)]
    pub top_k: Option<u32>,
    /// Use the model's native tool calling (its chat template's tool format) instead of the JSON reply.
    #[serde(default)]
    pub native_tools: bool,
    /// Other llama.cpp routers (e.g. LlamaApp's, "http://127.0.0.1:9931") whose loaded models are unloaded
    /// before this server starts or when it runs too slowly: two copies of the model do not fit the GPU.
    #[serde(default)]
    pub release: Vec<String>,
}

fn default_local_server_attempt_ms() -> u64 {
    90_000
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
