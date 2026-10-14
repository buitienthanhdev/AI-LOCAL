"""Chat window: how a completion is shown (gap G2/G7 step 5). Drives apps/agent-desktop/ui/index.html in headless
Chromium against an in-memory mock of the desktop API. No model, no network, no agent process.

    python tests/ui/completion_basis_test.py        (needs `pip install playwright` and `playwright install chromium`)

The record shapes are the ones the core writes (TaskRecord): `completion_basis` is "verified" | "unverified" |
"direct_answer" | absent (records written before the field existed).
"""
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "apps/agent-desktop/ui/index.html").read_bytes()
UNCONFIRMED = ["check 0 (filesystem.stat): exists = false, expected true", "check 1 (desktop.read_window_text): text missing in result"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def completed(task_id: str, goal: str, summary: str, **extra) -> dict:
    return {"task_id": task_id, "goal": goal, "mode": "normal", "status": "completed", "created_at": now(), "updated_at": now(),
            "plan": [], "steps": [], "final_summary": summary, "failure_reason": None, "artifacts": [], "escalation": None,
            "pending_question": None, "attachments": [], **extra}


CASES = {
    "verified": lambda tid, goal: completed(tid, goal, "Đã tạo tệp và kiểm tra lại.", completion_basis="verified", unverified_checks=[]),
    "unverified": lambda tid, goal: completed(tid, goal, "⚠ Lưu ý: chưa kiểm chứng được đầy đủ kết quả.\n\nTôi đã vẽ xong.",
                                              completion_basis="unverified", unverified_checks=UNCONFIRMED),
    "direct": lambda tid, goal: completed(tid, goal, "Xin chào!", completion_basis="direct_answer", unverified_checks=[]),
    "old": lambda tid, goal: completed(tid, goal, "Kết quả của một bản ghi cũ."),  # no completion_basis at all
}
TASKS: dict[str, dict] = {}
COUNTER = [0]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML)
        elif path.startswith("/vendor/"):
            f = ROOT / "apps/agent-desktop/ui" / path.lstrip("/")
            if not f.is_file():
                return self._json({"error": "nope"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if f.suffix == ".js" else "text/css" if f.suffix == ".css" else "font/woff2")
            self.end_headers()
            self.wfile.write(f.read_bytes())
        elif path == "/api/status":
            self._json({"gateway": True, "brains": [], "knowledge": True, "vision": False, "default_mode": "normal"})
        elif path == "/api/tasks":
            self._json({"tasks": [{"task_id": t["task_id"], "goal": t["goal"], "status": t["status"], "mode": "normal",
                                   "updated_at": t["updated_at"], "created_at": t["created_at"], "steps": 0, "attachments": 0,
                                   "interrupted": False, "conversation_id": t["task_id"],
                                   "completion_basis": t.get("completion_basis")} for t in TASKS.values()]})
        elif path.startswith("/api/tasks/") and path.endswith("/events"):
            self._json({"events": [], "next": 0})
        elif path.startswith("/api/tasks/"):
            t = TASKS.get(path.split("/")[3])
            if not t:
                return self._json({"error": "unknown task"}, 404)
            self._json({"task": t, "pending_request": None, "pause_requested": False, "interrupted": False, "conversation_id": t["task_id"]})
        else:
            self._json({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/tasks":
            COUNTER[0] += 1
            tid = f"t{COUNTER[0]:03d}"
            kind = body["task"].split(":")[0]
            TASKS[tid] = CASES[kind](tid, body["task"])
            self._json({"task_id": tid, "conversation_id": tid, "attachments": 0})
        else:
            self._json({})


server = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=server.serve_forever, daemon=True).start()
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def ask(page, kind: str) -> None:
    page.click("#newBtn")
    page.wait_for_selector(".hello")
    page.fill("#input", f"{kind}: một việc")
    page.press("#input", "Enter")
    page.wait_for_selector(".answer", timeout=8000)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1100, "height": 800})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
    page.wait_for_selector(".hello")

    ask(page, "verified")
    check("verified: green answer", page.locator(".answer.ok").count() == 1)
    check("verified: plain completion header", "Hoàn thành" in page.locator(".answer .status").inner_text())
    check("verified: no unverified notice", page.locator(".unverified").count() == 0)

    ask(page, "unverified")
    check("unverified: warning answer, not green", page.locator(".answer.warn").count() == 1 and page.locator(".answer.ok").count() == 0)
    check("unverified: header says it was not fully verified", "chưa kiểm chứng đủ" in page.locator(".answer .status").inner_text(),
          page.locator(".answer .status").inner_text())
    notice = page.locator(".unverified").inner_text()
    check("unverified: states how many conditions were not confirmed", "2 điều kiện" in notice, notice)
    page.locator(".unverified summary").click()
    items = page.locator(".unverified li").all_inner_texts()
    check("unverified: lists every unconfirmed check, verbatim", items == UNCONFIRMED, str(items))

    ask(page, "direct")
    check("direct answer: normal completion", page.locator(".answer.ok").count() == 1 and page.locator(".unverified").count() == 0)

    ask(page, "old")
    check("old record without the field: normal completion, no notice",
          page.locator(".answer.ok").count() == 1 and page.locator(".unverified").count() == 0
          and "Hoàn thành" in page.locator(".answer .status").inner_text())

    check("no script errors", not errors, "; ".join(errors))
    browser.close()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
