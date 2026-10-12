//! Work tracking for the chat window: today's time per program (read from the tracker's local database) and the
//! on/off switch. The tracker itself is `python -m lga_worker.activity` (services/python-agent/lga_worker/activity);
//! it stops by itself when `memory/activity/enabled.flag` is removed.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, Result};
use lga_core::config::Config;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

fn dir(config: &Config) -> PathBuf {
    config.root.join("memory").join("activity")
}

fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn midnight_local(now: f64) -> f64 {
    use chrono::{Local, TimeZone};
    let local = Local.timestamp_opt(now as i64, 0).single().unwrap_or_else(Local::now);
    let day = local.date_naive().and_hms_opt(0, 0, 0).unwrap();
    Local.from_local_datetime(&day).single().map(|d| d.timestamp() as f64).unwrap_or(now - 86_400.0)
}

fn tracker_pid(config: &Config) -> Option<u32> {
    let text = std::fs::read_to_string(dir(config).join("tracker.pid")).ok()?;
    let pid: u32 = text.trim().parse().ok()?;
    // alive?  (OpenProcess + exit code would be exact; a cheap check through tasklist-free means: the pid file is
    // removed by the tracker on exit, so a stale file is rare and only makes `running` optimistic for one refresh)
    let alive = std::process::Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/NH"])
        .creation_flags_hidden()
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains(&pid.to_string()))
        .unwrap_or(false);
    alive.then_some(pid)
}

trait HiddenCommand {
    fn creation_flags_hidden(&mut self) -> &mut Self;
}
impl HiddenCommand for std::process::Command {
    fn creation_flags_hidden(&mut self) -> &mut Self {
        use std::os::windows::process::CommandExt;
        self.creation_flags(0x0800_0000)
    }
}

/// `period`: "today" (default) or "7d".
pub fn summary(config: &Config, period: &str) -> Value {
    let now = now_s();
    let since = if period == "7d" { now - 7.0 * 86_400.0 } else { midnight_local(now) };
    let enabled = dir(config).join("enabled.flag").is_file();
    let running = tracker_pid(config).is_some();
    let db = dir(config).join("activity.db");
    let base = json!({"enabled": enabled, "running": running, "since": since, "until": now, "period": period});
    let mut out = base.as_object().cloned().unwrap();
    let Ok(conn) = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY) else {
        out.insert("records".into(), json!(0));
        out.insert("apps".into(), json!([]));
        return Value::Object(out);
    };
    let rows: Vec<(String, String, String, f64, f64)> = (|| -> Result<Vec<_>> {
        let mut stmt = conn.prepare(
            "SELECT process, title, state, MAX(start, ?1), MIN(end, ?2) FROM sessions WHERE end > ?1 AND start < ?2",
        )?;
        let rows = stmt
            .query_map([since, now], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    })()
    .unwrap_or_default();
    let (mut active, mut idle) = (0.0_f64, 0.0_f64);
    let mut apps: HashMap<String, f64> = HashMap::new();
    let mut windows: HashMap<(String, String), f64> = HashMap::new();
    let mut hours: HashMap<i64, f64> = HashMap::new();
    for (process, title, state, s, e) in &rows {
        let dur = (e - s).max(0.0);
        if dur <= 0.0 {
            continue;
        }
        if state == "idle" {
            idle += dur;
            continue;
        }
        active += dur;
        *apps.entry(process.clone()).or_default() += dur;
        *windows.entry((process.clone(), title.clone())).or_default() += dur;
        *hours.entry((*s / 3600.0) as i64).or_default() += dur;
    }
    let pct = |x: f64, whole: f64| if whole > 0.0 { (1000.0 * x / whole).round() / 10.0 } else { 0.0 };
    let mut app_list: Vec<(&String, &f64)> = apps.iter().collect();
    app_list.sort_by(|a, b| b.1.partial_cmp(a.1).unwrap());
    let mut win_list: Vec<(&(String, String), &f64)> = windows.iter().collect();
    win_list.sort_by(|a, b| b.1.partial_cmp(a.1).unwrap());
    let mut hour_list: Vec<(&i64, &f64)> = hours.iter().collect();
    hour_list.sort_by_key(|h| *h.0);
    out.insert("records".into(), json!(rows.len()));
    out.insert("active_seconds".into(), json!(active.round()));
    out.insert("idle_seconds".into(), json!(idle.round()));
    out.insert("tracked_seconds".into(), json!((active + idle).round()));
    out.insert("apps".into(), json!(app_list.iter().take(15).map(|(p, s)| {
        json!({"process": p, "seconds": s.round(), "percent_of_active": pct(**s, active), "percent_of_tracked": pct(**s, active + idle)})
    }).collect::<Vec<_>>()));
    out.insert("windows".into(), json!(win_list.iter().take(12).map(|((p, t), s)| json!({"process": p, "title": t, "seconds": s.round()})).collect::<Vec<_>>()));
    out.insert("hours".into(), json!(hour_list.iter().map(|(h, s)| json!({"hour_start": **h * 3600, "seconds": s.round()})).collect::<Vec<_>>()));
    Value::Object(out)
}

/// `start` creates the flag and launches the tracker (hidden, detached); `stop` removes the flag (the tracker
/// leaves by itself within a few seconds).
pub fn control(config: &Config, action: &str) -> Result<Value> {
    let d = dir(config);
    std::fs::create_dir_all(&d)?;
    let flag = d.join("enabled.flag");
    match action {
        "start" => {
            std::fs::write(&flag, chrono::Local::now().to_rfc3339())?;
            if tracker_pid(config).is_none() {
                let python = config.path(&config.worker.python);
                let cwd = config.root.join("services").join("python-agent");
                let root = config.root.to_string_lossy().to_string();
                lga_core::process::spawn_hidden_console(
                    &python,
                    &["-m", "lga_worker.activity"],
                    &cwd,
                    &[("LGA_ROOT", root.trim_start_matches(r"\\?\")), ("PYTHONIOENCODING", "utf-8")],
                )
                .map_err(|e| anyhow!("không khởi động được bộ theo dõi: {e}"))?;
            }
            Ok(json!({"enabled": true}))
        }
        "stop" => {
            let _ = std::fs::remove_file(&flag);
            Ok(json!({"enabled": false}))
        }
        _ => Err(anyhow!("hành động không hợp lệ")),
    }
}
