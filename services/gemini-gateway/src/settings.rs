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
    pub retry: RetrySettings,
    pub circuit_breaker: BreakerCfg,
    pub primary: PrimaryCfg,
    pub fallback: FallbackCfg,
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
}

pub fn ms(value: u64) -> Duration {
    Duration::from_millis(value)
}
