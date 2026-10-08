"""PowerShell execution with a hard timeout that also kills grandchildren.

Windows-Use's `Desktop.execute_command` returns stdout *or* stderr, uses a fixed 10 s timeout and leaves
child processes running after a timeout, so this worker uses its own runner:

* PowerShell 7 (`pwsh`) when available, else Windows PowerShell 5.1.
* `-NoProfile -NonInteractive`, stdin closed: a script that prompts fails instead of hanging.
* The process is placed in a Job Object with KILL_ON_JOB_CLOSE; on timeout or cancellation the whole
  job is terminated. Because the worker itself may run inside a job that permits silent breakaway
  (some IDE/terminal hosts do), descendants are also enumerated and killed with psutil.
* stdout and stderr are captured separately, decoded as UTF-8, and capped at `max_output_bytes` each.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import win32api
import win32con
import win32job

from lga_worker.windows.provider import ShellResult

ERROR_ELEVATION_REQUIRED = 740
_ELEVATION_MARKERS = (
    "requires elevation",
    "run as administrator",
    "administrator privilege",
    "access is denied",
)
_READ_CHUNK = 65536
_POLL_INTERVAL_S = 0.05
# After the process tree is killed, wait at most this long for the output pipes to drain. A process
# that escaped the job (possible when the parent job allows breakaway) could otherwise hold them open.
_DRAIN_TIMEOUT_S = 5.0
_UTF8_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$ProgressPreference = 'SilentlyContinue'; "
)


def resolve_powershell(preferred: str) -> str:
    """Return the full path of the PowerShell executable to use."""
    for candidate in (preferred, "pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    raise FileNotFoundError("neither pwsh nor powershell.exe is on PATH")


@dataclass
class _Capture:
    data: bytearray
    dropped: int = 0


def _pump(stream, capture: _Capture, limit: int) -> None:
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            return
        room = limit - len(capture.data)
        if room > 0:
            capture.data.extend(chunk[:room])
        capture.dropped += max(0, len(chunk) - max(room, 0))


def _create_kill_on_close_job() -> int:
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def _kill_tree(pid: int, job: int) -> None:
    try:
        root = psutil.Process(pid)
        descendants = root.children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    win32job.TerminateJobObject(job, 1)
    for proc in descendants:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue  # already exited, which is the goal


def looks_like_elevation_failure(result: ShellResult) -> bool:
    if result.exit_code == ERROR_ELEVATION_REQUIRED:
        return True
    lowered = result.stderr.lower()
    return result.exit_code != 0 and any(marker in lowered for marker in _ELEVATION_MARKERS)


def run_powershell(
    script: str,
    *,
    executable: str,
    cwd: Path | None,
    timeout_s: float,
    max_output_bytes: int,
    cancel_event: threading.Event | None = None,
) -> ShellResult:
    encoded = base64.b64encode((_UTF8_PREAMBLE + script).encode("utf-16-le")).decode("ascii")
    args = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    started = time.monotonic()
    job = _create_kill_on_close_job()
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)

        out, err = _Capture(bytearray()), _Capture(bytearray())
        readers = [
            threading.Thread(target=_pump, args=(process.stdout, out, max_output_bytes), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, err, max_output_bytes), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = started + timeout_s
        timed_out = False
        cancelled = False
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(_POLL_INTERVAL_S)
        if timed_out or cancelled:
            _kill_tree(process.pid, job)
            process.wait()
        drain_deadline = time.monotonic() + _DRAIN_TIMEOUT_S
        undrained = False
        for reader in readers:
            reader.join(timeout=max(0.0, drain_deadline - time.monotonic()))
            undrained = undrained or reader.is_alive()
        exit_code = process.returncode
    finally:
        win32api.CloseHandle(job)

    duration_ms = int((time.monotonic() - started) * 1000)
    stderr_text = err.data.decode("utf-8", errors="replace")
    if cancelled:
        stderr_text += "\n[lga] command cancelled; process tree terminated"
    return ShellResult(
        exit_code=exit_code,
        stdout=out.data.decode("utf-8", errors="replace"),
        stderr=stderr_text,
        timed_out=timed_out,
        truncated=bool(out.dropped or err.dropped or undrained),
        duration_ms=duration_ms,
    )
