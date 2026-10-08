//! Local Gemini Agent core. See docs/architecture.md.

pub mod config;
pub mod error;
pub mod job;
pub mod redact;
pub mod secrets;
pub mod tools;
pub mod worker;
pub mod cancel;
pub mod llm;
pub mod retry;
pub mod policy;
pub mod audit;
pub mod memory;
pub mod state;
pub mod agent;
pub mod planner;
pub mod doctor;
pub mod process;
pub mod interaction;
