"""Live tests of the browser overlay on a real Chrome (throwaway profile, headless, local HTTP pages).

Run explicitly:  uv run pytest --run-browser tests/test_browser_overlay_live.py -v
Covers: injection, navigation, reload, new tab, popup, input shield (CDP mouse/keys go through the same
hit-testing as a real mouse), agent actions passing, Stop (marker + in-flight call cancelled + later calls
refused + loading stopped), timeout, Chrome crash and the user's other Chrome processes being untouched.
"""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import http.server
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psutil
import pytest
import pytest_asyncio

from lga_common.config import Config
from lga_worker.browser.overlay import CdpConnection
from lga_worker.browser.provider import BrowserProvider, BrowserSettings
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext, ToolRegistry
from lga_worker.server import build_registry
from lga_worker.services import Services

pytestmark = [pytest.mark.browser, pytest.mark.asyncio(loop_scope="module")]

CALL_BUDGET_S = 60.0
POLL_S = 0.1

PAGES = {
    "index.html": """<!doctype html><title>Idx</title><body style="margin:0">
<button id="b" style="position:fixed;left:0;top:0;width:400px;height:300px" onclick="window.clicks=(window.clicks||0)+1">B</button>
<input id="t" style="position:fixed;left:0;top:320px;width:300px">
<a id="pop" href="#" onclick="window.open('/two.html','_blank');return false" style="position:fixed;left:0;top:380px">popup</a>
</body>""",
    "two.html": "<!doctype html><title>Two</title><p>second page</p>",
    "slow.html": "<!doctype html><title>Slow</title><img src='/hang'>",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/hang":
            time.sleep(30)
            return
        name = self.path.split("?")[0].lstrip("/") or "index.html"
        body = PAGES.get(name, "").encode()
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class Harness:
    def __init__(self, config: Config, registry: ToolRegistry, services: Services, base: str, tasks: Path) -> None:
        self.config, self.registry, self.services, self.base, self.tasks = config, registry, services, base, tasks
        self.task_id = f"overlay-live-{uuid.uuid4().hex[:8]}"
        self.step = 0
        self.user_chrome = user_chrome_pids(tasks.parent / "profile")

    @property
    def provider(self) -> BrowserProvider:
        return self.services.peek("browser")

    def ctx(self) -> ToolContext:
        self.step += 1
        return ToolContext(request_id=f"r{self.step}", task_id=self.task_id, step_id=f"s{self.step}",
                           deadline=time.monotonic() + CALL_BUDGET_S, config=self.config, services=self.services,
                           cancel_event=threading.Event())

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        _, outcome = await self.registry.invoke(self.ctx(), tool, args or {})
        return outcome

    def record(self, status: str, steps: list[dict[str, Any]] | None = None) -> None:
        (self.tasks / f"{self.task_id}.json").write_text(json.dumps({"status": status, "steps": steps or []}),
                                                         encoding="utf-8")


class Probe:
    """Our own CDP view of the pages (what a user would see / do)."""

    def __init__(self, conn: CdpConnection) -> None:
        self.conn = conn
        self.sessions: dict[str, str] = {}

    async def pages(self) -> list[dict[str, Any]]:
        infos = (await self.conn.send("Target.getTargets"))["targetInfos"]
        return [i for i in infos if i["type"] == "page"]

    async def session(self, target_id: str) -> str:
        if target_id not in self.sessions:
            self.sessions[target_id] = (await self.conn.send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        return self.sessions[target_id]

    async def eval(self, target_id: str, expr: str) -> Any:
        r = await self.conn.send("Runtime.evaluate", {"expression": expr, "returnByValue": True},
                                 await self.session(target_id))
        return r["result"].get("value")

    async def click(self, target_id: str, x: float, y: float) -> None:
        sid = await self.session(target_id)
        for kind in ("mousePressed", "mouseReleased"):
            await self.conn.send("Input.dispatchMouseEvent",
                                 {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}, sid)

    async def wait(self, target_id: str, expr: str, timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with_value = await self.eval(target_id, expr)
            if with_value:
                return with_value
            await asyncio.sleep(POLL_S)
        raise AssertionError(f"timed out waiting for {expr}")


def user_chrome_pids(our_profile: Path) -> list[int]:
    """Chrome browser processes that are NOT ours (e.g. the user's main Chrome)."""
    ours = str(our_profile).lower()
    pids = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        cmd = " ".join(proc.info["cmdline"] or []).lower()
        if (proc.info["name"] or "").lower() == "chrome.exe" and "--type=" not in cmd and ours not in cmd:
            pids.append(proc.pid)
    return pids


ALIVE = "!!(window.__lgaOverlay && window.__lgaOverlay.alive())"


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def harness(config: Config, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Harness]:
    root = tmp_path_factory.mktemp("overlay")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    services = Services(config)
    settings = dataclasses.replace(BrowserSettings.from_config(config), profile_dir=root / "profile",
                                   tasks_dir=root / "tasks", headless=True, keep_open=False, cdp_url="")
    services._extra["browser"] = BrowserProvider(settings)  # same object the tools get via get_or_create
    (root / "tasks").mkdir()
    h = Harness(config, build_registry(config.path("paths.tools_dir")), services,
                f"http://127.0.0.1:{server.server_address[1]}", root / "tasks")
    h.record("executing", [{"tool": "browser.open", "finished_at": None, "thought": "open the page"}])
    yield h
    pid = h.provider.chrome_pid
    await h.provider.close(keep_open=False)
    await services.close()
    server.shutdown()
    if pid is not None:
        assert not psutil.pid_exists(pid) or not psutil.Process(pid).is_running()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def probe(harness: Harness) -> AsyncIterator[Probe]:
    await harness.call("browser.open", {"url": harness.base + "/index.html"})
    conn = await CdpConnection.open(harness.provider.cdp_url)
    yield Probe(conn)
    await conn.close()


async def _page(probe: Probe, title: str) -> str:
    for _ in range(50):
        for p in await probe.pages():
            if p["title"] == title:
                return p["targetId"]
        await asyncio.sleep(POLL_S)
    raise AssertionError(f"no tab titled {title}")


async def test_1_overlay_injected_with_progress(harness: Harness, probe: Probe) -> None:
    tid = await _page(probe, "Idx")
    assert await probe.wait(tid, ALIVE)
    assert await probe.eval(tid, "document.querySelectorAll('lga-agent-overlay').length") == 1
    # browser-use must not offer the overlay as a clickable element, nor its text as page text
    state = await harness.call("browser.get_state")
    assert all("Dừng" not in json.dumps(e, ensure_ascii=False) for e in state.data["elements"])
    assert "Agent" not in state.data["text_excerpt"]


async def test_2_shield_blocks_user_input_but_agent_actions_pass(harness: Harness, probe: Probe) -> None:
    tid = await _page(probe, "Idx")
    await probe.click(tid, 100, 100)  # a "user" click while the agent owns the page
    await probe.conn.send("Input.insertText", {"text": "user"}, await probe.session(tid))
    assert await probe.eval(tid, "window.clicks||0") == 0
    assert await probe.eval(tid, "document.getElementById('t').value") == ""
    state = await harness.call("browser.get_state")
    button = next(e for e in state.data["elements"] if e["tag"] == "button")
    await harness.call("browser.click", {"index": button["index"]})  # the agent's click goes through
    assert await probe.eval(tid, "window.clicks||0") == 1


async def test_3_survives_navigation_reload_new_tab_and_popup(harness: Harness, probe: Probe) -> None:
    await harness.call("browser.navigate", {"url": harness.base + "/two.html"})
    tid = await _page(probe, "Two")
    assert await probe.wait(tid, ALIVE)
    await probe.conn.send("Page.reload", {}, await probe.session(tid))
    await asyncio.sleep(0.5)
    assert await probe.wait(tid, ALIVE)
    await harness.call("browser.navigate", {"url": harness.base + "/two.html?tab", "new_tab": True})
    tab = next(p["targetId"] for p in await probe.pages() if p["url"].endswith("?tab"))
    assert await probe.wait(tab, ALIVE)
    await harness.call("browser.navigate", {"url": harness.base + "/index.html"})
    idx = await _page(probe, "Idx")
    await probe.eval(idx, "document.getElementById('pop').click()")  # page-initiated popup
    for _ in range(50):
        pops = [p for p in await probe.pages() if p["url"].endswith("/two.html")]
        if pops:
            break
        await asyncio.sleep(POLL_S)
    assert pops and await probe.wait(pops[-1]["targetId"], ALIVE)


async def test_4_timeout_keeps_overlay_and_session(harness: Harness, probe: Probe) -> None:
    with pytest.raises(ToolError) as exc:
        await harness.call("browser.wait_for", {"text_contains": "never-there", "timeout_ms": 500})
    assert exc.value.code == ErrorCode.TIMEOUT
    tid = await _page(probe, "Idx")
    assert await probe.eval(tid, ALIVE)
    assert harness.provider.connected


async def test_5_chrome_crash_relaunches_and_overlay_returns(harness: Harness, probe: Probe) -> None:
    old = harness.provider.chrome_pid
    psutil.Process(old).kill()
    psutil.Process(old).wait(timeout=10) if psutil.pid_exists(old) else None
    await harness.call("browser.navigate", {"url": harness.base + "/index.html"})
    assert harness.provider.chrome_pid != old
    conn = await CdpConnection.open(harness.provider.cdp_url)
    probe.conn, probe.sessions = conn, {}
    tid = await _page(probe, "Idx")
    assert await probe.wait(tid, ALIVE, timeout=15)


async def test_6_stop_cancels_in_flight_call_and_refuses_later_ones(harness: Harness, probe: Probe) -> None:
    await harness.call("browser.navigate", {"url": harness.base + "/index.html"})
    tid = await _page(probe, "Idx")
    assert await probe.wait(tid, ALIVE)
    long_call = asyncio.create_task(harness.call("browser.wait_for", {"text_contains": "never", "timeout_ms": 30000}))
    await asyncio.sleep(1.0)
    started = time.monotonic()
    await probe.eval(tid, "window.__lgaAgentStop('test')")  # the binding the Stop button calls
    with pytest.raises((asyncio.CancelledError, ToolError)):
        await asyncio.wait_for(long_call, 5)
    assert time.monotonic() - started < 3
    assert (harness.tasks / f"{harness.task_id}.cancel").exists()
    with pytest.raises(ToolError) as exc:
        await harness.call("browser.get_state")
    assert exc.value.code == ErrorCode.CANCELLED
    assert await probe.wait(tid, "document.querySelector('lga-agent-overlay') !== null")
    await probe.click(tid, 100, 100)  # shield lowered after Stop: the browser is usable again
    assert await probe.wait(tid, "(window.clicks||0) >= 1")


async def test_7_real_click_on_stop_button(harness: Harness, probe: Probe) -> None:
    harness.task_id = f"overlay-live-{uuid.uuid4().hex[:8]}"
    harness.record("executing", [{"tool": "browser.click", "finished_at": None, "thought": "x"}])
    await harness.call("browser.navigate", {"url": harness.base + "/index.html"})
    tid = await _page(probe, "Idx")
    assert await probe.wait(tid, ALIVE)
    x, y = await probe.wait(tid, "(() => { const h = document.querySelector('lga-agent-overlay');"
                                 " const b = h && h.shadowRoot && h.shadowRoot.querySelector('button');"
                                 " if (!b) return null; const r = b.getBoundingClientRect();"
                                 " return r.width ? [r.x + r.width / 2, r.y + r.height / 2] : null; })()")
    await probe.click(tid, x, y)  # mouse click through normal hit-testing, like the user's
    marker = harness.tasks / f"{harness.task_id}.cancel"
    for _ in range(50):
        if marker.exists():
            break
        await asyncio.sleep(POLL_S)
    assert marker.exists()
    assert await probe.eval(tid, "window.clicks||0") == 0  # the click did not leak into the page
    with pytest.raises(ToolError):
        await harness.call("browser.get_state")


async def test_8_overlay_removed_when_task_finishes(harness: Harness, probe: Probe) -> None:
    harness.task_id = f"overlay-live-{uuid.uuid4().hex[:8]}"
    harness.record("executing", [{"tool": "browser.navigate", "finished_at": None, "thought": "x"}])
    await harness.call("browser.navigate", {"url": harness.base + "/index.html"})
    tid = await _page(probe, "Idx")
    assert await probe.wait(tid, ALIVE)
    harness.record("completed")
    assert await probe.wait(tid, "!document.querySelector('lga-agent-overlay')", timeout=5)
    await probe.click(tid, 100, 100)
    assert await probe.wait(tid, "(window.clicks||0) >= 1")


async def test_9_user_chrome_untouched(harness: Harness) -> None:
    alive = [pid for pid in harness.user_chrome if psutil.pid_exists(pid)]
    assert alive == harness.user_chrome, "a Chrome process of another profile died during the tests"
