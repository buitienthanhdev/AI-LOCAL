"""Phase H3 hang probe: what happens at shutdown when the REAL Chrome stops answering CDP.

A Chrome browser process is suspended (NtSuspendProcess via psutil), which is what a hung or frozen browser looks
like to the adapter: sockets stay open, nothing answers. Isolated: profile runtime/hang-test-profile, a
Gateway test host with the real ManagedProcess/KillOnCloseJob, a selector table that cannot submit a prompt.
Chrome is resumed or killed by PID at the end; nothing is killed by name.

    cargo build -p lga-gateway --example gateway_host
    services/python-agent/.venv/Scripts/python.exe services/chrome-gemini/tests/hang_probe.py [A B C]
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).parent))
import recovery_probe as rp  # noqa: E402  (same isolation helpers; its PROFILE is replaced below)
from lga_worker.browser import chrome as chrome_mod  # noqa: E402

rp.PROFILE = ROOT / "runtime" / "hang-test-profile"
rp.WORK = ROOT / "logs" / "dev" / "hang"
rp.SELECTORS = rp.WORK / "selectors.test.json"
PRODUCTION_PROFILE = ROOT / "runtime" / "gemini-profile"
CHAT_TOKEN = "host-chat-token"  # the one gateway_host gives its adapter


def post_chat(port: int, result: dict) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat", method="POST",
        data=json.dumps({"prompt": "never submitted: the test selectors cannot match", "transaction_id": "hang-1", "timeout_s": 30}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {CHAT_TOKEN}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.time()
    try:
        with opener.open(request, timeout=60) as response:
            result["reply"] = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["seconds"] = round(time.time() - started, 1)


def alive(pid: int | None) -> bool:
    return bool(pid) and psutil.pid_exists(pid)


def state_of(pids: dict[str, int | None], port: int) -> dict:
    return {**{name: alive(pid) for name, pid in pids.items()}, "adapter_listening": rp.listening(port),
            "profile_chrome_processes": len(rp.all_chrome())}


def scenario(kind: str) -> dict:
    """kind: A = owned Chrome frozen with a chat in flight; B = same with an EXTERNAL Chrome; C = owned, idle."""
    rp.cleanup()
    out: dict = {"kind": kind}
    external = None
    if kind == "B":
        external = chrome_mod.launch(rp.CHROME, rp.PROFILE, True, 30, rp.WORK / "chrome-logs")
        out["external_chrome_pid"] = external.pid
    h = rp.GatewayHost()
    chat_result: dict = {}
    thread = None
    try:
        adapter_pid = rp.wait_for(lambda: h.adapter_pid(), 20)
        rp.warm(h.port)
        browser = rp.browser_pids()
        out["before"] = {"adapter_pid": adapter_pid, "chrome_browser_pids": browser, "owner_file": (rp.PROFILE / "chrome-gemini-owner.json").exists()}
        victim = psutil.Process(browser[0])
        victim.suspend()  # the browser stops answering; the adapter's socket stays open
        out["suspended_chrome_pid"] = victim.pid
        if kind in ("A", "B"):
            thread = threading.Thread(target=post_chat, args=(h.port, chat_result), daemon=True)
            thread.start()
            time.sleep(1.0)  # let the exchange reach its first CDP call and hang there
        pids = {"adapter": adapter_pid, "chrome_browser": victim.pid, "external_chrome": external.pid if external else None}
        t0 = time.time()
        quit_reply = h.cmd("quit")  # gateway.close(): token-protected /shutdown, then terminate if it does not stop
        h.proc.wait(60)
        out["gateway_quit"] = {"reply": quit_reply, "seconds": round(time.time() - t0, 1), "host_exit": h.proc.returncode}
        time.sleep(1.0)
        out["after"] = state_of(pids, h.port)
        if thread:
            thread.join(30)
            out["chat_during_shutdown"] = {k: (v if k != "reply" else {x: v[x] for x in ("status", "error") if x in v}) for k, v in chat_result.items()}
        out["chrome_pids_left"] = rp.all_chrome()
    finally:
        h.close()
        for entry in rp.adapters_on_profile():
            try:
                psutil.Process(entry["pid"]).kill()
            except psutil.NoSuchProcess:
                pass
        for p in chrome_mod.processes_using_profile(rp.PROFILE):
            try:
                p.resume()
            except psutil.Error:
                pass
        time.sleep(0.5)
        out["cleanup_leftovers_before_cleanup"] = rp.all_chrome()
        rp.cleanup()
        out["cleanup_leftovers_after"] = rp.all_chrome()
    return out


def main() -> None:
    assert rp.HOST.is_file(), "cargo build -p lga-gateway --example gateway_host"
    prod_before = sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))
    chosen = sys.argv[1:] or ["A", "B", "C"]
    report = {k: scenario(k) for k in chosen}
    report["production_profile_chrome_pids"] = {
        "before": prod_before, "after": sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))}
    report["leftovers"] = {"chrome": rp.all_chrome(), "adapters": rp.adapters_on_profile()}
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
