//! PowerShell execution with a hard timeout that also kills grandchildren.
//!
//! * PowerShell 7 (`pwsh`) when available, else Windows PowerShell 5.1.
//! * `-NoProfile -NonInteractive`, stdin closed: a script that prompts fails instead of hanging.
//! * The process is placed in a Job Object with KILL_ON_JOB_CLOSE; on timeout or cancellation the whole
//!   job is terminated. Because the agent itself may run inside a job that permits silent breakaway (some
//!   IDE/terminal hosts do), descendants are also enumerated and killed.
//! * stdout and stderr are captured separately, decoded as UTF-8, and capped at `max_output_bytes` each.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};

use base64::Engine;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Notify;

use crate::job::JobObject;

const ERROR_ELEVATION_REQUIRED: i32 = 740;
const ELEVATION_MARKERS: [&str; 4] = ["requires elevation", "run as administrator", "administrator privilege", "access is denied"];
/// After the process tree is killed, wait at most this long for the output pipes to drain. A process that
/// escaped the job (possible when the parent job allows breakaway) could otherwise hold them open.
const DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const UTF8_PREAMBLE: &str = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); \
$OutputEncoding = [System.Text.UTF8Encoding]::new($false); \
$ProgressPreference = 'SilentlyContinue'; \
if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }; ";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

#[derive(Debug, Clone)]
pub struct ShellResult {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    pub cancelled: bool,
    pub truncated: bool,
    pub duration_ms: u64,
}

impl ShellResult {
    pub fn looks_like_elevation_failure(&self) -> bool {
        if self.exit_code == ERROR_ELEVATION_REQUIRED {
            return true;
        }
        let lowered = self.stderr.to_lowercase();
        self.exit_code != 0 && ELEVATION_MARKERS.iter().any(|m| lowered.contains(m))
    }
}

/// Full path of an executable on PATH (with PATHEXT), or of `name` itself when it is a file.
pub fn which(name: &str) -> Option<PathBuf> {
    let direct = Path::new(name);
    if direct.components().count() > 1 {
        return direct.is_file().then(|| direct.to_path_buf());
    }
    let exts: Vec<String> = std::env::var("PATHEXT")
        .unwrap_or_else(|_| ".EXE;.CMD;.BAT;.COM".into())
        .split(';')
        .filter(|e| !e.is_empty())
        .map(str::to_ascii_lowercase)
        .collect();
    let has_ext = direct.extension().is_some();
    for dir in std::env::split_paths(&std::env::var_os("PATH")?) {
        if has_ext && dir.join(name).is_file() {
            return Some(dir.join(name));
        }
        for ext in &exts {
            let candidate = dir.join(format!("{name}{ext}"));
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

pub fn resolve_powershell(preferred: &str) -> Option<PathBuf> {
    [preferred, "pwsh", "powershell"].into_iter().filter(|c| !c.is_empty()).find_map(which)
}

/// Process ids of every descendant of `root` (snapshot taken now).
fn descendants(root: u32) -> Vec<u32> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };
    let mut pairs = Vec::new();
    // SAFETY: the snapshot handle is closed before returning; `entry` has its dwSize set as required.
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Vec::new();
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        if Process32FirstW(snapshot, &mut entry) != 0 {
            loop {
                pairs.push((entry.th32ProcessID, entry.th32ParentProcessID));
                if Process32NextW(snapshot, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snapshot);
    }
    let mut found = vec![root];
    let mut i = 0;
    while i < found.len() {
        let parent = found[i];
        for (pid, ppid) in &pairs {
            if *ppid == parent && *pid != parent && !found.contains(pid) {
                found.push(*pid);
            }
        }
        i += 1;
    }
    found.remove(0);
    found
}

fn kill_pid(pid: u32) {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};
    // SAFETY: the handle is checked and closed.
    unsafe {
        let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if !handle.is_null() {
            TerminateProcess(handle, 1);
            CloseHandle(handle);
        }
    }
}

async fn pump(mut stream: impl tokio::io::AsyncRead + Unpin, limit: usize) -> (Vec<u8>, usize) {
    let mut data = Vec::new();
    let mut dropped = 0usize;
    let mut chunk = vec![0u8; 65536];
    loop {
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return (data, dropped),
            Ok(n) => {
                let room = limit.saturating_sub(data.len());
                data.extend_from_slice(&chunk[..n.min(room)]);
                dropped += n.saturating_sub(room);
            }
        }
    }
}

/// The exit code follows what a person at a prompt would call success: an explicit `exit N`, a failing
/// native program (`$LASTEXITCODE`) or a terminating error (exit 1) fail; non-terminating errors that
/// were reported on stderr (e.g. "access denied" on a few folders during a recursive search) do not turn a
/// useful result into a failure. Without this, `powershell -Command` exits 1 whenever any error record
/// was written, even with `-ErrorAction SilentlyContinue`.
fn wrap_script(script: &str) -> String {
    format!(
        "{UTF8_PREAMBLE}$global:LASTEXITCODE = 0
try {{
{script}
}} catch {{ [Console]::Error.WriteLine(($_ | Out-String)); exit 1 }}
if ($LASTEXITCODE -is [int]) {{ exit $LASTEXITCODE }} else {{ exit 0 }}
"
    )
}

pub async fn run_powershell(
    script: &str,
    executable: &Path,
    cwd: Option<&Path>,
    timeout: Duration,
    max_output_bytes: usize,
    cancel: Arc<Notify>,
) -> std::io::Result<ShellResult> {
    let utf16: Vec<u8> = wrap_script(script).encode_utf16().flat_map(u16::to_le_bytes).collect();
    let encoded = base64::engine::general_purpose::STANDARD.encode(utf16);
    let started = Instant::now();
    let job = JobObject::kill_on_close_strict()?;
    let mut command = Command::new(executable);
    command
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", &encoded])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        .kill_on_drop(true);
    if let Some(dir) = cwd {
        command.current_dir(dir);
    }
    let mut child = command.spawn()?;
    let pid = child.id().unwrap_or(0);
    let _ = job.assign(pid);
    let out = tokio::spawn(pump(child.stdout.take().expect("piped stdout"), max_output_bytes));
    let err = tokio::spawn(pump(child.stderr.take().expect("piped stderr"), max_output_bytes));

    let (mut timed_out, mut cancelled) = (false, false);
    let status = tokio::select! {
        status = child.wait() => status.ok(),
        _ = tokio::time::sleep(timeout) => { timed_out = true; None }
        _ = cancel.notified() => { cancelled = true; None }
    };
    let exit_code = match status {
        Some(status) => status.code().unwrap_or(-1),
        None => {
            let tree = descendants(pid);
            job.terminate(1);
            for pid in tree {
                kill_pid(pid);
            }
            let _ = child.kill().await;
            child.wait().await.ok().and_then(|s| s.code()).unwrap_or(1)
        }
    };
    let drain = async { (out.await.unwrap_or_default(), err.await.unwrap_or_default()) };
    let ((stdout, out_dropped), (stderr, err_dropped), undrained) = match tokio::time::timeout(DRAIN_TIMEOUT, drain).await {
        Ok((o, e)) => (o, e, false),
        Err(_) => ((Vec::new(), 0), (Vec::new(), 0), true),
    };
    drop(job);
    let mut stderr = String::from_utf8_lossy(&stderr).into_owned();
    if cancelled {
        stderr.push_str("\n[lga] command cancelled; process tree terminated");
    }
    Ok(ShellResult {
        exit_code,
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr,
        timed_out,
        cancelled,
        truncated: out_dropped > 0 || err_dropped > 0 || undrained,
        duration_ms: started.elapsed().as_millis() as u64,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shell() -> PathBuf {
        resolve_powershell("pwsh").expect("PowerShell on PATH")
    }

    #[tokio::test]
    async fn captures_output_exit_code_and_unicode() {
        let r = run_powershell("Write-Output 'xin chào'; [Console]::Error.WriteLine('oops'); exit 3", &shell(), None, Duration::from_secs(60), 1 << 20, Arc::new(Notify::new()))
            .await
            .unwrap();
        assert_eq!(r.exit_code, 3);
        assert_eq!(r.stdout.trim(), "xin chào");
        assert!(r.stderr.contains("oops"));
        assert!(!r.timed_out && !r.truncated);
    }

    #[tokio::test]
    async fn timeout_kills_the_process_tree() {
        let started = Instant::now();
        let r = run_powershell("Start-Process pwsh -ArgumentList '-NoProfile','-Command','Start-Sleep 60' -NoNewWindow; Start-Sleep 60", &shell(), None, Duration::from_secs(3), 1 << 20, Arc::new(Notify::new()))
            .await
            .unwrap();
        assert!(r.timed_out);
        assert!(started.elapsed() < Duration::from_secs(20));
    }

    #[tokio::test]
    async fn output_is_capped() {
        let r = run_powershell("'x' * 5000", &shell(), None, Duration::from_secs(60), 100, Arc::new(Notify::new())).await.unwrap();
        assert!(r.truncated);
        assert_eq!(r.stdout.len(), 100);
    }

    #[tokio::test]
    async fn exit_codes_follow_prompt_semantics() {
        let run = |s: &'static str| async move {
            run_powershell(s, &shell(), None, Duration::from_secs(60), 1 << 20, Arc::new(Notify::new())).await.unwrap()
        };
        let r = run("Get-Item C:/definitely-missing -ErrorAction SilentlyContinue; Write-Output found-nothing").await;
        assert_eq!(r.exit_code, 0, "{r:?}");
        let r = run("Get-Item C:/definitely-missing; Write-Output after").await;
        assert_eq!(r.exit_code, 0, "non-terminating error is reported, not fatal: {r:?}");
        assert!(r.stderr.contains("definitely-missing"));
        assert_eq!(run("throw 'boom'").await.exit_code, 1);
        assert_eq!(run("Get-Item C:/definitely-missing -ErrorAction Stop").await.exit_code, 1);
        assert_eq!(run("cmd /c exit 5").await.exit_code, 5);
        assert_eq!(run("Write-Output x; exit 7").await.exit_code, 7);
    }

    #[test]
    fn elevation_failures_are_recognised() {
        let r = ShellResult { exit_code: 1, stdout: String::new(), stderr: "Access is denied.".into(), timed_out: false, cancelled: false, truncated: false, duration_ms: 0 };
        assert!(r.looks_like_elevation_failure());
    }
}
