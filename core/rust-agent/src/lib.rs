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
pub mod guard;
pub mod policy;
pub mod audit;
pub mod banner;
pub mod memory;
pub mod state;
pub mod agent;
pub mod completion;
pub mod planner;
pub mod skills;
pub mod doctor;
pub mod process;
pub mod interaction;
pub mod native;
