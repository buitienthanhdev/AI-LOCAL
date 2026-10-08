"""Fast tests of the browser subsystem: contract binding, Chrome helpers, URL/state helpers, handlers with fakes.

Nothing here launches Chrome or imports browser_use.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lga_common.config import Config
from lga_worker.browser import chrome
from lga_worker.browser.provider import (
    Attached,
    BrowserProvider,
    BrowserSettings,
    PageInfo,
    discard_empty_temp_dir,
    png_size,
    to_tool_error,
)
from lga_worker.browser.state import (
    MASK,
    PageCriteria,
    hosts_match,
    is_error_page,
    resolve_tab_id,
    serialize_element,
    serialize_elements,
    state_observation,
)
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext
from lga_worker.server import build_registry
from lga_worker.services import Services

EXPECTED_VERIFICATION = {
    "browser.status": "none", "browser.open": "self", "browser.navigate": "self", "browser.tabs": "none",
    "browser.switch_tab": "self", "browser.close_tab": "self", "browser.get_state": "none",
    "browser.click": "observe", "browser.type": "self", "browser.press_keys": "observe", "browser.scroll": "observe",
    "browser.upload": "self", "browser.wait_for": "self", "browser.page_matches": "none",
    "browser.screenshot": "self", "browser.go_back": "observe", "browser.downloads": "none",
}
POLICY_CATEGORIES = {"browser.observe", "browser.navigate", "browser.input", "browser.upload", "browser.download"}
CALL_BUDGET_S = 30.0


# --------------------------------------------------------------------------- contract


def test_browser_contract_is_bound(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    browser_specs = {n: s for n, s in registry.specs.items() if n.startswith("browser.")}
    assert set(browser_specs) == set(EXPECTED_VERIFICATION)
    assert not [n for n in registry.unbound_tools() if n.startswith("browser.")]
    for name, spec in browser_specs.items():
        assert spec.verification == EXPECTED_VERIFICATION[name], name
        assert spec.policy_category in POLICY_CATEGORIES, name
        assert spec.input_schema.get("additionalProperties") is False, name


def test_url_args_carry_a_scheme_pattern(config: Config) -> None:
    document = json.loads((config.path("paths.tools_dir") / "browser.json").read_text(encoding="utf-8"))
    for tool in document["tools"]:
        url = tool["input_schema"].get("properties", {}).get("url")
        if url is not None:
            assert url["type"] == "string" and url["pattern"].startswith("^"), tool["name"]


def test_importing_browser_tools_does_not_import_browser_use() -> None:
    code = "import sys, lga_worker.tools.browser; assert 'browser_use' not in sys.modules, 'eager import'"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_settings_from_example_config(config: Config) -> None:
    settings = BrowserSettings.from_config(config)
    assert settings.profile_dir.is_absolute() and settings.downloads_dir.is_absolute()
    assert settings.launch_timeout_s > 0 and settings.poll_interval_s > 0
    assert settings.max_state_elements > 0


# --------------------------------------------------------------------------- chrome helpers


@pytest.mark.parametrize(("text", "port", "path"), [
    ("9222\n/devtools/browser/abc-123\n", 9222, "/devtools/browser/abc-123"),
    ("  51234  \r\n/devtools/browser/x", 51234, "/devtools/browser/x"),
    ("40000", 40000, ""),
])
def test_parse_devtools_active_port(text: str, port: int, path: str) -> None:
    assert chrome.parse_devtools_active_port(text) == (port, path)


@pytest.mark.parametrize("text", ["", "\n\n", "abc\n/devtools", "0\n/x", "70000\n/x"])
def test_parse_devtools_active_port_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        chrome.parse_devtools_active_port(text)


def test_read_devtools_active_port_file(tmp_path: Path) -> None:
    assert chrome.read_devtools_active_port(tmp_path) is None
    target = tmp_path / chrome.DEVTOOLS_ACTIVE_PORT_FILE
    target.write_text("", encoding="utf-8")
    assert chrome.read_devtools_active_port(tmp_path) is None  # still being written
    target.write_text("12345\n/devtools/browser/u", encoding="utf-8")
    assert chrome.read_devtools_active_port(tmp_path) == (12345, "/devtools/browser/u")


def test_build_command_uses_dedicated_profile(tmp_path: Path) -> None:
    exe = Path(r"C:\Chrome\chrome.exe")
    command = chrome.build_command(exe, tmp_path, headless=False)
    assert command[0] == str(exe)
    assert f"--user-data-dir={tmp_path}" in command
    assert "--remote-debugging-port=0" in command and chrome.HEADLESS_ARG not in command
    assert chrome.HEADLESS_ARG in chrome.build_command(exe, tmp_path, headless=True)


def test_probe_endpoint_without_listener_returns_none() -> None:
    assert chrome.probe_endpoint("http://127.0.0.1:9", timeout_s=0.5) is None


# --------------------------------------------------------------------------- URL / criteria / state helpers


@pytest.mark.parametrize(("requested", "final", "expected"), [
    ("https://github.com/Jeomon/Windows-Use", "https://github.com/Jeomon/Windows-Use", True),
    ("http://github.com", "https://github.com/", True),
    ("https://google.com", "https://www.google.com/?hl=vi", True),
    ("https://GitHub.com/a", "https://github.com/b", True),
    ("https://github.com", "https://login.microsoftonline.com/x", False),
    ("https://example.com", "chrome-error://chromewebdata/", False),
    ("about:blank", "about:blank", True),
])
def test_hosts_match(requested: str, final: str, expected: bool) -> None:
    assert hosts_match(requested, final) is expected


def test_error_page_detection() -> None:
    assert is_error_page("chrome-error://chromewebdata/")
    assert not is_error_page("https://example.com")


def test_page_criteria() -> None:
    criteria = PageCriteria.from_args({"url_contains": "/ISSUES", "title_contains": "windows-use"})
    assert not criteria.needs_text and not criteria.empty
    ok, details = criteria.evaluate("https://github.com/Jeomon/Windows-Use/issues", "Issues · Windows-Use", None)
    assert ok and len(details) == 2
    ok, details = criteria.evaluate("https://github.com/Jeomon/Windows-Use", "Windows-Use", None)
    assert not ok and any("no" in d for d in details)
    text_criteria = PageCriteria(text_contains="hello")
    assert text_criteria.needs_text
    assert text_criteria.evaluate("u", "t", "Say HELLO world")[0]
    assert not text_criteria.evaluate("u", "t", None)[0]
    assert PageCriteria.from_args({}).empty


class FakeNode:
    def __init__(self, tag: str, attributes: dict[str, str], text: str = "", role: str | None = None) -> None:
        self.tag_name = tag
        self.attributes = attributes
        self.ax_node = SimpleNamespace(role=role) if role else None
        self._text = text
        self.backend_node_id = 1

    def get_meaningful_text_for_llm(self) -> str:
        return self._text


def test_serialize_element_keeps_useful_attributes_and_masks_passwords() -> None:
    link = FakeNode("A", {"href": "/Jeomon/Windows-Use/issues", "class": "x y", "id": "issues-tab",
                          "data-foo": "bar"}, text="  Issues\n 3 ", role="link")
    element = serialize_element(1314, link)
    assert element == {"index": 1314, "tag": "a", "text": "Issues 3", "role": "link",
                       "attributes": {"id": "issues-tab", "href": "/Jeomon/Windows-Use/issues"}}
    secret = FakeNode("input", {"type": "password", "value": "hunter2", "name": "pw"}, text="hunter2")
    masked = serialize_element(7, secret)
    assert masked["attributes"]["value"] == MASK and masked["text"] == ""
    assert "hunter2" not in json.dumps(masked)


def test_serialize_elements_truncates_in_dom_order() -> None:
    nodes = {i: FakeNode("button", {"role": "button"}, text=f"b{i}") for i in (30, 10, 20)}
    elements, truncated = serialize_elements(nodes, 2)
    assert truncated and [e["index"] for e in elements] == [30, 10]
    assert serialize_elements(nodes, 10)[1] is False


def test_resolve_tab_id() -> None:
    ids = ["4FBDD7D717C6AF8CCF008F7A0F7BECC9", "11112222333344445555666677778888"]
    assert resolve_tab_id(ids[0], ids) == ids[0]
    assert resolve_tab_id("becc9", ids) == ids[0]
    assert resolve_tab_id("nope", ids) is None


def test_state_observation_is_bounded() -> None:
    tabs = [{"tab_id": "A", "url": "https://a", "title": "A", "active": True},
            {"tab_id": "B", "url": "https://b", "title": "B", "active": False}]
    text = state_observation("https://a", "A", tabs, "x" * 10_000, element_count=500, shown=250,
                             notes=["note"], max_chars=1_000)
    assert len(text) <= 1_100 and "truncated" in text and "* A" in text and "note" in text


def test_png_size() -> None:
    header = chrome_png_header(640, 480)
    assert png_size(header) == (640, 480)
    assert png_size(b"not a png") is None


def chrome_png_header(width: int, height: int) -> bytes:
    ihdr = b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"\x08\x06\x00\x00\x00"


def test_discard_empty_temp_dir_only_touches_browser_use_scratch_dirs(tmp_path: Path) -> None:
    temp_root = Path(tempfile.gettempdir())
    empty = temp_root / f"browser-use-user-data-dir-test{uuid.uuid4().hex[:6]}"
    full = temp_root / f"browser-use-downloads-test{uuid.uuid4().hex[:6]}"
    other = temp_root / f"lga-unrelated-{uuid.uuid4().hex[:6]}"
    for directory in (empty, full, other):
        directory.mkdir()
    (full / "file.bin").write_bytes(b"x")
    try:
        for directory in (empty, full, other):
            discard_empty_temp_dir(directory)
        assert not empty.exists() and full.exists() and other.exists()
        nested = tmp_path / "browser-use-user-data-dir-nested"
        nested.mkdir()
        discard_empty_temp_dir(nested)
        assert nested.exists()  # not directly under %TEMP%
    finally:
        (full / "file.bin").unlink(missing_ok=True)
        for directory in (full, other):
            directory.rmdir()


# --------------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(("exc", "code"), [
    (TimeoutError(), ErrorCode.TIMEOUT),
    (RuntimeError("Page.navigate() timed out after 30s"), ErrorCode.TIMEOUT),
    (RuntimeError("Navigation failed: net::ERR_NAME_NOT_RESOLVED"), ErrorCode.NOT_FOUND),
    (RuntimeError("Navigation failed: net::ERR_CERT_AUTHORITY_INVALID"), ErrorCode.EXECUTION_FAILED),
    (ConnectionError("reset"), ErrorCode.BACKEND_UNAVAILABLE),
    (AssertionError("Root CDP client not initialized"), ErrorCode.BACKEND_UNAVAILABLE),
    (ValueError("Target ABC has detached - no active sessions"), ErrorCode.STALE_ELEMENT),
    (RuntimeError("Upload failed - element 5 is not a file input."), ErrorCode.INVALID_ARGUMENT),
    (KeyError("weird"), ErrorCode.EXECUTION_FAILED),
])
def test_to_tool_error(exc: BaseException, code: ErrorCode) -> None:
    assert to_tool_error(exc, "x").code == code


def test_tool_errors_pass_through() -> None:
    original = ToolError(ErrorCode.CANCELLED, "stop")
    assert to_tool_error(original) is original


# --------------------------------------------------------------------------- provider with a fake session


def _fake_state(selector_map: dict[int, Any]) -> SimpleNamespace:
    dom = SimpleNamespace(selector_map=selector_map, llm_representation=lambda: "[5]<a />\n\tIssues")
    return SimpleNamespace(url="https://github.com/Jeomon/Windows-Use", title="Windows-Use", dom_state=dom,
                           pixels_above=0, pixels_below=1200, closed_popup_messages=["Are you sure?"],
                           state_error=None)


class FakeSession:
    def __init__(self, state: SimpleNamespace) -> None:
        self.state = state
        self.is_cdp_connected = True
        self.session_manager = object()
        self.agent_focus_target_id = "4FBDD7D717C6AF8CCF008F7A0F7BECC9"

    async def get_browser_state_summary(self, include_screenshot: bool = True) -> SimpleNamespace:
        assert include_screenshot is False
        return self.state

    async def get_tabs(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(target_id=self.agent_focus_target_id, url=self.state.url, title=self.state.title)]


@pytest.fixture
def settings(config: Config) -> BrowserSettings:
    return BrowserSettings.from_config(config)


async def test_provider_health_and_stale_index(settings: BrowserSettings) -> None:
    provider = BrowserProvider(settings)
    assert provider.unhealthy_reason() == "no session" and not provider.connected
    session = FakeSession(_fake_state({5: FakeNode("a", {"href": "/issues"}, "Issues")}))
    provider._session = session  # noqa: SLF001 - unit test injects a fake browser-use session
    assert provider.connected
    assert (await provider.element(5, 1.0)).tag_name == "a"
    with pytest.raises(ToolError) as exc:
        await provider.element(6, 1.0)
    assert exc.value.code == ErrorCode.STALE_ELEMENT
    session.is_cdp_connected = False
    assert provider.unhealthy_reason() == "CDP websocket closed"


def test_upload_never_redirects_a_text_field_to_another_input(settings: BrowserSettings) -> None:
    provider = BrowserProvider(settings)
    session = FakeSession(_fake_state({}))
    session.is_file_input = lambda node: node.attributes.get("type") == "file"
    session.find_file_input_near_element = lambda node: FakeNode("input", {"type": "file"})
    provider._session = session  # noqa: SLF001 - unit test injects a fake browser-use session
    file_input = FakeNode("input", {"type": "file"})
    assert provider.file_input_for(file_input) is file_input
    assert provider.file_input_for(FakeNode("button", {}, "Upload")).attributes["type"] == "file"
    with pytest.raises(ToolError) as exc:
        provider.file_input_for(FakeNode("input", {"type": "text"}))
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


class FakeProvider:
    """Stands in for BrowserProvider inside the real handlers (no Chrome)."""

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self.session = FakeSession(_fake_state({
            5: FakeNode("a", {"href": "/Jeomon/Windows-Use/issues"}, "Issues 3", role="link"),
            9: FakeNode("input", {"type": "password", "value": "secret-pw"}, ""),
        }))
        self.info = PageInfo(url=self.session.state.url, title=self.session.state.title, ready_state="complete")

    @asynccontextmanager
    async def attached(self, timeout_s: float, action: str = "") -> AsyncIterator[Attached]:
        yield Attached(self.session)

    async def browser_state(self, timeout_s: float) -> SimpleNamespace:
        return self.session.state

    async def tabs(self) -> tuple[list[Any], str | None]:
        return await self.session.get_tabs(), self.session.agent_focus_target_id

    async def page_text(self, timeout_s: float | None = None) -> str:
        return "Windows-Use  An AI Agent that interacts with Windows OS at GUI level."

    async def page_info(self, timeout_s: float | None = None) -> PageInfo:
        return self.info


async def _invoke(config: Config, provider: Any, tool: str, args: dict[str, Any]) -> Any:
    services = Services(config)

    async def factory() -> Any:
        return provider

    await services.get_or_create("browser", factory)
    registry = build_registry(config.path("paths.tools_dir"))
    ctx = ToolContext("r1", "unit", "s1", time.monotonic() + CALL_BUDGET_S, config, services, threading.Event())
    return await registry.invoke(ctx, tool, args)


async def test_get_state_handler_serializes_with_fakes(config: Config, settings: BrowserSettings) -> None:
    _, outcome = await _invoke(config, FakeProvider(settings), "browser.get_state", {})
    data = outcome.data
    assert data["url"] == "https://github.com/Jeomon/Windows-Use" and data["element_count"] == 2
    assert data["elements"][0] == {"index": 5, "tag": "a", "text": "Issues 3", "role": "link",
                                   "attributes": {"href": "/Jeomon/Windows-Use/issues"}}
    assert data["tabs"][0]["active"] is True and data["dialogs"] == ["Are you sure?"]
    assert "secret-pw" not in json.dumps(data)
    assert "Issues" in outcome.observation and "auto-accepted" in outcome.observation
    assert "1200px more content below" in outcome.observation


async def test_page_matches_and_wait_for_timeout_with_fakes(config: Config, settings: BrowserSettings) -> None:
    provider = FakeProvider(settings)
    _, hit = await _invoke(config, provider, "browser.page_matches",
                           {"text_contains": "ai agent", "url_contains": "windows-use"})
    assert hit.data["matches"] is True
    _, miss = await _invoke(config, provider, "browser.page_matches", {"title_contains": "nope"})
    assert miss.data["matches"] is False
    with pytest.raises(ToolError) as exc:
        await _invoke(config, provider, "browser.wait_for", {"title_contains": "nope", "timeout_ms": 300})
    assert exc.value.code == ErrorCode.TIMEOUT


async def test_status_never_launches(config: Config) -> None:
    services = Services(config)
    registry = build_registry(config.path("paths.tools_dir"))
    ctx = ToolContext("r1", "unit", "s1", time.monotonic() + CALL_BUDGET_S, config, services, threading.Event())
    _, outcome = await registry.invoke(ctx, "browser.status", {})
    assert outcome.data["connected"] is False and "ready" in outcome.data
    assert services.peek("browser") is None
