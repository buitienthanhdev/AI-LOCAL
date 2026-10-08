//! Windows Job Object with KILL_ON_JOB_CLOSE: child processes die with the owning process.
//!
//! BREAKAWAY_OK is set as well: applications the agent starts for the user (desktop.launch_app,
//! AutoCAD, Chrome) are created with CREATE_BREAKAWAY_FROM_JOB and must outlive the agent. Helper
//! processes started without that flag (PowerShell commands, the worker itself) still die with it.

#[cfg(windows)]
mod imp {
    use std::io;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
        TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE};

    pub struct JobObject {
        handle: HANDLE,
    }

    // SAFETY: a job object handle is a kernel handle usable from any thread.
    unsafe impl Send for JobObject {}
    unsafe impl Sync for JobObject {}

    impl JobObject {
        /// Kill-on-close job whose members may start processes outside it (see the module docs).
        pub fn kill_on_close() -> io::Result<Self> {
            Self::create(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        }

        /// Kill-on-close job without breakaway: the whole process tree dies with it (shell commands).
        pub fn kill_on_close_strict() -> io::Result<Self> {
            Self::create(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        }

        fn create(limit_flags: u32) -> io::Result<Self> {
            // SAFETY: plain Win32 calls with valid arguments; the handle is owned by the returned value.
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = limit_flags;
                let ok = SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    let err = io::Error::last_os_error();
                    CloseHandle(handle);
                    return Err(err);
                }
                Ok(Self { handle })
            }
        }

        pub fn assign(&self, pid: u32) -> io::Result<()> {
            // SAFETY: the process handle is closed before returning.
            unsafe {
                let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
                if process.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let ok = AssignProcessToJobObject(self.handle, process);
                let err = io::Error::last_os_error();
                CloseHandle(process);
                if ok == 0 {
                    return Err(err);
                }
                Ok(())
            }
        }
    }

    impl JobObject {
        /// Kill every process in the job now.
        pub fn terminate(&self, exit_code: u32) {
            // SAFETY: the handle is valid for the lifetime of self.
            unsafe {
                TerminateJobObject(self.handle, exit_code);
            }
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) {
            // SAFETY: the handle was created by CreateJobObjectW and is closed exactly once.
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }
}

#[cfg(windows)]
pub use imp::JobObject;
