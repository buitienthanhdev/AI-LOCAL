"""Phase H1 recovery probe: adapter crash under a real Gateway, and CDP disconnect / Chrome exit.

ISOLATED: profile runtime/recovery-test-profile, free loopback ports, no prompt can reach a page (the test
selector table names an input that cannot exist). Never touches runtime/gemini-profile or the production
gateway.

    cargo build -p lga-gateway --example gateway_host
    services/python-agent/.venv/Scripts/python.exe services/chrome-gemini/tests/recovery_probe.py [B C]
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "python-agent"))
sys.path.insert(0, str(ROOT / "services" / "chrome-gemini"))
from lga_worker.browser import chrome as chrome_mod  # noqa: E402

HOST = ROOT / "target" / "debug" / "examples" / "gateway_host.exe"
PYTHON = ROOT / "services" / "python-agent" / ".venv" / "Scripts" / "python.exe"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROFILE = ROOT / "runtime" / "recovery-test-profile"
WORK = ROOT / "logs" / "dev" / "recovery"
PRODUCTION_PROFILE = ROOT / "runtime" / "gemini-profile"
SELECTORS = WORK / "selectors.test.json"


def write_selectors() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    SELECTORS.write_text(json.dumps({
        "verified": True, "input": "#lga-test-nonexistent-input", "send": "#lga-test-nonexistent-send",
        "answer": "model-response message-content", "generating": "input-area-v2 .send-button.stop",
        "login_marker": "a", "model_label": "bard-mode-switcher button"}), encoding="utf-8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def browser_pids() -> list[int]:
    return sorted(p.pid for p in chrome_mod.processes_using_profile(PROFILE, browser_only=True))


def all_chrome() -> list[int]:
    return sorted(p.pid for p in chrome_mod.processes_using_profile(PROFILE))


def adapters_on_profile() -> list[dict]:
    """python processes running `-m chrome_gemini` with our profile (the singleton question)."""
    found = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        cmd = p.info.get("cmdline") or []
        if p.info["name"] and p.info["name"].lower().startswith("python") and "chrome_gemini" in cmd and str(PROFILE) in cmd:
            found.append({"pid": p.info["pid"], "ppid": p.info["ppid"], "port": cmd[cmd.index("--port") + 1]})
    return found


def cleanup() -> None:
    for entry in adapters_on_profile():
        try:
            psutil.Process(entry["pid"]).kill()
        except psutil.NoSuchProcess:
            pass
    for p in chrome_mod.processes_using_profile(PROFILE):
        chrome_mod.kill_process_tree(p)
    (PROFILE / chrome_mod.DEVTOOLS_ACTIVE_PORT_FILE).unlink(missing_ok=True)


class GatewayHost:
    def __init__(self) -> None:
        write_selectors()
        self.port = free_port()
        self.proc = subprocess.Popen(
            [str(HOST), str(PYTHON), str(ROOT / "services" / "chrome-gemini"), str(ROOT / "services" / "python-agent"),
             str(self.port), str(PROFILE), str(WORK / "chrome-logs"), str(SELECTORS), str(CHROME)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        assert json.loads(self.proc.stdout.readline())["event"] == "ready"

    def cmd(self, name: str):
        self.proc.stdin.write(name + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def route(self) -> dict:
        return self.cmd("health")[0]

    def adapter_pid(self) -> int | None:
        return self.route()["process"].get("pid")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.cmd("quit")
                self.proc.wait(20)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def http_health(port: int, timeout: float = 40) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
        return json.loads(r.read())


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


def wait_for(predicate, seconds: float, step: float = 0.25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


# ---------------------------------------------------------------------------------------------- B
def scenario_B_adapter_crash() -> dict:
    cleanup()
    h = GatewayHost()
    out: dict = {}
    try:
        first = wait_for(lambda: h.adapter_pid(), 20)
        warm(h.port)  # a request makes the adapter open Chrome on the test profile (health only observes)
        chrome_before = browser_pids()
        out["before"] = {"adapter_pid": first, "chrome_browser_pids": chrome_before, "adapters": adapters_on_profile(),
                         "owner_file": (PROFILE / "chrome-gemini-owner.json").read_text(encoding="utf-8")}
        t0 = time.time()
        psutil.Process(first).kill()  # the adapter alone dies; the gateway host stays up
        out["killed_adapter_pid"] = first
        # What the gateway does by itself (health loop -> ensure_process), without any chat.
        second = wait_for(lambda: (lambda p: p if p and p != first and psutil.pid_exists(p) else None)(h.adapter_pid()), 30)
        out["restart"] = {"new_adapter_pid": second, "seconds": round(time.time() - t0, 1)}
        wait_for(lambda: _try(lambda: http_health(h.port, 40)), 40)
        health = _try(lambda: http_health(h.port, 40))
        out["after"] = {"health": {k: health.get(k) for k in ("state", "model_status", "error_class")} if health else None,
                        "chrome_browser_pids": browser_pids(), "same_chrome": browser_pids() == chrome_before,
                        "adapters": adapters_on_profile(), "chrome_process_count": len(all_chrome()),
                        "adapter_listening": listening(h.port)}
        # A chat right after a second kill exercises restart_if_dead (connect refused -> BackendDown).
        psutil.Process(second).kill()
        out["chat_after_second_kill"] = h.cmd("chat")
        third = wait_for(lambda: (lambda p: p if p and p not in (first, second) and psutil.pid_exists(p) else None)(h.adapter_pid()), 30)
        out["third_adapter_pid"] = third
        wait_for(lambda: _try(lambda: http_health(h.port, 40)), 40)
        out["after_second_crash"] = {"chrome_browser_pids": browser_pids(), "same_chrome": browser_pids() == chrome_before,
                                     "adapters": adapters_on_profile(), "chrome_process_count": len(all_chrome())}
    finally:
        h.close()
        time.sleep(1)
        out["after_gateway_quit"] = {"chrome": all_chrome(), "adapters": adapters_on_profile(), "adapter_listening": listening(h.port)}
        cleanup()
    return out


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def scenario_B2_two_adapters() -> dict:
    """The singleton question: a second adapter started by hand on the same profile."""
    cleanup()
    write_selectors()
    out: dict = {}
    procs = []
    try:
        ports = [free_port(), free_port()]
        for port in ports:
            procs.append(subprocess.Popen(
                [str(PYTHON), "-m", "chrome_gemini", "--port", str(port), "--profile-dir", str(PROFILE), "--log-dir",
                 str(WORK / "chrome-logs"), "--selectors", str(SELECTORS), "--chrome", str(CHROME)],
                cwd=ROOT / "services" / "chrome-gemini", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={**__import__("os").environ, "LGA_ADAPTER_CHAT_TOKEN": "host-chat-token",
                     "PYTHONPATH": f"{ROOT / 'services' / 'chrome-gemini'};{ROOT / 'services' / 'python-agent'}"}))
            wait_for(lambda p=port: listening(p) or procs[-1].poll() is not None, 20)
            time.sleep(0.5)
        out["second_exited"] = procs[1].poll()
        out["second_exit_stderr_tail"] = (procs[1].stderr.read().decode("utf-8", "replace")[-300:] if procs[1].poll() is not None else None)
        out["listening"] = {str(p): listening(p) for p in ports}
        out["adapters_on_profile"] = adapters_on_profile()
        warm([p for p in ports if listening(p)][0])
        health = [_try(lambda p=p: http_health(p, 40)) for p in ports if listening(p)]
        out["chrome_browser_pids"] = browser_pids()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        time.sleep(0.5)
        cleanup()
    return out


# ---------------------------------------------------------------------------------------------- C
def scenario_C_cdp() -> dict:
    from chrome_gemini.cdp_page import CdpGeminiPage
    from chrome_gemini.page import PageError

    cleanup()
    write_selectors()
    out: dict = {}

    async def go() -> None:
        page = CdpGeminiPage(CHROME, PROFILE, WORK / "chrome-logs", SELECTORS, launch_timeout_s=30)
        await page.ensure_session()  # launches the (owned) Chrome and attaches
        pid = browser_pids()
        port = chrome_mod.read_devtools_active_port(PROFILE)[0]
        out["attached"] = {"chrome_browser_pids": pid, "cdp_port": port}

        # C1: drop only our CDP client; Chrome keeps running.
        await page._socket.close()
        out["after_client_disconnect"] = {"chrome_alive": bool(browser_pids()), "cdp_port_serves": chrome_mod.probe_endpoint(f"http://127.0.0.1:{port}") is not None}
        t0 = time.time()
        state = await page.read_state()  # CONNECTION_LOST internally -> one bounded reconnect
        out["reconnect_read"] = {"seconds": round(time.time() - t0, 2), "turns": state.turns, "generating": state.generating,
                                 "same_chrome": browser_pids() == pid, "chrome_process_count_ok": True,
                                 "same_port": chrome_mod.read_devtools_active_port(PROFILE)[0] == port}

        # C1b: a server-side drop mid-session (socket closed under us): the write fails, the next read recovers.
        await page._socket.close()
        t0 = time.time()
        state2 = await page.read_state()
        out["second_drop"] = {"seconds": round(time.time() - t0, 2), "same_chrome": browser_pids() == pid}

        # C2: Chrome really exits (killed by PID, the owned browser process only).
        victim = psutil.Process(pid[0])
        victim.kill()
        victim.wait(10)
        wait_for(lambda: not all_chrome() or None, 8)
        t0 = time.time()
        try:
            await page.read_state()
            out["after_chrome_exit"] = {"raised": None}
        except PageError as exc:
            out["after_chrome_exit"] = {"raised": exc.error_class.value, "message": str(exc)[:120],
                                        "seconds": round(time.time() - t0, 2), "chrome_relaunched": bool(browser_pids())}
        # A second read does not loop or relaunch either.
        t0 = time.time()
        try:
            await page.read_state()
        except PageError as exc:
            out["after_chrome_exit"]["second_read"] = {"raised": exc.error_class.value, "seconds": round(time.time() - t0, 2),
                                                       "chrome_relaunched": bool(browser_pids())}

    try:
        asyncio.run(go())
    finally:
        out["cleanup_before"] = all_chrome()
        cleanup()
        out["cleanup_after"] = all_chrome()
    return out


def main() -> None:
    assert HOST.is_file(), "cargo build -p lga-gateway --example gateway_host"
    prod_before = sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))
    chosen = sys.argv[1:] or ["B", "B2", "C"]
    table = {"B": scenario_B_adapter_crash, "B2": scenario_B2_two_adapters, "C": scenario_C_cdp}
    report = {name: table[name]() for name in chosen}
    report["production_profile_chrome_pids"] = {"before": prod_before,
                                                 "after": sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))}
    report["leftovers"] = {"chrome": all_chrome(), "adapters": adapters_on_profile()}
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
