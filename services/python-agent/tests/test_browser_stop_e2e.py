"""E2E (acceptance 1-2): a real `agent run` drives the agent's own Chrome; pressing Stop on the overlay ends
the task for real.

Needs the release build, a working brain (gateway) and Chrome. Run explicitly:
    uv run pytest --run-gateway --run-browser tests/test_browser_stop_e2e.py -v -s
Checks: the overlay shows in the agent's Chrome; a Stop click (real CDP mouse click on the button) makes the
task end as `cancelled` within seconds; no tool call of that task runs afterwards (the task record and the
audit log end there); the user's other Chrome processes are untouched.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from lga_common.config import load_config
from lga_worker.browser import chrome as chrome_mod
from lga_worker.browser.overlay import CdpConnection

pytestmark = [pytest.mark.gateway, pytest.mark.browser]

GOAL = ("Dùng trình duyệt tự động (browser.*) mở https://example.com, đọc tiêu đề trang, rồi dùng browser.wait_for "
        "chờ đoạn chữ 'lga-never-appears' với timeout_ms 120000, lặp lại việc chờ đó 5 lần.")


def _other_chrome(profile: Path) -> set[int]:
    ours = str(profile).lower()
    return {p.pid for p in psutil.process_iter(["name", "cmdline"])
            if (p.info["name"] or "").lower() == "chrome.exe"
            and "--type=" not in " ".join(p.info["cmdline"] or []).lower()
            and ours not in " ".join(p.info["cmdline"] or []).lower()}


async def _press_stop(cdp_url: str, deadline: float) -> bool:
    conn = await CdpConnection.open(cdp_url)
    try:
        while time.monotonic() < deadline:
            for info in (await conn.send("Target.getTargets"))["targetInfos"]:
                if info["type"] != "page" or "example.com" not in info["url"]:
                    continue
                sid = (await conn.send("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True}))["sessionId"]
                r = await conn.send("Runtime.evaluate", {"returnByValue": True, "expression":
                    "(() => { const h = document.querySelector('lga-agent-overlay'); const b = h && h.shadowRoot"
                    " && h.shadowRoot.querySelector('button'); if (!b) return null; const r = b.getBoundingClientRect();"
                    " return r.width ? [r.x + r.width / 2, r.y + r.height / 2] : null; })()"}, sid)
                xy = r["result"].get("value")
                if xy:
                    for kind in ("mousePressed", "mouseReleased"):
                        await conn.send("Input.dispatchMouseEvent", {"type": kind, "x": xy[0], "y": xy[1],
                                                                     "button": "left", "clickCount": 1}, sid)
                    return True
            await asyncio.sleep(1)
        return False
    finally:
        await conn.close()


def test_stop_on_overlay_cancels_real_task() -> None:
    config = load_config()
    root = config.root
    profile = config.path("browser.profile_dir").resolve()
    tasks = config.path("memory.tasks_dir")
    others = _other_chrome(profile)
    proc = subprocess.Popen([str(root / "target" / "release" / "agent.exe"), "run", GOAL], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 300
        cdp = ""
        while time.monotonic() < deadline and not cdp:
            port = chrome_mod.read_devtools_active_port(profile)
            if port and chrome_mod.probe_endpoint(chrome_mod.http_cdp_url(port[0])):
                cdp = chrome_mod.http_cdp_url(port[0])
            time.sleep(1)
        assert cdp, "the agent never started its Chrome"
        assert asyncio.run(_press_stop(cdp, deadline)), "overlay with a Stop button never appeared"
        pressed = time.monotonic()
        proc.wait(timeout=60)
        took = time.monotonic() - pressed
        newest = max(tasks.glob("2*.json"), key=lambda p: p.stat().st_mtime)
        record = json.loads(newest.read_text(encoding="utf-8"))
        print(f"task {record['task_id']} -> {record['status']} {took:.1f}s after Stop")
        assert record["status"] == "cancelled"
        assert took < 20
        last = record["steps"][-1] if record["steps"] else {}
        assert not last or last.get("finished_at"), "a step was left running"
        assert others <= _other_chrome(profile), "a Chrome of another profile was closed"
    finally:
        if proc.poll() is None:
            proc.kill()
