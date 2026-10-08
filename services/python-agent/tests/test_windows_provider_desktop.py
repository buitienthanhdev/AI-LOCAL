"""Real-desktop validation of WindowsUseProvider. Uses a private fixture window and Character Map.

Run: uv run pytest -m desktop --run-desktop -v
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from lga_common.config import Config
from lga_worker.windows.provider import ElementQuery
from lga_worker.windows.windows_use_provider import WindowsUseProvider

pytestmark = pytest.mark.desktop
FIXTURE_APP = Path(__file__).parent / "fixtures" / "test_app.ps1"
FIXTURE_ARGS = ["-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File"]
TIMEOUT = 60.0


@pytest.fixture(scope="module")
def provider(config: Config) -> Iterator[WindowsUseProvider]:
    p = WindowsUseProvider(config)
    p.executor.call_sync(TIMEOUT, p.initialize)
    yield p
    p.executor.shutdown()


def call(p: WindowsUseProvider, fn, *args, **kwargs):
    return p.executor.call_sync(TIMEOUT, fn, *args, **kwargs)


def test_list_windows_and_cursor(provider: WindowsUseProvider) -> None:
    windows = call(provider, provider.list_windows)
    assert all(w.title for w in windows)
    x, y = call(provider, provider.move_mouse, 200, 200)
    assert (x, y) == (200, 200)


def test_screenshot(provider: WindowsUseProvider, tmp_path: Path) -> None:
    shot = call(provider, provider.take_screenshot, tmp_path / "s.png", None, None)
    assert shot.path.is_file() and shot.width > 0 and shot.height > 0


def test_type_save_verify_and_close(provider: WindowsUseProvider, tmp_path: Path) -> None:
    """Uses a private fixture window (tests/fixtures/test_app.py), never the user's own applications."""
    title = f"LGA test {uuid.uuid4().hex[:8]}"
    target = tmp_path / "saved.txt"
    launched = call(provider, provider.launch_app, "powershell.exe",
                    [*FIXTURE_ARGS, str(FIXTURE_APP), "-Title", title, "-SavePath", str(target)], 30.0, title)
    window = launched.window
    call(provider, provider.focus_window, window.hwnd, 5.0)

    editors = call(provider, provider.find_element, ElementQuery(window_hwnd=window.hwnd, control_type="Edit", max_results=1))
    assert editors, "fixture text field not found through UI Automation"
    editor = editors[0]
    call(provider, provider.focus_element, editor.element_id)
    call(provider, provider.type_text, "hello world")
    content = call(provider, provider.read_element, editor.element_id)
    assert "hello world" in (content.value or content.text or "")

    call(provider, provider.hotkey, ["ctrl", "s"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (target.exists() and target.read_text(encoding="utf-8") == "hello world"):
        time.sleep(0.2)
    assert target.read_text(encoding="utf-8") == "hello world"

    tree = call(provider, provider.get_ui_tree, window.hwnd, 200)
    assert tree.window is not None and tree.window.hwnd == window.hwnd
    assert tree.elements, "interactive elements expected in the fixture window"

    result = call(provider, provider.close_app, window.hwnd, False, 8.0)
    assert result.closed


def test_close_app_charmap(provider: WindowsUseProvider) -> None:
    launched = call(provider, provider.launch_app, "charmap.exe", [], 20.0, None)
    result = call(provider, provider.close_app, launched.window.hwnd, False, 8.0)
    assert result.closed
    windows = call(provider, provider.list_windows)
    assert launched.window.hwnd not in {w.hwnd for w in windows}
