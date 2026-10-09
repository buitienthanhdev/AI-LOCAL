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
"#;
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
}

impl Recall {
    pub fn is_empty(&self) -> bool {
        self.procedures.is_empty() && self.failures.is_empty() && self.app_state.is_empty() && self.notes.is_empty()
    }

    pub fn render(&self) -> String {
        let mut out = String::new();
        for note in &self.notes {
            out.push_str(&format!("- The user asked you to remember: {note}
"));
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
        conn.execute_batch(SCHEMA)?;
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
            .prepare("SELECT note FROM user_notes ORDER BY id DESC LIMIT ?1")?
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
            .prepare("SELECT goal, keywords, steps, success_count FROM procedures")?
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, i64>(3)?))
            })?
            .filter_map(|r| r.ok())
            .map(|(goal, kw, steps, count)| (score(&kw), Procedure { goal, steps, success_count: count }))
            .filter(|(s, _)| *s > 0)
            .collect();
        procedures.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.success_count.cmp(&a.1.success_count)));

        let mut failures: Vec<(usize, FailurePattern)> = conn
            .prepare("SELECT tool, error_code, context, recovery, count FROM failures")?
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
        })
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
}
