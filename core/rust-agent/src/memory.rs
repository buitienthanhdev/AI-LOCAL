//! Persistent memory (SQLite, `memory/persistent.db`).
//!
//! Three tiers (docs/architecture.md, "Memory"):
//! * short-term: the planner's sliding window of recent steps (in `agent.rs`, never persisted here);
//! * task memory: the task record in `memory/tasks/<id>.json` (`state.rs`);
//! * persistent memory: this database: task history, successful procedures, known application
//!   state, failure patterns and the recovery that worked, and the notes the user asked the agent to
//!   remember (preferences, facts about them), which every planner prompt carries.
//! Everything is redacted before it is stored; secrets are never written.

use std::collections::HashSet;
use std::path::Path;
use std::sync::Mutex;

use anyhow::{anyhow, Context, Result};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;

use crate::redact::Redactor;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, summary TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS procedures (
    id INTEGER PRIMARY KEY, goal TEXT NOT NULL, keywords TEXT NOT NULL, steps TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 1, last_used TEXT NOT NULL, UNIQUE(goal)
);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY, tool TEXT NOT NULL, error_code TEXT NOT NULL, context TEXT NOT NULL,
    recovery TEXT, count INTEGER NOT NULL DEFAULT 1, last_seen TEXT NOT NULL,
    UNIQUE(tool, error_code, context)
);
CREATE TABLE IF NOT EXISTS app_state (
    app TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (app, key)
);
CREATE TABLE IF NOT EXISTS user_notes (
    id INTEGER PRIMARY KEY, note TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
-- Episodic (finished tasks), feedback (the user's corrections / ratings) and semantic (confirmed lasting facts and
-- rules) memories. Every row says where it came from, how sure we are and whether the user switched it off.
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                       -- episodic | feedback | semantic
    scope TEXT NOT NULL DEFAULT 'global',     -- global | project | session
    text TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'auto',      -- user | auto | feedback
    task_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence TEXT NOT NULL DEFAULT 'unverified',  -- verified | unverified | user
    status TEXT NOT NULL DEFAULT 'active',    -- active | disabled | conflict
    hits INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_used TEXT,
    UNIQUE(kind, text)
);
"#;
/// Added to the older tables so one switch turns any memory off without deleting it.
const MIGRATIONS: [&str; 3] = [
    "ALTER TABLE user_notes ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE procedures ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE failures ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
];
const MAX_RECALLED_MEMORIES: usize = 5;
/// Notes carried in every planner prompt (newest first).
const MAX_PROMPT_NOTES: usize = 40;
const MAX_STORED_TEXT: usize = 4000;
const MIN_KEYWORD_CHARS: usize = 3;

#[derive(Debug, Clone, Serialize)]
pub struct Procedure {
    pub goal: String,
    pub steps: String,
    pub success_count: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct FailurePattern {
    pub tool: String,
    pub error_code: String,
    pub context: String,
    pub recovery: Option<String>,
    pub count: i64,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct Recall {
    pub procedures: Vec<Procedure>,
    pub failures: Vec<FailurePattern>,
    pub app_state: Vec<(String, String, String)>,
    /// What the user asked to be remembered (always included, not ranked).
    pub notes: Vec<String>,
    /// Past episodes, corrections and confirmed rules that match the request (ranked, at most a few).
    pub memories: Vec<RecalledMemory>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecalledMemory {
    pub id: i64,
    pub kind: String,
    pub text: String,
    pub confidence: f64,
    pub evidence: String,
    pub status: String,
    pub hits: i64,
    pub date: String,
}

impl Recall {
    pub fn is_empty(&self) -> bool {
        self.procedures.is_empty() && self.failures.is_empty() && self.app_state.is_empty() && self.notes.is_empty() && self.memories.is_empty()
    }

    pub fn render(&self) -> String {
        let mut out = String::new();
        for note in &self.notes {
            out.push_str(&format!("- The user asked you to remember: {note}
"));
        }
        for m in &self.memories {
            let how = if m.status == "conflict" {
                "CONFLICTS with something remembered earlier: if it changes the result, ask the user which is right".to_string()
            } else if m.evidence == "verified" || m.evidence == "user" {
                "confirmed".to_string()
            } else {
                "not verified".to_string()
            };
            let label = match m.kind.as_str() {
                "feedback" => format!("The user corrected you ({}x, {}, {})", m.hits, m.date, how),
                "semantic" => format!("Remembered rule ({}, {})", m.date, how),
                _ => format!("Earlier similar task ({}, {})", m.date, how),
            };
            out.push_str(&format!("- {label}: {}\n", m.text));
        }
        for p in &self.procedures {
            out.push_str(&format!("- Procedure that worked ({}x) for \"{}\": {}\n", p.success_count, p.goal, p.steps));
        }
        for f in &self.failures {
            out.push_str(&format!(
                "- Known failure: {} -> {} ({}x) when {}{}\n",
                f.tool,
                f.error_code,
                f.count,
                f.context,
                f.recovery.as_ref().map(|r| format!("; recovery that worked: {r}")).unwrap_or_default()
            ));
        }
        for (app, key, value) in &self.app_state {
            out.push_str(&format!("- Known state of {app}: {key} = {value}\n"));
        }
        out
    }
}

pub struct Memory {
    conn: Mutex<Connection>,
    redactor: Redactor,
}

pub fn keywords(text: &str) -> Vec<String> {
    let mut seen = HashSet::new();
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| w.chars().count() >= MIN_KEYWORD_CHARS)
        .filter(|w| seen.insert(w.to_string()))
        .map(String::from)
        .collect()
}

fn truncate(text: &str) -> String {
    text.chars().take(MAX_STORED_TEXT).collect()
}

impl Memory {
    pub fn open(path: &Path, redactor: Redactor) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(path).with_context(|| format!("opening {}", path.display()))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.execute_batch(SCHEMA)?;
        for statement in MIGRATIONS {
            let _ = conn.execute(statement, []); // "duplicate column" on a database that already has it
        }
        Ok(Self { conn: Mutex::new(conn), redactor })
    }

    fn conn(&self) -> Result<std::sync::MutexGuard<'_, Connection>> {
        self.conn.lock().map_err(|_| anyhow!("memory lock poisoned"))
    }

    fn clean(&self, text: &str) -> String {
        truncate(&self.redactor.redact(text))
    }

    pub fn integrity_check(&self) -> Result<String> {
        let conn = self.conn()?;
        Ok(conn.query_row("PRAGMA integrity_check", [], |row| row.get(0))?)
    }

    pub fn record_task(&self, task_id: &str, goal: &str, status: &str, summary: Option<&str>) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn()?.execute(
            "INSERT INTO tasks (task_id, goal, status, summary, created_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?5)
             ON CONFLICT(task_id) DO UPDATE SET status = excluded.status, summary = excluded.summary, updated_at = excluded.updated_at",
            params![task_id, self.clean(goal), status, summary.map(|s| self.clean(s)), now],
        )?;
        Ok(())
    }

    pub fn record_procedure(&self, goal: &str, steps: &str) -> Result<()> {
        let goal = self.clean(goal);
        let kw = keywords(&goal).join(" ");
        self.conn()?.execute(
            "INSERT INTO procedures (goal, keywords, steps, success_count, last_used) VALUES (?1, ?2, ?3, 1, ?4)
             ON CONFLICT(goal) DO UPDATE SET steps = excluded.steps, success_count = success_count + 1, last_used = excluded.last_used",
            params![goal, kw, self.clean(steps), Utc::now().to_rfc3339()],
        )?;
        Ok(())
    }

    pub fn record_failure(&self, tool: &str, error_code: &str, context: &str, recovery: Option<&str>) -> Result<()> {
        self.conn()?.execute(
            "INSERT INTO failures (tool, error_code, context, recovery, count, last_seen) VALUES (?1, ?2, ?3, ?4, 1, ?5)
             ON CONFLICT(tool, error_code, context) DO UPDATE SET count = count + 1, last_seen = excluded.last_seen,
             recovery = COALESCE(excluded.recovery, recovery)",
            params![tool, error_code, self.clean(context), recovery.map(|r| self.clean(r)), Utc::now().to_rfc3339()],
        )?;
        Ok(())
    }

    pub fn set_app_state(&self, app: &str, key: &str, value: &str) -> Result<()> {
        self.conn()?.execute(
            "INSERT INTO app_state (app, key, value, updated_at) VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(app, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            params![app, key, self.clean(value), Utc::now().to_rfc3339()],
        )?;
        Ok(())
    }

    pub fn app_state(&self, app: &str, key: &str) -> Result<Option<String>> {
        Ok(self
            .conn()?
            .query_row("SELECT value FROM app_state WHERE app = ?1 AND key = ?2", params![app, key], |r| r.get(0))
            .optional()?)
    }

    /// Remember a note for future tasks. Returns false when the same note is already stored.
    pub fn remember(&self, note: &str) -> Result<bool> {
        anyhow::ensure!(!self.looks_sensitive(note.trim()), "this looks like a password, key or token, so it was not stored");
        let note = self.clean(note.trim());
        anyhow::ensure!(!note.is_empty(), "the note is empty");
        let added = self.conn()?.execute(
            "INSERT INTO user_notes (note, created_at) VALUES (?1, ?2) ON CONFLICT(note) DO NOTHING",
            params![note, Utc::now().to_rfc3339()],
        )?;
        Ok(added > 0)
    }

    /// Forget every note containing `text` (case-insensitive). Returns the removed notes.
    pub fn forget(&self, text: &str) -> Result<Vec<String>> {
        let needle = text.trim().to_lowercase();
        anyhow::ensure!(!needle.is_empty(), "say which note to forget");
        let conn = self.conn()?;
        let matching: Vec<(i64, String)> = conn
            .prepare("SELECT id, note FROM user_notes")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
            .filter_map(|r| r.ok())
            .filter(|(_, note): &(i64, String)| note.to_lowercase().contains(&needle))
            .collect();
        for (id, _) in &matching {
            conn.execute("DELETE FROM user_notes WHERE id = ?1", params![id])?;
        }
        Ok(matching.into_iter().map(|(_, note)| note).collect())
    }

    /// Remembered notes, newest first.
    pub fn notes(&self, limit: usize) -> Result<Vec<String>> {
        let conn = self.conn()?;
        let notes = conn
            .prepare("SELECT note FROM user_notes WHERE disabled = 0 ORDER BY id DESC LIMIT ?1")?
            .query_map(params![limit as i64], |r| r.get(0))?
            .filter_map(|r| r.ok())
            .collect();
        Ok(notes)
    }

    /// Items relevant to `goal`, ranked by keyword overlap.
    pub fn recall(&self, goal: &str, limit: usize) -> Result<Recall> {
        let wanted: HashSet<String> = keywords(goal).into_iter().collect();
        let conn = self.conn()?;
        let score = |text: &str| keywords(text).iter().filter(|k| wanted.contains(*k)).count();

        let mut procedures: Vec<(usize, Procedure)> = conn
            .prepare("SELECT goal, keywords, steps, success_count FROM procedures WHERE disabled = 0")?
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, i64>(3)?))
            })?
            .filter_map(|r| r.ok())
            .map(|(goal, kw, steps, count)| (score(&kw), Procedure { goal, steps, success_count: count }))
            .filter(|(s, _)| *s > 0)
            .collect();
        procedures.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.success_count.cmp(&a.1.success_count)));

        let mut failures: Vec<(usize, FailurePattern)> = conn
            .prepare("SELECT tool, error_code, context, recovery, count FROM failures WHERE disabled = 0")?
            .query_map([], |r| {
                Ok(FailurePattern {
                    tool: r.get(0)?,
                    error_code: r.get(1)?,
                    context: r.get(2)?,
                    recovery: r.get(3)?,
                    count: r.get(4)?,
                })
            })?
            .filter_map(|r| r.ok())
            .map(|f| (score(&format!("{} {}", f.tool, f.context)), f))
            .filter(|(s, _)| *s > 0)
            .collect();
        failures.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.count.cmp(&a.1.count)));

        let app_state: Vec<(String, String, String)> = conn
            .prepare("SELECT app, key, value FROM app_state ORDER BY updated_at DESC")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
            .filter_map(|r| r.ok())
            .filter(|(app, key, _): &(String, String, String)| score(&format!("{app} {key}")) > 0)
            .take(limit)
            .collect();

        drop(conn);
        Ok(Recall {
            procedures: procedures.into_iter().take(limit).map(|(_, p)| p).collect(),
            failures: failures.into_iter().take(limit).map(|(_, f)| f).collect(),
            app_state,
            notes: self.notes(MAX_PROMPT_NOTES)?,
            memories: self.recall_memories(goal)?,
        })
    }

    /// True when `text` carries a credential or secret (a configured secret value, or a "password: ..." style
    /// statement). Such text is never written to long-term memory.
    pub fn looks_sensitive(&self, text: &str) -> bool {
        if self.redactor.redact(text) != text {
            return true;
        }
        let lower = text.to_lowercase();
        ["password", "mật khẩu", "mat khau", "passwd", "api key", "api_key", "apikey", "secret", "token", "private key", "otp"]
            .iter()
            .any(|k| lower.contains(k) && lower.chars().any(|c| c.is_ascii_digit()) && lower.matches(|c: char| ":=".contains(c)).count() > 0)
    }

    fn upsert_memory(&self, kind: &str, scope: &str, text: &str, source: &str, task_id: Option<&str>, confidence: f64, evidence: &str) -> Result<i64> {
        let text = self.clean(text.trim());
        anyhow::ensure!(!text.is_empty(), "the memory is empty");
        let now = Utc::now().to_rfc3339();
        let conn = self.conn()?;
        conn.execute(
            "INSERT INTO memories (kind, scope, text, keywords, source, task_id, confidence, evidence, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?9)
             ON CONFLICT(kind, text) DO UPDATE SET hits = hits + 1, updated_at = excluded.updated_at,
               confidence = MAX(confidence, excluded.confidence),
               status = CASE WHEN status = 'disabled' THEN 'disabled' ELSE status END",
            params![kind, scope, text, keywords(&text).join(" "), source, task_id, confidence, evidence, now],
        )?;
        Ok(conn.query_row("SELECT id FROM memories WHERE kind = ?1 AND text = ?2", params![kind, text], |r| r.get(0))?)
    }

    /// One line for a finished task: what was asked, how it ended, with which tools. Built from the task record
    /// (no model call). Unverified outcomes are stored as such; a failure keeps only the reason that was observed.
    pub fn record_episode(&self, task_id: &str, goal: &str, status: &str, summary: Option<&str>, failure: Option<&str>, tools: &[String], steps: usize, verified: bool) -> Result<()> {
        if goal.trim().is_empty() || self.looks_sensitive(goal) {
            return Ok(());
        }
        let outcome = match status {
            "completed" => "xong",
            "failed" => "thất bại",
            "cancelled" => "bị dừng",
            other => other,
        };
        let mut text = format!("Yêu cầu: {} → {outcome}", goal.trim().chars().take(160).collect::<String>());
        if !tools.is_empty() {
            text.push_str(&format!("; công cụ: {}", tools.iter().take(6).cloned().collect::<Vec<_>>().join(", ")));
        }
        text.push_str(&format!("; {steps} bước"));
        if let Some(s) = summary.map(str::trim).filter(|s| !s.is_empty() && status == "completed") {
            text.push_str(&format!("; kết quả: {}", s.replace('\n', " ").chars().take(180).collect::<String>()));
        }
        if let Some(f) = failure.map(str::trim).filter(|f| !f.is_empty() && status != "completed") {
            text.push_str(&format!("; lý do: {}", f.replace('\n', " ").chars().take(180).collect::<String>()));
        }
        let (confidence, evidence) = match (status, verified) {
            ("completed", true) => (0.8, "verified"),
            ("completed", false) => (0.55, "unverified"),
            ("failed", _) => (0.6, "verified"), // the failure was observed, its cause may still be a guess
            _ => (0.4, "unverified"),
        };
        self.upsert_memory("episodic", "project", &text, "auto", Some(task_id), confidence, evidence)?;
        Ok(())
    }

    /// A rating or correction from the user. `rating`: up | down | fix. Stored with the request it concerns so it
    /// only applies to similar requests; the same correction seen again raises `hits`, it is never generalised.
    pub fn record_feedback(&self, task_id: &str, goal: &str, rating: &str, comment: &str, category: &str) -> Result<i64> {
        anyhow::ensure!(["up", "down", "fix"].contains(&rating), "rating must be up, down or fix");
        let comment = comment.trim();
        anyhow::ensure!(rating == "up" || !comment.is_empty(), "say what should be different");
        anyhow::ensure!(!self.looks_sensitive(comment), "the comment looks like a secret and was not stored");
        let goal_short: String = goal.trim().chars().take(120).collect();
        let text = match rating {
            "up" => format!("Với việc \"{goal_short}\": cách làm được chấp nhận{}", if comment.is_empty() { String::new() } else { format!(" ({comment})") }),
            _ => format!("Với việc \"{goal_short}\": {comment}{}", if category.is_empty() { String::new() } else { format!(" [lỗi: {category}]") }),
        };
        self.upsert_memory("feedback", "project", &format!("{text}"), "feedback", Some(task_id), if rating == "up" { 0.7 } else { 0.9 }, "user")
    }

    /// A lasting fact or rule the user states. Refuses secrets; a near-duplicate with different wording is stored
    /// with status "conflict" so neither silently wins.
    pub fn add_semantic(&self, text: &str, scope: &str) -> Result<(i64, Option<i64>)> {
        let text = text.trim();
        anyhow::ensure!(!text.is_empty(), "the memory is empty");
        anyhow::ensure!(!self.looks_sensitive(text), "this looks like a password, key or token, so it was not stored");
        let scope = if ["global", "project", "session"].contains(&scope) { scope } else { "global" };
        let wanted: HashSet<String> = keywords(text).into_iter().collect();
        let mut clash = None;
        {
            let conn = self.conn()?;
            let rows: Vec<(i64, String, String)> = conn
                .prepare("SELECT id, text, keywords FROM memories WHERE kind = 'semantic' AND status = 'active'")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                .filter_map(|r| r.ok())
                .collect();
            for (id, existing, kw) in rows {
                let have: HashSet<String> = kw.split_whitespace().map(String::from).collect();
                let inter = wanted.intersection(&have).count();
                let union = wanted.union(&have).count().max(1);
                if existing != text && inter >= 3 && inter * 10 >= union * 6 {
                    clash = Some(id);
                    break;
                }
            }
        }
        let id = self.upsert_memory("semantic", scope, text, "user", None, 0.95, "user")?;
        if let Some(other) = clash {
            self.conn()?.execute("UPDATE memories SET status = 'conflict' WHERE id IN (?1, ?2)", params![id, other])?;
        }
        Ok((id, clash))
    }

    fn recall_memories(&self, goal: &str) -> Result<Vec<RecalledMemory>> {
        let wanted: HashSet<String> = keywords(goal).into_iter().collect();
        if wanted.is_empty() {
            return Ok(Vec::new());
        }
        let conn = self.conn()?;
        let rows: Vec<(i64, String, String, String, f64, String, String, i64, String, String)> = conn
            .prepare("SELECT id, kind, text, keywords, confidence, evidence, status, hits, updated_at, scope FROM memories WHERE status != 'disabled'")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?)))?
            .filter_map(|r| r.ok())
            .collect();
        let mut scored: Vec<(f64, RecalledMemory)> = Vec::new();
        for (id, kind, text, kw, confidence, evidence, status, hits, updated, scope) in rows {
            let overlap = kw.split_whitespace().filter(|k| wanted.contains(*k)).count();
            let needed = if kind == "episodic" { 2 } else { 1 };
            if overlap < needed || (scope == "session") {
                continue;
            }
            let score = overlap as f64 * confidence * (1.0 + (hits as f64).ln_1p()) * if kind == "feedback" { 1.5 } else { 1.0 };
            scored.push((score, RecalledMemory { id, kind, text, confidence, evidence, status, hits, date: updated.chars().take(10).collect() }));
        }
        scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        let picked: Vec<RecalledMemory> = scored.into_iter().take(MAX_RECALLED_MEMORIES).map(|(_, m)| m).collect();
        let now = Utc::now().to_rfc3339();
        for m in &picked {
            let _ = conn.execute("UPDATE memories SET use_count = use_count + 1, last_used = ?2 WHERE id = ?1", params![m.id, now]);
        }
        Ok(picked)
    }

    /// Everything remembered, for the settings page: [{id, kind, text, source, ...}]. Ids are "<table>:<n>".
    pub fn list_all(&self, query: &str) -> Result<Vec<serde_json::Value>> {
        use serde_json::json;
        let needle = query.trim().to_lowercase();
        let conn = self.conn()?;
        let mut out: Vec<serde_json::Value> = Vec::new();
        let mut push = |v: serde_json::Value| {
            if needle.is_empty() || v["text"].as_str().is_some_and(|t| t.to_lowercase().contains(&needle)) {
                out.push(v);
            }
        };
        for r in conn.prepare("SELECT id, note, created_at, disabled FROM user_notes ORDER BY id DESC")?.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, i64>(3)?)))?.flatten() {
            push(json!({"id": format!("n:{}", r.0), "kind": "semantic", "scope": "global", "text": r.1, "source": "user", "confidence": 0.95, "evidence": "user",
                        "status": if r.3 == 1 { "disabled" } else { "active" }, "hits": 1, "use_count": 0, "created_at": r.2, "updated_at": r.2, "editable": true}));
        }
        for r in conn.prepare("SELECT id, kind, scope, text, source, task_id, confidence, evidence, status, hits, use_count, created_at, updated_at FROM memories ORDER BY updated_at DESC")?
            .query_map([], |r| Ok(json!({"id": format!("m:{}", r.get::<_, i64>(0)?), "kind": r.get::<_, String>(1)?, "scope": r.get::<_, String>(2)?, "text": r.get::<_, String>(3)?,
                "source": r.get::<_, String>(4)?, "task_id": r.get::<_, Option<String>>(5)?, "confidence": r.get::<_, f64>(6)?, "evidence": r.get::<_, String>(7)?,
                "status": r.get::<_, String>(8)?, "hits": r.get::<_, i64>(9)?, "use_count": r.get::<_, i64>(10)?, "created_at": r.get::<_, String>(11)?,
                "updated_at": r.get::<_, String>(12)?, "editable": true})))?
            .flatten()
        {
            push(r);
        }
        for r in conn.prepare("SELECT id, goal, steps, success_count, last_used, disabled FROM procedures ORDER BY last_used DESC")?.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, i64>(3)?, r.get::<_, String>(4)?, r.get::<_, i64>(5)?)))?.flatten() {
            push(json!({"id": format!("p:{}", r.0), "kind": "procedural", "scope": "project", "text": format!("{} → {}", r.1, r.2), "source": "auto", "confidence": 0.8,
                        "evidence": "verified", "status": if r.5 == 1 { "disabled" } else { "active" }, "hits": r.3, "use_count": r.3, "created_at": r.4, "updated_at": r.4, "editable": false}));
        }
        for r in conn.prepare("SELECT id, tool, error_code, context, recovery, count, last_seen, disabled FROM failures ORDER BY last_seen DESC")?.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?, r.get::<_, Option<String>>(4)?, r.get::<_, i64>(5)?, r.get::<_, String>(6)?, r.get::<_, i64>(7)?)))?.flatten() {
            push(json!({"id": format!("f:{}", r.0), "kind": "failure", "scope": "project", "text": format!("{} → {} khi {}{}", r.1, r.2, r.3, r.4.map(|x| format!("; cách khắc phục: {x}")).unwrap_or_default()),
                        "source": "auto", "confidence": 0.6, "evidence": "verified", "status": if r.7 == 1 { "disabled" } else { "active" }, "hits": r.5, "use_count": r.5,
                        "created_at": r.6, "updated_at": r.6, "editable": false}));
        }
        Ok(out)
    }

    fn split_id(id: &str) -> Result<(&str, i64)> {
        let (table, n) = id.split_once(':').ok_or_else(|| anyhow!("bad memory id"))?;
        anyhow::ensure!(["n", "m", "p", "f"].contains(&table), "bad memory id");
        Ok((table, n.parse().map_err(|_| anyhow!("bad memory id"))?))
    }

    /// Switch a memory off/on (`status` active|disabled) and/or rewrite its text (notes and "m:" memories only).
    pub fn update_memory(&self, id: &str, text: Option<&str>, status: Option<&str>) -> Result<()> {
        let (table, n) = Self::split_id(id)?;
        if let Some(status) = status {
            anyhow::ensure!(["active", "disabled"].contains(&status), "status must be active or disabled");
            let conn = self.conn()?;
            let off = (status == "disabled") as i64;
            match table {
                "n" => conn.execute("UPDATE user_notes SET disabled = ?2 WHERE id = ?1", params![n, off])?,
                "p" => conn.execute("UPDATE procedures SET disabled = ?2 WHERE id = ?1", params![n, off])?,
                "f" => conn.execute("UPDATE failures SET disabled = ?2 WHERE id = ?1", params![n, off])?,
                _ => conn.execute("UPDATE memories SET status = ?2, updated_at = ?3 WHERE id = ?1", params![n, status, Utc::now().to_rfc3339()])?,
            };
        }
        if let Some(text) = text {
            let text = text.trim();
            anyhow::ensure!(!text.is_empty(), "the memory is empty");
            anyhow::ensure!(!self.looks_sensitive(text), "this looks like a password, key or token, so it was not stored");
            let text = self.clean(text);
            let conn = self.conn()?;
            match table {
                "n" => conn.execute("UPDATE user_notes SET note = ?2 WHERE id = ?1", params![n, text])?,
                "m" => conn.execute(
                    "UPDATE memories SET text = ?2, keywords = ?3, status = CASE WHEN status = 'conflict' THEN 'active' ELSE status END, updated_at = ?4 WHERE id = ?1",
                    params![n, text, keywords(&text).join(" "), Utc::now().to_rfc3339()],
                )?,
                _ => anyhow::bail!("this kind of memory cannot be edited, only switched off or deleted"),
            };
        }
        Ok(())
    }

    pub fn delete_memory(&self, id: &str) -> Result<bool> {
        let (table, n) = Self::split_id(id)?;
        let sql = match table {
            "n" => "DELETE FROM user_notes WHERE id = ?1",
            "p" => "DELETE FROM procedures WHERE id = ?1",
            "f" => "DELETE FROM failures WHERE id = ?1",
            _ => "DELETE FROM memories WHERE id = ?1",
        };
        Ok(self.conn()?.execute(sql, params![n])? > 0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recall_ranks_by_keywords_and_redacts() {
        let dir = tempfile::tempdir().unwrap();
        let m = Memory::open(&dir.path().join("m.db"), Redactor::new()).unwrap();
        m.record_procedure("Open notepad and save hello.txt", "desktop.launch_app notepad -> desktop.type -> desktop.hotkey ctrl+s").unwrap();
        m.record_procedure("Draw a circle in AutoCAD", "autocad.create_circle").unwrap();
        m.record_failure("desktop.click", "STALE_ELEMENT", "notepad save dialog", Some("re-inspect")).unwrap();
        m.set_app_state("autocad", "last_drawing", "C:\\temp\\a.dwg password=hunter22").unwrap();
        let recall = m.recall("mở notepad rồi save file", 5).unwrap();
        assert_eq!(recall.procedures.len(), 1);
        assert!(recall.procedures[0].goal.contains("notepad"));
        assert_eq!(recall.failures.len(), 1);
        let stored = m.app_state("autocad", "last_drawing").unwrap().unwrap();
        assert!(!stored.contains("hunter22"));
        assert_eq!(m.integrity_check().unwrap(), "ok");
    }

    #[test]
    fn notes_are_remembered_recalled_and_forgotten() {
        let dir = tempfile::tempdir().unwrap();
        let m = Memory::open(&dir.path().join("m.db"), Redactor::new()).unwrap();
        assert!(m.remember("Tôi thích câu trả lời ngắn gọn").unwrap());
        assert!(!m.remember("Tôi thích câu trả lời ngắn gọn").unwrap());
        m.remember("Sếp của tôi tên Hùng").unwrap();
        let recall = m.recall("việc gì đó không liên quan", 5).unwrap();
        assert_eq!(recall.notes, vec!["Sếp của tôi tên Hùng", "Tôi thích câu trả lời ngắn gọn"]);
        assert!(recall.render().contains("remember: Sếp của tôi tên Hùng"));
        assert_eq!(m.forget("sếp").unwrap(), vec!["Sếp của tôi tên Hùng"]);
        assert_eq!(m.notes(10).unwrap().len(), 1);
    }

    #[test]
    fn episodes_feedback_and_semantic_memories_are_recalled_only_when_relevant() {
        let dir = tempfile::tempdir().unwrap();
        let m = Memory::open(&dir.path().join("m.db"), Redactor::new()).unwrap();
        m.record_episode("t1", "Lập báo cáo doanh thu tuần từ file Excel", "completed", Some("Đã tạo bao_cao.docx"), None, &["office.excel_read".into(), "office.word_write".into()], 4, true).unwrap();
        m.record_episode("t2", "Vẽ con mèo trong Paint", "failed", None, Some("Paint không lấy được foreground"), &["paint.draw".into()], 2, false).unwrap();
        m.record_feedback("t1", "Lập báo cáo doanh thu tuần từ file Excel", "fix", "Phải cộng cả cột phụ phí khi tính tổng", "reasoning").unwrap();
        let recall = m.recall("làm báo cáo doanh thu tháng 10 bằng Excel", 5).unwrap();
        let text = recall.render();
        assert!(text.contains("phụ phí"), "{text}");
        assert!(text.contains("Earlier similar task"), "{text}");
        assert!(!text.contains("con mèo"), "an unrelated episode is not injected: {text}");
        let unrelated = m.recall("dịch câu này sang tiếng Anh", 5).unwrap();
        assert!(unrelated.memories.is_empty());
    }

    #[test]
    fn corrections_repeat_without_becoming_general_rules_and_deleted_memories_vanish() {
        let dir = tempfile::tempdir().unwrap();
        let m = Memory::open(&dir.path().join("m.db"), Redactor::new()).unwrap();
        let id = m.record_feedback("t1", "xuất báo cáo pdf", "fix", "Đặt tên file theo ngày", "tool").unwrap();
        m.record_feedback("t2", "xuất báo cáo pdf", "fix", "Đặt tên file theo ngày", "tool").unwrap();
        let all = m.list_all("").unwrap();
        let row = all.iter().find(|v| v["id"] == format!("m:{id}")).unwrap();
        assert_eq!(row["hits"], 2);
        assert!(m.recall("xuất báo cáo pdf", 5).unwrap().render().contains("2x"));
        assert!(m.recall("mở notepad", 5).unwrap().memories.is_empty());
        m.update_memory(&format!("m:{id}"), None, Some("disabled")).unwrap();
        assert!(m.recall("xuất báo cáo pdf", 5).unwrap().memories.is_empty(), "a disabled memory is not retrieved");
        m.update_memory(&format!("m:{id}"), None, Some("active")).unwrap();
        assert!(m.delete_memory(&format!("m:{id}")).unwrap());
        assert!(m.recall("xuất báo cáo pdf", 5).unwrap().memories.is_empty());
    }

    #[test]
    fn secrets_are_not_remembered_and_near_duplicates_are_flagged() {
        let dir = tempfile::tempdir().unwrap();
        let m = Memory::open(&dir.path().join("m.db"), Redactor::new()).unwrap();
        assert!(m.add_semantic("Mật khẩu wifi của tôi là: abc12345", "global").is_err());
        assert!(m.add_semantic("api_key = sk-123456", "global").is_err());
        let (first, clash) = m.add_semantic("Luôn lưu báo cáo tuần vào thư mục D:\\Bao cao", "project").unwrap();
        assert!(clash.is_none());
        let (second, clash) = m.add_semantic("Luôn lưu báo cáo tuần vào thư mục E:\\Tai lieu", "project").unwrap();
        assert_eq!(clash, Some(first));
        let all = m.list_all("báo cáo").unwrap();
        let statuses: Vec<_> = all.iter().filter(|v| v["id"] == format!("m:{first}") || v["id"] == format!("m:{second}")).map(|v| v["status"].as_str().unwrap().to_string()).collect();
        assert_eq!(statuses, vec!["conflict", "conflict"]);
        let text = m.recall("lưu báo cáo tuần", 5).unwrap().render();
        assert!(text.contains("CONFLICTS"), "{text}");
        assert!(m.record_feedback("t", "x", "down", "", "").is_err(), "a rejection must say what should change");
    }

    #[test]
    fn legacy_notes_can_be_switched_off_and_the_database_migrates() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("m.db");
        {
            // a database made before the `disabled` column existed
            let c = Connection::open(&path).unwrap();
            c.execute_batch("CREATE TABLE user_notes (id INTEGER PRIMARY KEY, note TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL); INSERT INTO user_notes (note, created_at) VALUES ('cũ', '2026-01-01');").unwrap();
        }
        let m = Memory::open(&path, Redactor::new()).unwrap();
        assert_eq!(m.notes(10).unwrap(), vec!["cũ"]);
        let id = m.list_all("").unwrap()[0]["id"].as_str().unwrap().to_string();
        m.update_memory(&id, None, Some("disabled")).unwrap();
        assert!(m.notes(10).unwrap().is_empty());
        assert_eq!(m.integrity_check().unwrap(), "ok");
    }
}
