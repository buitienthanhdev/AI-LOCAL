//! Supervision of the backend processes the gateway manages.
//!
//! Each managed backend runs as a child process inside a Job Object with KILL_ON_JOB_CLOSE, so the
//! backends die with the gateway even if the gateway is terminated abruptly. Child output is redacted
//! line by line before it is written to logs/gateway/<backend>.log. Restarts are rate limited.

use std::collections::{HashMap, VecDeque};
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Result};
use lga_core::redact::Redactor;
use serde_json::{json, Value};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

/// At most this many restarts within the window; afterwards the backend stays down until the window passes.
const MAX_RESTARTS_PER_WINDOW: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(600);
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

/// A Job Object that kills every assigned process when the gateway exits.
pub struct KillOnCloseJob(HANDLE);

// SAFETY: a job handle is a process-wide kernel handle; Windows job APIs are thread-safe.
unsafe impl Send for KillOnCloseJob {}
unsafe impl Sync for KillOnCloseJob {}

impl KillOnCloseJob {
    pub fn new() -> Result<Self> {
        // SAFETY: null attributes/name create an anonymous job; `info` is a plain-old-data struct passed by
        // pointer with its exact size.
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                bail!("CreateJobObjectW failed: {}", std::io::Error::last_os_error());
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                CloseHandle(job);
                bail!("SetInformationJobObject failed: {}", std::io::Error::last_os_error());
            }
            Ok(Self(job))
        }
    }

    fn assign(&self, child: &Child) -> Result<()> {
        // SAFETY: both handles are valid for the duration of the call.
        if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) } == 0 {
            bail!("AssignProcessToJobObject failed: {}", std::io::Error::last_os_error());
        }
        Ok(())
    }
}

impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        // SAFETY: the handle is owned by this struct.
        unsafe { CloseHandle(self.0) };
    }
}

/// Start a server that must outlive the gateway (not in the kill-on-close job): hidden console, own
/// process group, outside any job the gateway may be in when that is allowed.
pub fn spawn_independent(program: &std::path::Path, args: &[String]) -> std::io::Result<()> {
    const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
    let base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP;
    let spawn = |flags: u32| {
        Command::new(program).args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).creation_flags(flags).spawn()
    };
    spawn(base | CREATE_BREAKAWAY_FROM_JOB).or_else(|_| spawn(base)).map(|_| ())
}

#[derive(Debug, Clone)]
pub struct ProcessSpec {
    pub name: String,
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub env: HashMap<String, String>,
    pub log_path: Option<PathBuf>,
}

#[derive(Default)]
struct State {
    child: Option<Child>,
    restarts: VecDeque<Instant>,
    last_exit_code: Option<i32>,
    start_count: u32,
}

pub struct ManagedProcess {
    pub spec: ProcessSpec,
    job: Arc<KillOnCloseJob>,
    redactor: Arc<Redactor>,
    state: Mutex<State>,
}

impl ManagedProcess {
    pub fn new(spec: ProcessSpec, job: Arc<KillOnCloseJob>, redactor: Arc<Redactor>) -> Self {
        Self { spec, job, redactor, state: Mutex::new(State::default()) }
    }

    fn running(state: &mut State) -> bool {
        let Some(child) = state.child.as_mut() else { return false };
        match child.try_wait() {
            Ok(Some(status)) => {
                state.last_exit_code = status.code();
                state.child = None;
                false
            }
            Ok(None) => true,
            Err(_) => false,
        }
    }

    pub fn is_running(&self) -> bool {
        Self::running(&mut self.state.lock().expect("process lock"))
    }

    pub fn last_exit_code(&self) -> Option<i32> {
        self.state.lock().expect("process lock").last_exit_code
    }

    pub fn start(&self) -> Result<()> {
        let mut state = self.state.lock().expect("process lock");
        if Self::running(&mut state) {
            return Ok(());
        }
        if state.start_count > 0 {
            let now = Instant::now();
            while state.restarts.front().is_some_and(|t| now.duration_since(*t) > RESTART_WINDOW) {
                state.restarts.pop_front();
            }
            if state.restarts.len() >= MAX_RESTARTS_PER_WINDOW {
                bail!("{}: restart limit reached ({MAX_RESTARTS_PER_WINDOW} in {}s)", self.spec.name, RESTART_WINDOW.as_secs());
            }
            state.restarts.push_back(now);
        }
        let executable = PathBuf::from(&self.spec.argv[0]);
        if !executable.is_file() {
            bail!("{}: {} does not exist; run scripts/setup-gateway.ps1", self.spec.name, executable.display());
        }
        let mut child = Command::new(&executable)
            .args(&self.spec.argv[1..])
            .current_dir(&self.spec.cwd)
            .envs(&self.spec.env)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
            .spawn()
            .map_err(|e| anyhow!("{}: {e}", self.spec.name))?;
        if let Err(e) = self.job.assign(&child) {
            let _ = child.kill();
            return Err(e);
        }
        let log = self.spec.log_path.as_ref().and_then(|path| {
            std::fs::create_dir_all(path.parent()?).ok()?;
            OpenOptions::new().create(true).append(true).open(path).ok()
        });
        let log = Arc::new(Mutex::new(log));
        if let Some(out) = child.stdout.take() {
            self.pump(out, Arc::clone(&log));
        }
        if let Some(err) = child.stderr.take() {
            self.pump(err, log);
        }
        state.start_count += 1;
        crate::log::info(&format!("started {} (pid {})", self.spec.name, child.id()));
        state.child = Some(child);
        Ok(())
    }

    fn pump(&self, stream: impl Read + Send + 'static, log: Arc<Mutex<Option<std::fs::File>>>) {
        let redactor = Arc::clone(&self.redactor);
        std::thread::Builder::new()
            .name(format!("log-{}", self.spec.name))
            .spawn(move || {
                for line in BufReader::new(stream).split(b'\n').map_while(Result::ok) {
                    let text = redactor.redact(String::from_utf8_lossy(&line).trim_end());
                    if let Some(file) = log.lock().expect("log lock").as_mut() {
                        let _ = writeln!(file, "{} {text}", chrono::Local::now().format("%Y-%m-%dT%H:%M:%S"));
                    }
                }
            })
            .expect("spawning a log thread");
    }

    /// gemini-web2api-go has no graceful shutdown handler; terminating is the stop path.
    pub fn stop(&self) {
        let mut state = self.state.lock().expect("process lock");
        if let Some(mut child) = state.child.take() {
            let _ = child.kill();
            if let Ok(status) = child.wait() {
                state.last_exit_code = status.code();
            }
            crate::log::info(&format!("stopped {}", self.spec.name));
        }
    }

    pub fn status(&self) -> Value {
        let mut state = self.state.lock().expect("process lock");
        let running = Self::running(&mut state);
        json!({
            "managed": true,
            "running": running,
            "pid": state.child.as_ref().map(Child::id),
            "starts": state.start_count,
            "last_exit_code": state.last_exit_code,
        })
    }
}
