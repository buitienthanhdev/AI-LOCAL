//! Settings-page data: the person's own playbooks (skills, style, lessons) and model usage statistics.
//!
//! User playbooks are ordinary `skills/u-<id>.md` files in the repository's playbook folder, so the agent's existing
//! playbook matcher uses them with no other plumbing; a trigger of `*` means "always" (standing style/instructions).

use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use anyhow::{anyhow, Result};
use lga_core::config::Config;
use serde_json::{json, Value};

const MAX_BODY_CHARS: usize = 3000;
const MAX_TRIGGERS: usize = 16;

fn dir(config: &Config) -> PathBuf {
    config.root.join("skills")
}

fn valid_id(id: &str) -> bool {
    id.starts_with("u-") && id.len() <= 64 && id.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

/// Playbooks written from the settings page (`u-*.md`), newest first.
pub fn list_skills(config: &Config) -> Value {
    let mut items: Vec<(SystemTime, Value)> = Vec::new();
    for entry in fs::read_dir(dir(config)).into_iter().flatten().flatten() {
        let path = entry.path();
        let id = path.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
        if !valid_id(&id) || path.extension().is_none_or(|e| e != "md") {
            continue;
        }
        let Ok(text) = fs::read_to_string(&path) else { continue };
        let Some(book) = lga_core::skills::parse(&text) else { continue };
        let kind = text.lines().find_map(|l| l.trim().strip_prefix("kind:")).map(|k| k.trim().to_string()).unwrap_or_else(|| "skill".into());
        let modified = entry.metadata().and_then(|m| m.modified()).unwrap_or(SystemTime::UNIX_EPOCH);
        items.push((modified, json!({"id": id, "name": book.name, "triggers": book.triggers, "body": book.body, "kind": kind,
                                     "always": book.triggers.iter().any(|t| t == "*")})));
    }
    items.sort_by_key(|(t, _)| std::cmp::Reverse(*t));
    json!({"skills": items.into_iter().map(|(_, v)| v).collect::<Vec<_>>()})
}

/// Create or replace one user playbook. `id` empty = new. Returns the id.
pub fn save_skill(config: &Config, body: &Value) -> Result<Value> {
    let name = body["name"].as_str().unwrap_or("").trim().replace(['\r', '\n'], " ");
    let text = body["body"].as_str().unwrap_or("").trim().to_string();
    let kind = match body["kind"].as_str().unwrap_or("skill") {
        k @ ("skill" | "style" | "lesson") => k,
        _ => "skill",
    };
    if name.is_empty() || name.chars().count() > 80 {
        return Err(anyhow!("tên phải dài 1–80 ký tự"));
    }
    if text.is_empty() || text.chars().count() > MAX_BODY_CHARS {
        return Err(anyhow!("nội dung phải dài 1–{MAX_BODY_CHARS} ký tự"));
    }
    if text.starts_with("---") {
        return Err(anyhow!("nội dung không được bắt đầu bằng ---"));
    }
    let always = body["always"].as_bool().unwrap_or(false);
    let mut triggers: Vec<String> = body["triggers"]
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_str).map(|t| t.trim().to_lowercase().replace([',', '\n', '\r'], " ")).filter(|t| !t.is_empty() && t != "*").collect())
        .unwrap_or_default();
    triggers.truncate(MAX_TRIGGERS);
    if always {
        triggers = vec!["*".into()];
    }
    if triggers.is_empty() {
        return Err(anyhow!("cần ít nhất một từ khóa kích hoạt (hoặc chọn 'luôn áp dụng')"));
    }
    let id = match body["id"].as_str().filter(|s| !s.is_empty()) {
        Some(id) if valid_id(id) => id.to_string(),
        Some(_) => return Err(anyhow!("mã kỹ năng không hợp lệ")),
        None => format!("u-{}", uuid::Uuid::new_v4().simple().to_string().chars().take(10).collect::<String>()),
    };
    fs::create_dir_all(dir(config))?;
    let content = format!("---\nname: {name}\nkind: {kind}\ntriggers: {}\n---\n{text}\n", triggers.join(", "));
    let tmp = dir(config).join(format!("{id}.md.tmp"));
    fs::write(&tmp, content)?;
    fs::rename(&tmp, dir(config).join(format!("{id}.md")))?;
    Ok(json!({"ok": true, "id": id}))
}

pub fn delete_skill(config: &Config, id: &str) -> Result<Value> {
    if !valid_id(id) {
        return Err(anyhow!("mã kỹ năng không hợp lệ"));
    }
    let path = dir(config).join(format!("{id}.md"));
    if path.is_file() {
        fs::remove_file(path)?;
    }
    Ok(json!({"ok": true}))
}

/// What each brain did over the last `days` days, read from the task audit logs (`logs/tasks/*.jsonl`).
pub fn usage(config: &Config, days: u64) -> Value {
    #[derive(Default)]
    struct Acc {
        ok: u64,
        failed: u64,
        prompt_tokens: u64,
        completion_tokens: u64,
        ms: u64,
        last_error: String,
        last_error_at: String,
        last_ok_at: String,
        model: String,
        by_day: BTreeMap<String, (u64, u64)>,
    }
    let cutoff = SystemTime::now() - Duration::from_secs(days.max(1) * 86_400);
    let cutoff_day = chrono::DateTime::<chrono::Utc>::from(cutoff).format("%Y-%m-%d").to_string();
    let mut by_backend: BTreeMap<String, Acc> = BTreeMap::new();
    let tasks = config.logs_dir().join("tasks");
    for entry in fs::read_dir(&tasks).into_iter().flatten().flatten() {
        let path = entry.path();
        if path.extension().is_none_or(|e| e != "jsonl") {
            continue;
        }
        if entry.metadata().and_then(|m| m.modified()).is_ok_and(|m| m < cutoff) {
            continue;
        }
        let Ok(file) = fs::File::open(&path) else { continue };
        for line in BufReader::new(file).lines().map_while(Result::ok) {
            if !line.contains("\"kind\":\"llm\"") {
                continue;
            }
            let Ok(event) = serde_json::from_str::<Value>(&line) else { continue };
            let ts = event["ts"].as_str().unwrap_or("").to_string();
            if ts.get(..10).is_some_and(|d| d < cutoff_day.as_str()) {
                continue;
            }
            let day = ts.get(..10).unwrap_or("").to_string();
            for attempt in event["detail"]["attempts"].as_array().into_iter().flatten() {
                let name = attempt["backend"].as_str().unwrap_or("?").to_string();
                let acc = by_backend.entry(name).or_default();
                let ok = attempt["ok"].as_bool().unwrap_or(false);
                let slot = acc.by_day.entry(day.clone()).or_insert((0, 0));
                if ok {
                    acc.ok += 1;
                    slot.0 += 1;
                    acc.ms += attempt["duration_ms"].as_u64().unwrap_or(0);
                    if ts > acc.last_ok_at {
                        acc.last_ok_at = ts.clone();
                    }
                } else if attempt["error_kind"].as_str() != Some("breaker_open") {
                    acc.failed += 1;
                    slot.1 += 1;
                    if ts >= acc.last_error_at {
                        acc.last_error_at = ts.clone();
                        acc.last_error = attempt["message"].as_str().unwrap_or("").chars().take(300).collect();
                    }
                }
            }
            // tokens and model belong to the backend that answered
            let answered = event["provider"].as_str().and_then(|p| p.rsplit(':').next()).unwrap_or("").to_string();
            if let Some(acc) = by_backend.get_mut(&answered) {
                if let Some(model) = event["detail"]["model"].as_str() {
                    acc.model = model.to_string();
                }
                let usage = &event["detail"]["usage"];
                acc.prompt_tokens += usage["prompt_tokens"].as_u64().unwrap_or(0);
                acc.completion_tokens += usage["completion_tokens"].as_u64().unwrap_or(0);
            }
        }
    }
    let list: Vec<Value> = by_backend
        .into_iter()
        .map(|(name, a)| {
            let quota_hit = {
                let e = a.last_error.to_lowercase();
                e.contains("quota") || e.contains("429") || e.contains("usage limit") || e.contains("rate limit")
            };
            json!({
                "name": name, "model": a.model, "ok": a.ok, "failed": a.failed,
                "avg_ms": if a.ok > 0 { a.ms / a.ok } else { 0 },
                "prompt_tokens": a.prompt_tokens, "completion_tokens": a.completion_tokens,
                "last_error": a.last_error, "last_error_at": a.last_error_at, "last_ok_at": a.last_ok_at,
                "quota_hit": quota_hit && a.last_error_at >= a.last_ok_at,
                "by_day": a.by_day.iter().map(|(d, (ok, bad))| json!({"day": d, "ok": ok, "failed": bad})).collect::<Vec<_>>(),
            })
        })
        .collect();
    json!({"days": days, "backends": list})
}
