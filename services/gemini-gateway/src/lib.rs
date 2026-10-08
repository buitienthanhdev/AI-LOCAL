//! Gemini Web gateway: one local, provider-neutral chat API in front of two Gemini Web implementations
//! (primary gemini-web2api-go, fallback WebAI-to-API) with circuit breakers, bounded retries, health
//! monitoring and supervision of the backend processes. See docs/architecture.md.

pub mod app;
pub mod attachments;
pub mod backend;
pub mod errors;
pub mod gateway;
pub mod log;
pub mod resilience;
pub mod server;
pub mod settings;
pub mod supervisor;
pub mod types;
