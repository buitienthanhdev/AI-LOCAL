"""Live browser tests: launch the dedicated-profile Chrome and drive a real page over CDP.

Run explicitly:  uv run pytest -m browser --run-browser -v tests/test_browser_live.py
The tests share one worker `Services` (one Chrome) and one event loop and run in file order.
"""

from __future__ import annotations

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
from lga_worker.registry import ToolContext, ToolRegistry
from lga_worker.server import build_registry
from lga_worker.services import Services

pytestmark = [pytest.mark.browser, pytest.mark.asyncio(loop_scope="module")]

REPO_URL = "https://github.com/Jeomon/Windows-Use"
REPO_HOST = "github.com"
CALL_BUDGET_S = 120.0
WAIT_TIMEOUT_MS = 20_000
KILL_WAIT_S = 10
POLL_S = 0.2


class Harness:
    def __init__(self, config: Config, registry: ToolRegistry, services: Services) -> None:
        self.config, self.registry, self.services = config, registry, services
        self.task_id = f"browser-live-{uuid.uuid4().hex[:8]}"
        self.step = 0

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        self.step += 1
        ctx = ToolContext(request_id=f"r{self.step}", task_id=self.task_id, step_id=f"s{self.step}",
                          deadline=time.monotonic() + CALL_BUDGET_S, config=self.config, services=self.services,
                          cancel_event=threading.Event())
        spec, outcome = await self.registry.invoke(ctx, tool, args or {})
        status = outcome.verification_status(spec.verification)
        print(f"\n[{tool}] {status}: {outcome.observation[:600]}")
        assert status != "failed", f"{tool} verification failed: {outcome.checks}"
        return outcome


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def harness(config: Config) -> AsyncIterator[Harness]:
    services = Services(config)
    registry = build_registry(config.path("paths.tools_dir"))
    yield Harness(config, registry, services)
    provider = services.peek("browser")
    pid = provider.chrome_pid if provider else None
    provider = services.peek("browser")
    if provider is not None:
        await provider.close(keep_open=False)  # the test must not leave its Chrome behind
    await services.close()
    if pid is not None:
        assert not psutil.pid_exists(pid) or not psutil.Process(pid).is_running()


async def test_1_status_does_not_launch(harness: Harness) -> None:
    outcome = await harness.call("browser.status")
    assert outcome.data["ready"] is True, outcome.observation
    assert outcome.data["connected"] is False
    assert harness.services.peek("browser") is None


async def test_2_open_and_navigate(harness: Harness) -> None:
    opened = await harness.call("browser.open")
    assert opened.data["mode"] in {"launched", "reused"}
    nav = await harness.call("browser.navigate", {"url": REPO_URL})
    assert REPO_HOST in nav.data["final_url"]
    assert "Windows-Use" in nav.data["title"]


async def test_3_get_state_and_page_matches(harness: Harness) -> None:
    state = await harness.call("browser.get_state")
    assert state.data["url"].startswith(REPO_URL)
    assert state.data["element_count"] > 0 and state.data["elements"]
    assert state.data["text_excerpt"]
    match = await harness.call("browser.page_matches", {"url_contains": "Jeomon/Windows-Use",
                                                        "title_contains": "Windows-Use"})
    assert match.data["matches"] is True
    miss = await harness.call("browser.page_matches", {"title_contains": "definitely-not-this-title"})
    assert miss.data["matches"] is False


async def test_4_click_issues_tab_changes_url(harness: Harness) -> None:
    state = await harness.call("browser.get_state", {"max_elements": 2000})
    candidates = [e for e in state.data["elements"]
                  if e["tag"] == "a" and e["attributes"].get("href", "").endswith("/issues")
                  and e["text"].lower().startswith("issues")]
    assert candidates, "no 'Issues' tab link in the page state"
    clicked = await harness.call("browser.click", {"index": candidates[0]["index"]})
    waited = await harness.call("browser.wait_for", {"url_contains": "/issues", "timeout_ms": WAIT_TIMEOUT_MS})
    assert "/issues" in waited.data["url"]
    match = await harness.call("browser.page_matches", {"url_contains": "Jeomon/Windows-Use/issues"})
    assert match.data["matches"] is True
    assert clicked.data["element"]["index"] == candidates[0]["index"]


async def test_5_stale_index_is_reported(harness: Harness) -> None:
    from lga_worker.errors import ErrorCode, ToolError

    with pytest.raises(ToolError) as exc:
        await harness.call("browser.click", {"index": 999_999_999})
    assert exc.value.code == ErrorCode.STALE_ELEMENT


async def test_6_screenshot_tabs_and_back(harness: Harness) -> None:
    shot = await harness.call("browser.screenshot")
    path = Path(shot.data["path"])
    assert path.is_file() and path.stat().st_size > 0
    assert shot.data["width"] > 0 and shot.data["height"] > 0
    assert shot.artifacts == [{"kind": "screenshot", "path": str(path)}]
    tabs = await harness.call("browser.tabs")
    assert sum(1 for t in tabs.data["tabs"] if t["active"]) == 1
    back = await harness.call("browser.go_back")
    assert back.data["url"].rstrip("/").endswith("Jeomon/Windows-Use")


async def test_7_recovers_after_chrome_is_killed(harness: Harness) -> None:
    provider = harness.services.peek("browser")
    assert provider is not None and provider.chrome_pid is not None
    old_pid = provider.chrome_pid
    victim = psutil.Process(old_pid)
    victim.kill()  # simulate a Chrome crash (browser process only; renderers die with it)
    victim.wait(timeout=KILL_WAIT_S)
    nav = await harness.call("browser.navigate", {"url": REPO_URL})
    assert nav.data["restarted"] is True or "restarted" in nav.observation.lower()
    assert "Windows-Use" in nav.data["title"]
    assert provider.chrome_pid is not None and provider.chrome_pid != old_pid
    state = await harness.call("browser.get_state")
    assert state.data["element_count"] > 0


SEARCH_URL = "https://github.com/search?q=Windows-Use"
OTHER_REPO_URL = "https://github.com/CursorTouch/Windows-MCP"


def _link(state: Any, *, href_suffix: str = "", text: str = "") -> int:
    for element in state.data["elements"]:
        href = element["attributes"].get("href", "")
        if element["tag"] == "a" and (not href_suffix or href.endswith(href_suffix)) \
                and (not text or element["text"].strip().lower() == text):
            return element["index"]
    raise AssertionError(f"no link href*={href_suffix!r} text={text!r} on {state.data['url']}")


async def test_8_stale_index_after_external_navigation_never_clicks(harness: Harness) -> None:
    """Regression (acceptance run 20261008-035252): the tab navigated by itself (not via a browser tool) between
    browser.get_state and browser.click. The click must be refused truthfully and must not click anything."""
    from lga_worker.errors import ErrorCode, ToolError

    await harness.call("browser.navigate", {"url": SEARCH_URL})
    await harness.call("browser.wait_for", {"text_contains": "Jeomon/", "timeout_ms": WAIT_TIMEOUT_MS})
    state = await harness.call("browser.get_state", {"max_elements": 2000})
    index = _link(state, href_suffix="/Jeomon/Windows-Use")
    provider = harness.services.peek("browser")
    async with provider.attached(CALL_BUDGET_S):
        await provider.evaluate(f"location.href = {OTHER_REPO_URL!r}", CALL_BUDGET_S)  # page-initiated navigation
    await harness.call("browser.wait_for", {"url_contains": "CursorTouch/Windows-MCP", "timeout_ms": WAIT_TIMEOUT_MS})
    with pytest.raises(ToolError) as exc:
        await harness.call("browser.click", {"index": index})
    assert exc.value.code == ErrorCode.STALE_ELEMENT and exc.value.details["performed"] is False
    assert "CursorTouch/Windows-MCP" in exc.value.message
    still = await harness.call("browser.page_matches", {"url_contains": "CursorTouch/Windows-MCP"})
    assert still.data["matches"] is True  # nothing was clicked on the new page
    # With a fresh state the same kind of click works and reports the page it led to.
    await harness.call("browser.navigate", {"url": SEARCH_URL})
    await harness.call("browser.wait_for", {"text_contains": "Jeomon/", "timeout_ms": WAIT_TIMEOUT_MS})
    state = await harness.call("browser.get_state", {"max_elements": 2000})
    clicked = await harness.call("browser.click", {"index": _link(state, href_suffix="/Jeomon/Windows-Use")})
    assert clicked.data["url_changed"] is True and clicked.data["url"].rstrip("/").endswith("Jeomon/Windows-Use")


async def test_9_sign_in_link_click_reports_the_new_url(harness: Harness) -> None:
    """Regression: clicking GitHub's 'Sign in' <a href="/login?return_to=..."> must report the login URL."""
    await harness.call("browser.navigate", {"url": SEARCH_URL})
    state = await harness.call("browser.get_state", {"max_elements": 2000})
    clicked = await harness.call("browser.click", {"index": _link(state, text="sign in")})
    assert clicked.data["url_changed"] is True, clicked.observation
    assert "/login" in clicked.data["url"] and "(changed)" in clicked.observation


async def test_10_keep_open_detach_then_reattach_from_new_services(harness: Harness) -> None:
    """Regression: close(keep_open=True) leaves Chrome running; a NEW Services re-attaches and works; then
    close(keep_open=False) really closes it."""
    first = harness.services.peek("browser")
    assert first is not None and first.chrome_pid is not None
    pid = first.chrome_pid
    await first.close(keep_open=True)  # what the worker does on shutdown with [browser] keep_open = true
    assert psutil.pid_exists(pid) and psutil.Process(pid).is_running(), "Chrome must stay open"

    second = Harness(harness.config, harness.registry, Services(harness.config))
    nav = await second.call("browser.navigate", {"url": REPO_URL})
    assert "Windows-Use" in nav.data["title"] and nav.data["restarted"] is False
    reattached = second.services.peek("browser")
    assert reattached.mode == "reused" and reattached.chrome_pid == pid
    state = await second.call("browser.get_state")
    clicked = await second.call("browser.click", {"index": _link(state, href_suffix="/issues")})
    assert "/issues" in clicked.data["url"]

    await reattached.close(keep_open=False)
    await second.services.close()
    deadline = time.monotonic() + KILL_WAIT_S
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(POLL_S)
    assert not psutil.pid_exists(pid), "Chrome must be closed"
