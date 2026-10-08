"""Our own Chrome process: launch with a dedicated automation profile, find its CDP port, kill it.

Why we launch Chrome ourselves instead of letting browser-use do it: when browser-use launches
Chrome with a `user_data_dir` it COPIES that profile (cookies included) into %TEMP% and never deletes
the copy. We therefore start `chrome.exe --user-data-dir=<profile_dir> --remote-debugging-port=0`
ourselves, read the port Chrome picked from `<profile_dir>/DevToolsActivePort` and let browser-use
only *attach* over CDP.

The profile directory is dedicated to the agent (default `runtime/chrome-profile`, git-ignored).
Nothing here reads the user's real Chrome profile. Chrome's stdout/stderr go to a log file under
`logs/browser/`, never to an unread pipe (and never to the worker's protocol stdout).
"""

from __future__ import annotations

from typing import Any

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import psutil

from lga_worker.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

DEVTOOLS_ACTIVE_PORT_FILE = "DevToolsActivePort"
MIN_TCP_PORT = 1
MAX_TCP_PORT = 65535
LOOPBACK_HOST = "127.0.0.1"
PORT_POLL_INTERVAL_S = 0.1
# A single HTTP probe of /json/version; the overall wait is bounded by the launch timeout.
ENDPOINT_PROBE_TIMEOUT_S = 2.0
KILL_WAIT_S = 5.0

BASE_ARGS = (
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
)
HEADLESS_ARG = "--headless=new"
INITIAL_URL = "about:blank"


def parse_devtools_active_port(text: str) -> tuple[int, str]:
    """Parse Chrome's DevToolsActivePort file: line 1 = port, line 2 = browser websocket path."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("DevToolsActivePort is empty")
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise ValueError(f"DevToolsActivePort first line is not a port: {lines[0]!r}") from exc
    if not MIN_TCP_PORT <= port <= MAX_TCP_PORT:
        raise ValueError(f"DevToolsActivePort port out of range: {port}")
    ws_path = lines[1] if len(lines) > 1 else ""
    return port, ws_path


def read_devtools_active_port(profile_dir: Path) -> tuple[int, str] | None:
    """The (port, ws_path) Chrome wrote into the profile, or None while the file is absent/incomplete."""
    path = profile_dir / DEVTOOLS_ACTIVE_PORT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError:
        return None  # Chrome is still writing it
    try:
        return parse_devtools_active_port(text)
    except ValueError:
        return None  # partially written; the caller polls again


def http_cdp_url(port: int) -> str:
    return f"http://{LOOPBACK_HOST}:{port}"


def probe_endpoint(cdp_url: str, timeout_s: float = ENDPOINT_PROBE_TIMEOUT_S) -> dict | None:
    """GET <cdp_url>/json/version without any proxy. Returns the JSON or None when nothing answers."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(cdp_url.rstrip("/") + "/json/version", timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _is_browser_process_of(proc: psutil.Process, profile_marker: str) -> bool:
    cmdline = proc.cmdline()
    joined = " ".join(cmdline).lower()
    return profile_marker in joined and not any(arg.startswith("--type=") for arg in cmdline)


def processes_using_profile(profile_dir: Path, browser_only: bool = False) -> list[psutil.Process]:
    """Chrome processes started with `--user-data-dir=<profile_dir>` (only ours: the profile is dedicated)."""
    marker = f"--user-data-dir={profile_dir}".lower()
    found: list[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() != "chrome.exe":
            continue
        try:
            cmdline = " ".join(proc.cmdline()).lower()
            if marker not in cmdline:
                continue
            if browser_only and not _is_browser_process_of(proc, marker):
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # exited meanwhile, or not ours (other users' processes)
        found.append(proc)
    return found


def kill_process_tree(proc: psutil.Process, wait_s: float = KILL_WAIT_S) -> None:
    try:
        children = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for victim in [*children, proc]:
        try:
            victim.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs([proc, *children], timeout=wait_s)


@dataclass
class ChromeProcess:
    """A Chrome browser process we own (launched by us, or a leftover of ours on the same profile)."""

    pid: int
    port: int
    profile_dir: Path
    launched_at: float
    log_handle: IO[bytes] | None = field(default=None, repr=False)

    @property
    def cdp_url(self) -> str:
        return http_cdp_url(self.port)

    def is_alive(self) -> bool:
        try:
            proc = psutil.Process(self.pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def kill(self) -> None:
        """Kill the browser process, its children and any stray process still holding our profile."""
        try:
            kill_process_tree(psutil.Process(self.pid))
        except psutil.NoSuchProcess:
            logger.debug("chrome pid %s already gone", self.pid)
        for stray in processes_using_profile(self.profile_dir):
            kill_process_tree(stray)
        self.close_log()

    def close_log(self) -> None:
        if self.log_handle is not None and not self.log_handle.closed:
            self.log_handle.close()
        self.log_handle = None


def find_running(profile_dir: Path) -> ChromeProcess | None:
    """A Chrome of ours that is still running on `profile_dir` and answering CDP (e.g. after a worker crash)."""
    active = read_devtools_active_port(profile_dir)
    if active is None:
        return None
    browsers = processes_using_profile(profile_dir, browser_only=True)
    if not browsers:
        return None
    if probe_endpoint(http_cdp_url(active[0])) is None:
        return None
    return ChromeProcess(pid=browsers[0].pid, port=active[0], profile_dir=profile_dir, launched_at=time.time())


def build_command(executable: Path, profile_dir: Path, headless: bool) -> list[str]:
    command = [str(executable), f"--user-data-dir={profile_dir}", *BASE_ARGS]
    if headless:
        command.append(HEADLESS_ARG)
    command.append(INITIAL_URL)
    return command


# CreateProcess fails with ERROR_ACCESS_DENIED when an enclosing job forbids CREATE_BREAKAWAY_FROM_JOB.
_ERROR_ACCESS_DENIED = 5


def _spawn(command: list[str], log_handle: Any) -> subprocess.Popen[bytes]:
    """Start Chrome outside the worker's kill-on-close job when allowed, so it can outlive the agent."""
    def spawn(extra: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                                close_fds=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | extra)

    try:
        return spawn(subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
            raise
        logger.warning("job does not allow breakaway; Chrome will close when the agent exits")
        return spawn(0)


def launch(executable: Path, profile_dir: Path, headless: bool, timeout_s: float, log_dir: Path) -> ChromeProcess:
    """Start Chrome on the dedicated profile and wait until its CDP endpoint answers.

    Blocking (runs in a worker thread). Raises TIMEOUT / BACKEND_UNAVAILABLE; never leaves a half-started
    Chrome behind on failure.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    stale = profile_dir / DEVTOOLS_ACTIVE_PORT_FILE
    stale.unlink(missing_ok=True)  # an old port from a crashed run must not be mistaken for the new one
    for leftover in processes_using_profile(profile_dir):
        logger.warning("killing leftover chrome pid %s on the automation profile", leftover.pid)
        kill_process_tree(leftover)
    log_path = log_dir / f"chrome-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle = log_path.open("ab")
    command = build_command(executable, profile_dir, headless)
    logger.info("launching chrome: %s", " ".join(command))
    try:
        popen = _spawn(command, log_handle)
    except OSError as exc:
        log_handle.close()
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"could not start Chrome {executable}: {exc}") from exc
    chrome = ChromeProcess(pid=popen.pid, port=0, profile_dir=profile_dir, launched_at=time.time(),
                           log_handle=log_handle)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            exit_code = popen.poll()
            if exit_code is not None:
                raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                                f"Chrome exited immediately with code {exit_code} (see {log_path})")
            active = read_devtools_active_port(profile_dir)
            if active is not None and probe_endpoint(http_cdp_url(active[0])) is not None:
                chrome.port = active[0]
                logger.info("chrome pid %s listening on CDP port %s", chrome.pid, chrome.port)
                return chrome
            if time.monotonic() >= deadline:
                raise ToolError(ErrorCode.TIMEOUT,
                                f"Chrome did not open its DevTools port within {timeout_s:.0f}s (see {log_path})")
            time.sleep(PORT_POLL_INTERVAL_S)
    except BaseException:
        chrome.kill()
        raise
