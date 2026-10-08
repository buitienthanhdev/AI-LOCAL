//! JSON-lines log under logs/gateway/<date>.jsonl, every message redacted (same format as the other services).

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::{Mutex, OnceLock};

use lga_core::redact::Redactor;
use serde_json::json;

struct Sink {
    file: Mutex<Option<File>>,
    redactor: Redactor,
    debug: bool,
}

static SINK: OnceLock<Sink> = OnceLock::new();

pub fn init(logs_dir: &Path, level: &str, redactor: Redactor) {
    let dir = logs_dir.join("gateway");
    let file = std::fs::create_dir_all(&dir)
        .ok()
        .and_then(|_| OpenOptions::new().create(true).append(true).open(dir.join(format!("{}.jsonl", chrono::Local::now().format("%Y-%m-%d")))).ok());
    let _ = SINK.set(Sink { file: Mutex::new(file), redactor, debug: level.eq_ignore_ascii_case("debug") });
}

fn write(level: &str, message: &str) {
    let Some(sink) = SINK.get() else { return };
    if level == "debug" && !sink.debug {
        return;
    }
    let message = sink.redactor.redact(message);
    let line = json!({
        "ts": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        "service": "gateway",
        "level": level,
        "logger": "lga_gateway",
        "message": message,
    });
    if let Some(file) = sink.file.lock().expect("log lock").as_mut() {
        let _ = writeln!(file, "{line}");
    }
    eprintln!("{} gateway {} {message}", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), level.to_uppercase());
}

pub fn info(message: &str) {
    write("info", message);
}

pub fn warn(message: &str) {
    write("warning", message);
}

pub fn error(message: &str) {
    write("error", message);
}
