//! Cross-process task cancellation.
//!
//! `agent cancel <id>` (another process) creates `memory/tasks/<id>.cancel`. The running task polls
//! for that marker and also exposes an in-process token (Ctrl+C).

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub const POLL_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Clone)]
pub struct CancelToken {
    flag: Arc<AtomicBool>,
    marker: PathBuf,
    pause_marker: PathBuf,
    activity_pause_marker: PathBuf,
}

pub fn marker_path(tasks_dir: &Path, task_id: &str) -> PathBuf {
    tasks_dir.join(format!("{task_id}.cancel"))
}

pub fn pause_marker_path(tasks_dir: &Path, task_id: &str) -> PathBuf {
    tasks_dir.join(format!("{task_id}.pause"))
}

/// Ask a running task to pause before its next step (`agent pause`).
pub fn request_pause(tasks_dir: &Path, task_id: &str) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(tasks_dir)?;
    let path = pause_marker_path(tasks_dir, task_id);
    std::fs::write(&path, chrono::Utc::now().to_rfc3339())?;
    Ok(path)
}

/// Let a paused task continue (`agent unpause`).
pub fn clear_pause(tasks_dir: &Path, task_id: &str) -> std::io::Result<()> {
    match std::fs::remove_file(pause_marker_path(tasks_dir, task_id)) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
        _ => Ok(()),
    }
}

impl CancelToken {
    pub fn new(tasks_dir: &Path, task_id: &str) -> Self {
        Self {
            flag: Arc::new(AtomicBool::new(false)),
            marker: marker_path(tasks_dir, task_id),
            pause_marker: pause_marker_path(tasks_dir, task_id),
            activity_pause_marker: tasks_dir.join(format!("{task_id}.activity-pause")),
        }
    }

    pub fn is_paused(&self) -> bool {
        self.pause_marker.exists()
    }

    pub fn is_activity_paused(&self) -> bool {
        self.activity_pause_marker.exists()
    }

    pub fn pause_for_activity(&self) -> std::io::Result<()> {
        std::fs::write(&self.activity_pause_marker, chrono::Utc::now().to_rfc3339())
    }

    pub fn clear_activity_pause(&self) -> std::io::Result<()> {
        match std::fs::remove_file(&self.activity_pause_marker) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        }
    }

    pub fn cancel(&self) {
        self.flag.store(true, Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        if self.flag.load(Ordering::SeqCst) {
            return true;
        }
        if self.marker.exists() {
            self.flag.store(true, Ordering::SeqCst);
            return true;
        }
        false
    }

    /// Resolves once the task is cancelled.
    pub async fn cancelled(&self) {
        while !self.is_cancelled() {
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    }

    /// Remove a stale marker (e.g. before resuming a task the user cancelled earlier).
    pub fn clear(&self) -> std::io::Result<()> {
        self.flag.store(false, Ordering::SeqCst);
        match std::fs::remove_file(&self.marker) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        }
    }
}

/// Request cancellation of a task running in another process.
pub fn request_cancel(tasks_dir: &Path, task_id: &str) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(tasks_dir)?;
    let path = marker_path(tasks_dir, task_id);
    std::fs::write(&path, chrono::Utc::now().to_rfc3339())?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    #[test]
    fn activity_pause_is_distinct_from_a_manual_pause() {
        let dir = tempfile::tempdir().unwrap();
        let token = super::CancelToken::new(dir.path(), "activity-test");

        token.pause_for_activity().unwrap();
        assert!(token.is_activity_paused());
        assert!(!token.is_paused());
        token.clear_activity_pause().unwrap();
        assert!(!token.is_activity_paused());

        super::request_pause(dir.path(), "activity-test").unwrap();
        assert!(token.is_paused());
        assert!(!token.is_activity_paused());
        super::clear_pause(dir.path(), "activity-test").unwrap();
    }
}
