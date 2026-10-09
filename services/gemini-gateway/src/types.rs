//! Provider-neutral chat types. These are what the agent core sees; no Gemini specifics.

use std::collections::HashMap;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub enum Part {
    Text(String),
    Image(String),
}

#[derive(Debug, Clone)]
pub struct Message {
    pub role: String,
    pub parts: Vec<Part>,
}

impl Message {
    pub fn text(&self) -> String {
        self.parts
            .iter()
            .filter_map(|p| if let Part::Text(t) = p { Some(t.as_str()) } else { None })
            .collect::<Vec<_>>()
            .join("\n")
    }

    pub fn has_images(&self) -> bool {
        self.parts.iter().any(|p| matches!(p, Part::Image(_)))
    }
}

#[derive(Debug, Clone)]
pub struct ChatRequest {
    pub messages: Vec<Message>,
    /// "auto" lets each backend use its configured default model.
    pub model: String,
    pub timeout: Option<Duration>,
    /// Free-form correlation data (task_id, step_id) recorded in logs; never sent upstream.
    pub metadata: HashMap<String, String>,
    /// The caller needs a single JSON object back; backends with a JSON mode use it.
    pub json: bool,
    /// Optional JSON schema for that object; a local llama.cpp server decodes under it (grammar).
    pub schema: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Attempt {
    pub backend: String,
    pub ok: bool,
    pub error_kind: Option<String>,
    pub message: Option<String>,
    pub duration_ms: u64,
    pub status: Option<u16>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChatResult {
    pub content: String,
    pub reasoning: Option<String>,
    pub model: String,
    pub backend: String,
    pub usage: Option<Value>,
    pub latency_ms: u64,
    pub attempts: Vec<Attempt>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StreamDelta {
    pub content: String,
    pub reasoning: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModelInfo {
    pub id: String,
    pub backend: String,
    pub description: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendState {
    /// Reachable and able to serve chats.
    Ready,
    /// Reachable but missing a capability (e.g. no account for images).
    Degraded,
    Unavailable,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Capabilities {
    pub chat: bool,
    pub images: bool,
}

impl Capabilities {
    pub const NONE: Capabilities = Capabilities { chat: false, images: false };
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendHealth {
    pub name: String,
    pub kind: String,
    pub state: BackendState,
    pub detail: String,
    pub capabilities: Capabilities,
    pub breaker: String,
    pub checked_at: f64,
    pub process: Value,
}
