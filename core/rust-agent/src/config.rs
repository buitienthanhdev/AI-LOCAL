//! Configuration shared with the Python services.
//!
//! `configs/config.example.toml` holds every default; `configs/config.toml` (git-ignored) and the file
//! named by `LGA_CONFIG` are deep-merged on top. Unknown keys are ignored here because they belong to
//! the Python services.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};

pub const EXAMPLE_CONFIG: &str = "configs/config.example.toml";
pub const USER_CONFIG: &str = "configs/config.toml";

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionMode {
    Normal,
    Trusted,
    FullAccess,
}

impl ExecutionMode {
    pub fn as_str(self) -> &'static str {
        match self {
            ExecutionMode::Normal => "normal",
            ExecutionMode::Trusted => "trusted",
            ExecutionMode::FullAccess => "full_access",
        }
    }

    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "normal" => Ok(Self::Normal),
            "trusted" => Ok(Self::Trusted),
            "full_access" | "full-access" => Ok(Self::FullAccess),
            other => bail!("unknown execution mode {other:?} (normal | trusted | full_access)"),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct AgentCfg {
    pub mode: ExecutionMode,
    pub max_steps: u32,
    pub max_consecutive_failures: u32,
    pub max_identical_actions: u32,
    pub max_parse_retries: u32,
    pub task_timeout_ms: u64,
    pub history_images: usize,
    pub history_steps: usize,
    pub language: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PathsCfg {
    pub state_dir: String,
    pub logs_dir: String,
    pub artifacts_dir: String,
    pub memory_dir: String,
    pub tools_dir: String,
    pub policy_dir: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WorkerCfg {
    pub python: String,
    pub startup_timeout_ms: u64,
    pub hung_grace_ms: u64,
    pub max_restarts: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RetryCfg {
    pub max_attempts: u32,
    pub initial_backoff_ms: u64,
    pub max_backoff_ms: u64,
    pub multiplier: f64,
    pub jitter: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LlmCfg {
    pub provider: String,
    pub gateway_url: String,
    pub gateway_autostart: bool,
    pub gateway_startup_timeout_ms: u64,
    pub request_timeout_ms: u64,
    pub model: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GatewayCfg {
    pub listen_host: String,
    pub listen_port: u16,
    pub python: String,
    pub api_key_secret: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MemoryCfg {
    pub persistent_db: String,
    pub tasks_dir: String,
    pub max_recalled_items: usize,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ObservabilityCfg {
    pub log_level: String,
    pub redact_env: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BrowserCfg {
    pub chrome_path: String,
    pub cdp_url: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct VisionCfg {
    pub backend: String,
    pub ui_tars_base_url: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AutocadCfg {
    pub prog_id: String,
}

#[derive(Debug, Clone, Deserialize)]
struct RawConfig {
    agent: AgentCfg,
    paths: PathsCfg,
    worker: WorkerCfg,
    retry: RetryCfg,
    llm: LlmCfg,
    gateway: GatewayCfg,
    memory: MemoryCfg,
    observability: ObservabilityCfg,
    browser: BrowserCfg,
    vision: VisionCfg,
    autocad: AutocadCfg,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub root: PathBuf,
    pub agent: AgentCfg,
    pub paths: PathsCfg,
    pub worker: WorkerCfg,
    pub retry: RetryCfg,
    pub llm: LlmCfg,
    pub gateway: GatewayCfg,
    pub memory: MemoryCfg,
    pub observability: ObservabilityCfg,
    pub browser: BrowserCfg,
    pub vision: VisionCfg,
    pub autocad: AutocadCfg,
}

fn deep_merge(base: &mut toml::Value, overlay: toml::Value) {
    match (base, overlay) {
        (toml::Value::Table(base_table), toml::Value::Table(overlay_table)) => {
            for (key, value) in overlay_table {
                match base_table.get_mut(&key) {
                    Some(existing) if existing.is_table() && value.is_table() => deep_merge(existing, value),
                    _ => {
                        base_table.insert(key, value);
                    }
                }
            }
        }
        (base, overlay) => *base = overlay,
    }
}

fn read_toml(path: &Path) -> Result<toml::Value> {
    let text = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    toml::from_str(&text).with_context(|| format!("parsing {}", path.display()))
}

/// Locate the repository root: `LGA_ROOT`, else the nearest ancestor of the executable or the current
/// directory that contains the example config.
pub fn find_root() -> Result<PathBuf> {
    if let Ok(root) = std::env::var("LGA_ROOT") {
        let root = PathBuf::from(root);
        if root.join(EXAMPLE_CONFIG).is_file() {
            return Ok(root);
        }
        bail!("LGA_ROOT={} does not contain {EXAMPLE_CONFIG}", root.display());
    }
    let mut starts = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        starts.push(exe);
    }
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd);
    }
    for start in starts {
        for candidate in start.ancestors() {
            if candidate.join(EXAMPLE_CONFIG).is_file() {
                return Ok(candidate.to_path_buf());
            }
        }
    }
    Err(anyhow!("cannot find {EXAMPLE_CONFIG}; run from the repository or set LGA_ROOT"))
}

impl Config {
    pub fn load() -> Result<Self> {
        Self::load_from(&find_root()?)
    }

    pub fn load_from(root: &Path) -> Result<Self> {
        let mut value = read_toml(&root.join(EXAMPLE_CONFIG))?;
        let user = root.join(USER_CONFIG);
        if user.is_file() {
            deep_merge(&mut value, read_toml(&user)?);
        }
        if let Ok(extra) = std::env::var("LGA_CONFIG") {
            deep_merge(&mut value, read_toml(Path::new(&extra))?);
        }
        let raw: RawConfig = value.try_into().context("configuration does not match the expected shape")?;
        Ok(Config {
            root: root.to_path_buf(),
            agent: raw.agent,
            paths: raw.paths,
            worker: raw.worker,
            retry: raw.retry,
            llm: raw.llm,
            gateway: raw.gateway,
            memory: raw.memory,
            observability: raw.observability,
            browser: raw.browser,
            vision: raw.vision,
            autocad: raw.autocad,
        })
    }

    /// Resolve a configured path relative to the repository root.
    pub fn path(&self, value: &str) -> PathBuf {
        let path = PathBuf::from(value);
        if path.is_absolute() {
            path
        } else {
            self.root.join(path)
        }
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.path(&self.paths.logs_dir)
    }

    pub fn artifacts_dir(&self) -> PathBuf {
        self.path(&self.paths.artifacts_dir)
    }

    pub fn tasks_dir(&self) -> PathBuf {
        self.path(&self.memory.tasks_dir)
    }

    pub fn state_dir(&self) -> PathBuf {
        self.path(&self.paths.state_dir)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deep_merge_overrides_nested_keys_only() {
        let mut base: toml::Value = toml::from_str("[a]\nx = 1\ny = 2\n[b]\nz = 3").unwrap();
        let overlay: toml::Value = toml::from_str("[a]\ny = 20").unwrap();
        deep_merge(&mut base, overlay);
        assert_eq!(base["a"]["x"].as_integer(), Some(1));
        assert_eq!(base["a"]["y"].as_integer(), Some(20));
        assert_eq!(base["b"]["z"].as_integer(), Some(3));
    }

    #[test]
    fn example_config_loads() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let config = Config::load_from(&root).expect("example config must deserialize");
        assert!(config.agent.max_steps > 0);
        assert!(config.llm.gateway_url.starts_with("http://127.0.0.1"));
    }
}
