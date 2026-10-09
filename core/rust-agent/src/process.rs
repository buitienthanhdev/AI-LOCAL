//! Starting long-lived background processes (the Gemini gateway) fully detached from the caller.
//!
//! `std::process::Command` always passes `bInheritHandles = TRUE`, so a detached child would inherit
//! every inheritable handle of the CLI, including the pipe a shell attached to its stdout, and keep
//! that pipe open long after the CLI exited. Here `CreateProcessW` is called with handle inheritance
//! disabled and an explicit environment block.

use std::ffi::OsStr;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;

use windows_sys::Win32::Foundation::CloseHandle;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, CREATE_BREAKAWAY_FROM_JOB, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_UNICODE_ENVIRONMENT,
    DETACHED_PROCESS,
    PROCESS_INFORMATION, STARTUPINFOW,
};

/// Quote one argument following the MSVCRT/CommandLineToArgvW rules.
fn quote_arg(arg: &str) -> String {
    if !arg.is_empty() && !arg.contains([' ', '\t', '\n', '\x0b', '"']) {
        return arg.to_string();
    }
    let mut out = String::from("\"");
    let mut backslashes = 0;
    for c in arg.chars() {
        match c {
            '\\' => backslashes += 1,
            '"' => {
                out.push_str(&"\\".repeat(backslashes * 2 + 1));
                out.push('"');
                backslashes = 0;
            }
            _ => {
                out.push_str(&"\\".repeat(backslashes));
                out.push(c);
                backslashes = 0;
            }
        }
    }
    out.push_str(&"\\".repeat(backslashes * 2));
    out.push('"');
    out
}

fn wide(s: &OsStr) -> Vec<u16> {
    s.encode_wide().chain(std::iter::once(0)).collect()
}

/// Spawn `program args` detached (no console, own process group, no inherited handles).
/// Returns the new process id.
pub fn spawn_detached(program: &Path, args: &[&str], cwd: &Path, extra_env: &[(&str, &str)]) -> io::Result<u32> {
    spawn_with(program, args, cwd, extra_env, DETACHED_PROCESS)
}

/// Like `spawn_detached`, but console programs (PowerShell, Python) get a hidden console of their own: with
/// no console at all some of them exit at once.
pub fn spawn_hidden_console(program: &Path, args: &[&str], cwd: &Path, extra_env: &[(&str, &str)]) -> io::Result<u32> {
    spawn_with(program, args, cwd, extra_env, CREATE_NO_WINDOW)
}

fn spawn_with(program: &Path, args: &[&str], cwd: &Path, extra_env: &[(&str, &str)], console: u32) -> io::Result<u32> {
    let mut command_line = quote_arg(&program.to_string_lossy());
    for arg in args {
        command_line.push(' ');
        command_line.push_str(&quote_arg(arg));
    }
    let mut command_line: Vec<u16> = OsStr::new(&command_line).encode_wide().chain(std::iter::once(0)).collect();

    let mut vars: Vec<(String, String)> = std::env::vars()
        .filter(|(k, _)| !extra_env.iter().any(|(e, _)| e.eq_ignore_ascii_case(k)))
        .collect();
    vars.extend(extra_env.iter().map(|(k, v)| (k.to_string(), v.to_string())));
    vars.sort_by_key(|(k, _)| k.to_uppercase());
    let mut env_block: Vec<u16> = Vec::new();
    for (k, v) in vars {
        env_block.extend(OsStr::new(&format!("{k}={v}")).encode_wide());
        env_block.push(0);
    }
    env_block.push(0);

    let application = wide(program.as_os_str());
    let directory = wide(cwd.as_os_str());
    let base_flags = console | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT;
    // Leave the caller's job (so the gateway outlives the CLI) when the job permits it.
    for flags in [base_flags | CREATE_BREAKAWAY_FROM_JOB, base_flags] {
        // SAFETY: all pointers reference live, NUL-terminated buffers owned by this function; the
        // returned process and thread handles are closed immediately.
        unsafe {
            let mut startup: STARTUPINFOW = std::mem::zeroed();
            startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
            let mut info: PROCESS_INFORMATION = std::mem::zeroed();
            let ok = CreateProcessW(
                application.as_ptr(),
                command_line.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                0, // bInheritHandles = FALSE
                flags,
                env_block.as_ptr() as *const core::ffi::c_void,
                directory.as_ptr(),
                &startup,
                &mut info,
            );
            if ok != 0 {
                CloseHandle(info.hThread);
                CloseHandle(info.hProcess);
                return Ok(info.dwProcessId);
            }
        }
    }
    Err(io::Error::last_os_error())
}

#[cfg(test)]
mod tests {
    use super::quote_arg;

    #[test]
    fn quoting_follows_argv_rules() {
        assert_eq!(quote_arg("plain"), "plain");
        assert_eq!(quote_arg("with space"), "\"with space\"");
        assert_eq!(quote_arg(r#"a"b"#), r#""a\"b""#);
        assert_eq!(quote_arg(r"C:\dir with space\"), r#""C:\dir with space\\""#);
        assert_eq!(quote_arg(""), "\"\"");
    }
}
