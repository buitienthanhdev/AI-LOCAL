"""Phase H4A: Gateway -> adapter -> CDP -> a real Chrome -> a LOCAL stand-in for the Gemini page.

No Gemini, no account and no network: the page is tests/fake_gemini.py, which counts on the server side every
prompt that actually arrives. The adapter runs with the SHIPPED selector file (configs/chrome-gemini.selectors.json),
so this also checks that file against the structure phase F observed. Isolated profile runtime/e2e-test-profile.

    cargo build -p lga-gateway --example gateway_host
    services/python-agent/.venv/Scripts/python.exe services/chrome-gemini/tests/e2e_probe.py [S1 S2 ...]
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import recovery_probe as rp  # noqa: E402
from fake_gemini import FakeGemini  # noqa: E402
from lga_worker.browser import chrome as chrome_mod  # noqa: E402

rp.PROFILE = ROOT / "runtime" / "e2e-test-profile"
rp.WORK = ROOT / "logs" / "dev" / "e2e"
SHIPPED_SELECTORS = ROOT / "configs" / "chrome-gemini.selectors.json"
CHAT_TOKEN = "host-chat-token"  # what gateway_host gives its adapter
PRODUCTION_PROFILE = ROOT / "runtime" / "gemini-profile"


class E2EHost:
    """gateway_host with the shipped selectors and the stand-in page's URL."""

    def __init__(self, fake: FakeGemini) -> None:
        rp.WORK.mkdir(parents=True, exist_ok=True)
        self.port = rp.free_port()
        self.proc = subprocess.Popen(
            [str(rp.HOST), str(rp.PYTHON), str(ROOT / "services" / "chrome-gemini"), str(ROOT / "services" / "python-agent"),
             str(self.port), str(rp.PROFILE), str(rp.WORK / "chrome-logs"), str(SHIPPED_SELECTORS), str(rp.CHROME), fake.url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        assert json.loads(self.proc.stdout.readline())["event"] == "ready"

    def cmd(self, line: str):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def chat(self, prompt: str, timeout_ms: int = 40_000) -> dict:
        return self.cmd(f"chat {timeout_ms} {prompt}")

    def adapter_pid(self):
        return self.cmd("health")[0]["process"].get("pid")

    def quit(self) -> float:
        t0 = time.time()
        self.cmd("quit")
        self.proc.wait(60)
        return round(time.time() - t0, 1)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.quit()
            except Exception:  # noqa: BLE001
                self.proc.kill()


def adapter_post(port: int, prompt: str, tid: str, token: str | None = CHAT_TOKEN, timeout_s: float = 30) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat", method="POST", headers=headers,
                                     data=json.dumps({"prompt": prompt, "transaction_id": tid, "timeout_s": timeout_s}).encode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=90) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        return -1, {"transport_error": f"{type(exc).__name__}: {exc}"}


def adapter_get(port: int, tid: str, token: str | None = CHAT_TOKEN) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/transactions/{tid}", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def start_host(fake: FakeGemini) -> E2EHost:
    host = E2EHost(fake)
    rp.wait_for(lambda: host.adapter_pid(), 20)
    return host


def common_ok(r: dict) -> dict:
    return {k: r.get(k) for k in ("ok", "backend", "content", "model", "kind", "attempts", "attempt_kinds")} | \
        {"transaction_status": ((r.get("usage") or {}).get("transaction") or {}).get("status")}


# ---------------------------------------------------------------------------------------------
def s1_to_s6(fake: FakeGemini) -> dict:
    out: dict = {}
    host = start_host(fake)
    try:
        fake.set(delay_ms=300, stuck=False, nostop=False, login=False)

        # S1 happy path through the gateway
        out["S1"] = {"reply": common_ok(host.chat("hello one")), "prompts": fake.prompts()}

        # S2 authentication: nothing reaches the page without the chat token
        before = len(fake.prompts())
        statuses = [adapter_post(host.port, "no token", "s2a", token=None)[0],
                    adapter_post(host.port, "wrong token", "s2b", token="nope")[0],
                    adapter_post(host.port, "shutdown token", "s2c", token="host-test-token")[0]]
        out["S2"] = {"statuses": statuses, "submissions_added": len(fake.prompts()) - before}

        # S3 duplicate transaction id: the page sees one prompt
        before = len(fake.prompts())
        a = adapter_post(host.port, "dup prompt", "dup-1")
        b = adapter_post(host.port, "dup prompt", "dup-1")
        c = adapter_post(host.port, "a different prompt", "dup-1")
        out["S3"] = {"first": a[1].get("status"), "second": b[1].get("status"), "same_content": a[1].get("content") == b[1].get("content"),
                     "different_prompt": [c[1].get("status"), (c[1].get("error") or {}).get("class")],
                     "submissions_added": len(fake.prompts()) - before}

        # S4 concurrent requests: serialised, never mixed
        before = len(fake.prompts())
        results: dict[str, tuple[int, dict]] = {}
        threads = [threading.Thread(target=lambda i=i: results.__setitem__(f"q{i}", adapter_post(host.port, f"question {i}", f"c{i}")))
                   for i in range(3)]
        [t.start() for t in threads]
        [t.join(120) for t in threads]
        added = fake.prompts()[before:]
        out["S4"] = {
            "results": {k: {"status": v[1].get("status"), "content": v[1].get("content"),
                            "class": (v[1].get("error") or {}).get("class")} for k, v in results.items()},
            "submitted": added,
        }

        # S5 timeout while generating: uncertain, exactly one prompt, nothing re-sent afterwards
        fake.set(stuck=True)
        before = len(fake.prompts())
        r5 = host.chat("will not finish", timeout_ms=7_000)
        time.sleep(4)  # a blind retry would show up here
        out["S5"] = {"reply": common_ok(r5), "submissions_added": len(fake.prompts()) - before}
        fake.set(stuck=False)

        # S6 no usable completion signal: never declared COMPLETED
        fake.set(nostop=True, delay_ms=300)
        before = len(fake.prompts())
        r6 = host.chat("signals are missing", timeout_ms=11_000)
        out["S6"] = {"reply": common_ok(r6), "submissions_added": len(fake.prompts()) - before}
        fake.set(nostop=False)

        out["quit_seconds"] = host.quit()
    finally:
        host.close()
    return out


def s7_shutdown_in_flight(fake: FakeGemini) -> dict:
    fake.set(delay_ms=300, stuck=True, nostop=False, login=False)
    before = len(fake.prompts())
    host = start_host(fake)
    out: dict = {}
    reply: dict = {}
    try:
        def call() -> None:
            reply["status"], reply["body"] = adapter_post(host.port, "interrupted by shutdown", "s7", timeout_s=60)

        t = threading.Thread(target=call)
        t.start()
        rp.wait_for(lambda: len(fake.prompts()) > before, 30)  # the prompt reached the page; now generating forever
        time.sleep(0.5)
        out["quit_seconds"] = host.quit()
        t.join(30)
        body = reply.get("body", {})
        out["chat"] = {"http": reply.get("status"), "status": body.get("status"), "class": (body.get("error") or {}).get("class"),
                       "content": body.get("content"), "transport_error": body.get("transport_error")}
        out["submissions_added"] = len(fake.prompts()) - before
        time.sleep(1)
        out["after"] = {"chrome": rp.all_chrome(), "adapters": rp.adapters_on_profile(), "listening": rp.listening(host.port)}
    finally:
        host.close()
    return out


def s8_chrome_dies_mid_generation(fake: FakeGemini) -> dict:
    fake.set(stuck=True, nostop=False, login=False)
    before = len(fake.prompts())
    host = start_host(fake)
    out: dict = {}
    reply: dict = {}
    try:
        def call() -> None:
            reply["status"], reply["body"] = adapter_post(host.port, "chrome will die", "s8", timeout_s=40)

        t = threading.Thread(target=call)
        t.start()
        rp.wait_for(lambda: len(fake.prompts()) > before, 30)
        time.sleep(1.0)
        victim = rp.browser_pids()
        psutil.Process(victim[0]).kill()  # by PID: the browser process of the test profile only
        t.join(60)
        body = reply.get("body", {})
        time.sleep(1)
        out["chat"] = {"status": body.get("status"), "class": (body.get("error") or {}).get("class"), "content": body.get("content")}
        out["submissions_added"] = len(fake.prompts()) - before
        out["chrome_relaunched"] = bool(rp.browser_pids())
        out["quit_seconds"] = host.quit()
    finally:
        host.close()
    return out


def s10_late_answer(fake: FakeGemini) -> dict:
    """The answer arrives AFTER the transaction ended: recorded beside the result, nothing re-sent or overwritten."""
    fake.set(delay_ms=9000, stuck=False, nostop=False, login=False)
    before = len(fake.prompts())
    host = start_host(fake)
    out: dict = {}
    try:
        status, first = adapter_post(host.port, "late answer please", "late-e2e", timeout_s=3)
        out["terminal"] = {"http": status, "status": first.get("status"), "class": (first.get("error") or {}).get("class"),
                           "content": first.get("content"), "last_observed_state": first.get("metadata", {}).get("signals", {}).get("last_observed_state"),
                           "late_at_that_moment": first.get("late_observation")}
        # while the page is still generating there is nothing to record
        mid_status, mid = adapter_get(host.port, "late-e2e")
        out["lookup_while_generating"] = {"http": mid_status, "late": mid.get("late_observation")}
        time.sleep(12)  # the page finishes on its own
        status2, later = adapter_get(host.port, "late-e2e")
        out["lookup_after_answer"] = {"http": status2, "status": later.get("status"), "class": (later.get("error") or {}).get("class"),
                                      "content": later.get("content"), "late": later.get("late_observation"),
                                      "finished_at_unchanged": later.get("metadata", {}).get("finished_at") == first.get("metadata", {}).get("finished_at")}
        out["lookup_requires_chat_token"] = [adapter_get(host.port, "late-e2e", token=None)[0], adapter_get(host.port, "late-e2e", token="host-test-token")[0],
                                             adapter_get(host.port, "no-such-id")[0]]
        # the same id again returns the record, never the page
        status3, again = adapter_post(host.port, "late answer please", "late-e2e", timeout_s=3)
        out["same_id_again"] = {"http": status3, "status": again.get("status"), "has_late": bool(again.get("late_observation"))}
        out["submissions_added"] = len(fake.prompts()) - before
        out["quit_seconds"] = host.quit()
    finally:
        host.close()
        fake.set(delay_ms=300)
    return out


def s9_logged_out(fake: FakeGemini) -> dict:
    fake.set(login=True, stuck=False, nostop=False)
    before = len(fake.prompts())
    host = start_host(fake)
    out: dict = {}
    try:
        out["reply"] = common_ok(host.chat("anyone there?", timeout_ms=20_000))
        out["submissions_added"] = len(fake.prompts()) - before
        out["quit_seconds"] = host.quit()
    finally:
        host.close()
        fake.set(login=False)
    return out


def main() -> None:
    assert rp.HOST.is_file(), "cargo build -p lga-gateway --example gateway_host"
    prod_before = sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))
    fake = FakeGemini().start()
    report: dict = {}
    try:
        chosen = sys.argv[1:] or ["S1-6", "S7", "S8", "S9", "S10"]
        table = {"S1-6": s1_to_s6, "S7": s7_shutdown_in_flight, "S8": s8_chrome_dies_mid_generation, "S9": s9_logged_out,
                 "S10": s10_late_answer}
        for name in chosen:
            rp.cleanup()
            report[name] = table[name](fake)
        report["page_loads"] = fake.page_loads
        report["all_prompts_seen_by_the_page"] = fake.prompts()
    finally:
        fake.stop()
        rp.cleanup()
    report["production_profile_chrome_pids"] = {
        "before": prod_before, "after": sorted(p.pid for p in chrome_mod.processes_using_profile(PRODUCTION_PROFILE))}
    report["leftovers"] = {"chrome": rp.all_chrome(), "adapters": rp.adapters_on_profile()}
    print(json.dumps(report, indent=1, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
