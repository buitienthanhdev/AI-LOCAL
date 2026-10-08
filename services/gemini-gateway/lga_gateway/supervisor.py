"""Supervision of the backend processes the gateway manages.

Each managed backend runs as a child process inside a Job Object with KILL_ON_JOB_CLOSE, so the
backends die with the gateway even if the gateway is terminated abruptly. Child output is redacted
line by line before it is written to logs/gateway/<backend>.log. Restarts are rate limited.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import win32api
import win32con
import win32job

from lga_common.redact import Redactor

logger = logging.getLogger(__name__)

# At most this many restarts within the window; afterwards the backend stays down until the window passes.
MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_S = 600.0
STOP_GRACE_S = 5.0


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    log_path: Path | None = None


class ManagedProcess:
    def __init__(self, spec: ProcessSpec, job: int, redactor: Redactor) -> None:
        self.spec = spec
        self._job = job
        self._redactor = redactor
        self._process: subprocess.Popen[bytes] | None = None
        self._restarts: deque[float] = deque()
        self._lock = threading.Lock()
        self.last_exit_code: int | None = None
        self.start_count = 0

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def is_running(self) -> bool:
        if self._process is None:
            return False
        code = self._process.poll()
        if code is not None:
            self.last_exit_code = code
            return False
        return True

    def can_restart(self) -> bool:
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > RESTART_WINDOW_S:
            self._restarts.popleft()
        return len(self._restarts) < MAX_RESTARTS_PER_WINDOW

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            if self.start_count > 0:
                if not self.can_restart():
                    raise RuntimeError(f"{self.spec.name}: restart limit reached "
                                       f"({MAX_RESTARTS_PER_WINDOW} in {RESTART_WINDOW_S:.0f}s)")
                self._restarts.append(time.monotonic())
            executable = Path(self.spec.argv[0])
            if not executable.is_file():
                raise FileNotFoundError(f"{self.spec.name}: {executable} does not exist; run scripts/setup-gateway.ps1")
            env = {**os.environ, **self.spec.env}
            self._process = subprocess.Popen(
                self.spec.argv, cwd=str(self.spec.cwd), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False,
                                          self._process.pid)
            try:
                win32job.AssignProcessToJobObject(self._job, handle)
            finally:
                win32api.CloseHandle(handle)
            self.start_count += 1
            threading.Thread(target=self._pump_output, args=(self._process,), daemon=True,
                             name=f"log-{self.spec.name}").start()
            logger.info("started %s (pid %s)", self.spec.name, self._process.pid)

    def _pump_output(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        log_file = None
        if self.spec.log_path is not None:
            self.spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = self.spec.log_path.open("a", encoding="utf-8")
        try:
            for raw in process.stdout:
                line = self._redactor(raw.decode("utf-8", errors="replace").rstrip())
                if log_file is not None:
                    log_file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
                    log_file.flush()
        finally:
            if log_file is not None:
                log_file.close()

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()  # gemini-web2api-go has no graceful shutdown handler; terminate is the stop path
        try:
            process.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_GRACE_S)
        self.last_exit_code = process.returncode
        logger.info("stopped %s", self.spec.name)

    def status(self) -> dict[str, object]:
        return {"managed": True, "running": self.is_running(), "pid": self.pid, "starts": self.start_count,
                "last_exit_code": self.last_exit_code}


def create_kill_on_close_job() -> int:
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job
