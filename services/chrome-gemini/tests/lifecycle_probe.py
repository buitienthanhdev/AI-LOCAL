"""Phase G lifecycle probe: who owns the Chrome of the Chrome Gemini adapter, and who dies when.

Runs scenarios A-F against an ISOLATED test profile (runtime/lifecycle-test-profile) with the real
ManagedProcess/KillOnCloseJob through the `lifecycle_host` example. It never touches runtime/gemini-profile,
the production gateway or any prompt. Records PIDs/PPIDs and liveness before/after; prints one JSON report.

    cargo build -p lga-gateway --example lifecycle_host
    services/python-agent/.venv/Scripts/python.exe services/chrome-gemini/tests/lifecycle_probe.py [A B C D E]
"""

from __future__ import annotations

import ctypes
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "python-agent"))
from lga_worker.browser import chrome as chrome_mod  # noqa: E402

HOST = ROOT / "target" / "debug" / "examples" / "lifecycle_host.exe"
PYTHON = ROOT / "services" / "python-agent" / ".venv" / "Scripts" / "python.exe"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROFILE = ROOT / "runtime" / "lifecycle-test-profile"
WORK = ROOT / "logs" / "dev" / "lifecycle"
PRODUCTION_PROFILE = ROOT / "runtime" / "gemini-profile"

PROCESS_QUERY_LIMITED = 0x1000
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]


def in_any_job(pid: int) -> bool | None:
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED, 0, pid)
    if not handle:
        return None
    try:
        flag = ctypes.c_int(0)
        ok = _k32.IsProcessInJob(handle, None, ctypes.byref(flag))
        return bool(flag.value) if ok else None
    finally:
        _k32.CloseHandle(handle)


def describe(pid: int | None) -> dict:
    if pid is None:
        return {"pid": None}
    try:
        p = psutil.Process(pid)
        return {"pid": pid, "alive": p.is_running() and p.status() != psutil.STATUS_ZOMBIE, "ppid": p.ppid(),
                "name": p.name(), "in_a_job": in_any_job(pid)}
    except psutil.NoSuchProcess:
        return {"pid": pid, "alive": False}


def test_chrome_processes() -> list[psutil.Process]:
    return chrome_mod.processes_using_profile(PROFILE)


def test_chrome_browser() -> psutil.Process | None:
    found = chrome_mod.processes_using_profile(PROFILE, browser_only=True)
    return found[0] if found else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def health(port: int, timeout: float = 45) -> dict:
    deadline = time.time() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=40) as r:
                return json.loads(r.read())
        except OSError:
            time.sleep(0.4)
    raise TimeoutError("adapter /health did not answer")


def warm(port: int, token: str = "host-chat-token") -> None:
    """Make the adapter start its Chrome. Health no longer does (it only observes), so a request does it:
    the test selectors name an input that cannot exist, so this request fails before anything is typed."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat", method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        data=json.dumps({"prompt": "warm-up: never submitted", "transaction_id": f"warm-{time.time_ns()}", "timeout_s": 20}).encode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with opener.open(request, timeout=60):
                return
        except urllib.request.HTTPError as exc:  # answered but refused: retrying will not help
            raise RuntimeError(f"warm-up request refused with HTTP {exc.code}") from exc
        except OSError:
            time.sleep(0.4)
    raise TimeoutError("adapter did not answer the warm-up request")


def wait_gone(pids: list[int], seconds: float = 8) -> list[int]:
    deadline = time.time() + seconds
    alive = list(pids)
    while alive and time.time() < deadline:
        alive = [p for p in alive if psutil.pid_exists(p)]
        time.sleep(0.2)
    return alive


def cleanup() -> None:
    for p in test_chrome_processes():
        chrome_mod.kill_process_tree(p)
    (PROFILE / chrome_mod.DEVTOOLS_ACTIVE_PORT_FILE).unlink(missing_ok=True)


class Host:
    def __init__(self) -> None:
        self.port = free_port()
        WORK.mkdir(parents=True, exist_ok=True)
        selectors = WORK / "selectors.test.json"
        selectors.write_text(json.dumps({"verified": True, "input": "#lga-test-nonexistent-input", "send": "#lga-test-nonexistent-send",
                                         "answer": "model-response", "generating": "stop", "login_marker": "a"}), encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(HOST), str(PYTHON), str(ROOT / "services" / "chrome-gemini"), str(ROOT / "services" / "python-agent"),
             str(self.port), str(PROFILE), str(WORK / "chrome-logs"), str(selectors), str(CHROME)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        ready = json.loads(self.proc.stdout.readline())
        assert ready["event"] == "ready"

    def cmd(self, name: str) -> dict:
        self.proc.stdin.write(name + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def adapter_pid(self) -> int | None:
        return self.cmd("status").get("pid")

    def terminate(self) -> None:
        psutil.Process(self.proc.pid).kill()  # TerminateProcess on the host: no destructors, no stop()
        self.proc.wait(10)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.cmd("quit")
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def snapshot(label: str, adapter: int | None, host: int | None = None) -> dict:
    browser = test_chrome_browser()
    return {"label": label, "host": describe(host), "adapter": describe(adapter),
            "chrome_browser": describe(browser.pid if browser else None),
            "chrome_processes": len(test_chrome_processes()),
            "devtools_port_file": chrome_mod.read_devtools_active_port(PROFILE)}


def scenario_A() -> list[dict]:
    cleanup()
    h, out = Host(), []
    try:
        a = h.adapter_pid()
        out.append(snapshot("started", a, h.proc.pid))
        warm(h.port)  # a request starts Chrome; health only observes
        out.append({"health": {k: v for k, v in health(h.port).items() if k in ("state", "error_class")}})
        out.append(snapshot("after health (adapter opened Chrome)", a, h.proc.pid))
        chrome_pids = [p.pid for p in test_chrome_processes()]
        h.cmd("quit")
        h.proc.wait(10)
        out.append({"after_normal_shutdown": snapshot("host quit", a, h.proc.pid),
                    "chrome_pids_still_alive": wait_gone(chrome_pids), "adapter_port_listening": listening(h.port)})
    finally:
        h.close()
        out.append({"cleanup_leftovers": [p.pid for p in test_chrome_processes()]})
        cleanup()
    return out


def scenario_B() -> list[dict]:
    cleanup()
    h, out = Host(), []
    try:
        a = h.adapter_pid()
        warm(h.port)
        before = snapshot("health done", a, h.proc.pid)
        out.append(before)
        stop = h.cmd("stop")
        time.sleep(1)
        out.append({"stop_reply": stop, "after_adapter_stop": snapshot("adapter stopped", a, h.proc.pid),
                    "adapter_port_listening": listening(h.port)})
        out.append({"then_host_quit": None})
        chrome_pids = [p.pid for p in test_chrome_processes()]
        h.cmd("quit")
        h.proc.wait(10)
        out[-1] = {"then_host_quit": {"chrome_pids_still_alive": wait_gone(chrome_pids)}}
    finally:
        h.close()
        out.append({"cleanup_leftovers": [p.pid for p in test_chrome_processes()]})
        cleanup()
    return out


def scenario_C() -> list[dict]:
    cleanup()
    h, out = Host(), []
    try:
        a = h.adapter_pid()
        warm(h.port)
        out.append(snapshot("before crash", a, h.proc.pid))
        chrome_pids = [p.pid for p in test_chrome_processes()]
        h.terminate()
        time.sleep(1.5)
        out.append({"host_terminated": True, "adapter": describe(a),
                    "chrome_pids_still_alive": wait_gone(chrome_pids), "adapter_port_listening": listening(h.port)})
    finally:
        h.close()
        out.append({"cleanup_leftovers": [p.pid for p in test_chrome_processes()]})
        cleanup()
    return out


def scenario_D_E() -> list[dict]:
    """D: an external Chrome is reused and survives the adapter. E: a restarted adapter finds it again."""
    cleanup()
    out = []
    external = chrome_mod.launch(CHROME, PROFILE, True, 30, WORK / "chrome-logs")  # launched by this script, not by an adapter
    out.append({"external_chrome": describe(external.pid), "cdp_port": external.port})
    h = Host()
    try:
        a1 = h.adapter_pid()
        out.append({"health_1": {k: v for k, v in health(h.port).items() if k in ("state", "error_class")}})
        after_first = snapshot("adapter reused external chrome", a1, h.proc.pid)
        out.append({"same_browser_pid": after_first["chrome_browser"]["pid"] == external.pid,
                    "same_port": after_first["devtools_port_file"] and after_first["devtools_port_file"][0] == external.port,
                    "chrome_processes": after_first["chrome_processes"]})
        out.append({"stop": h.cmd("stop")})
        time.sleep(1)
        out.append({"D_after_adapter_stop": {"external_alive": describe(external.pid)["alive"], "adapter": describe(a1)}})
        out.append({"start": h.cmd("start")})
        a2 = h.adapter_pid()
        out.append({"health_2": {k: v for k, v in health(h.port).items() if k in ("state", "error_class")}})
        again = snapshot("restarted adapter", a2, h.proc.pid)
        out.append({"E_restart": {"new_adapter_pid": a2, "old_adapter_pid": a1,
                                  "same_browser_pid": again["chrome_browser"]["pid"] == external.pid,
                                  "same_port": again["devtools_port_file"] and again["devtools_port_file"][0] == external.port,
                                  "chrome_processes": again["chrome_processes"]}})
        h.cmd("quit")
        h.proc.wait(10)
        time.sleep(1)
        out.append({"D_after_host_quit": {"external_alive": describe(external.pid)}})
    finally:
        h.close()
        out.append({"cleanup_leftovers_before_cleanup": [p.pid for p in test_chrome_processes()]})
        cleanup()
    return out


def main() -> None:
    assert HOST.is_file(), "build the host first: cargo build -p lga-gateway --example lifecycle_host"
    prod_before = sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))
    chosen = sys.argv[1:] or ["A", "B", "C", "DE"]
    table = {"A": scenario_A, "B": scenario_B, "C": scenario_C, "DE": scenario_D_E}
    report = {}
    for name in chosen:
        report[name] = table[name]()
    prod_after = sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))
    report["production_profile_chrome_pids"] = {"before": prod_before, "after": prod_after}
    report["test_profile_leftovers"] = [p.pid for p in test_chrome_processes()]
    listening_test = [c.laddr.port for c in psutil.net_connections("inet") if c.status == "LISTEN" and c.pid in
                      {p.pid for p in test_chrome_processes()}]
    report["test_profile_listeners"] = listening_test
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
