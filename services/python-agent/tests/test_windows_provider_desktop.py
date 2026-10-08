"""Real-desktop validation of WindowsUseProvider. Opens Notepad and Character Map.

Run: uv run pytest -m desktop --run-desktop -v
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lga_worker.config import Config
from lga_worker.windows.provider import ElementQuery
from lga_worker.windows.windows_use_provider import WindowsUseProvider

pytestmark = pytest.mark.desktop
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


def test_notepad_type_save_verify(provider: WindowsUseProvider, tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("", encoding="utf-8")
    launched = call(provider, provider.launch_app, "notepad.exe", [str(target)], 20.0, "hello")
    window = launched.window
    assert window.process_name.lower() == "notepad.exe"
    call(provider, provider.focus_window, window.hwnd, 5.0)

    editors = call(provider, provider.find_element,
                   ElementQuery(window_hwnd=window.hwnd, control_type="Document", max_results=1))
    if not editors:
        editors = call(provider, provider.find_element,
                       ElementQuery(window_hwnd=window.hwnd, control_type="Edit", max_results=1))
    assert editors, "Notepad text area not found through UI Automation"
    editor = editors[0]
    call(provider, provider.focus_element, editor.element_id)
    call(provider, provider.type_text, "hello world")
    content = call(provider, provider.read_element, editor.element_id)
    assert "hello world" in (content.text or content.value or "")

    call(provider, provider.hotkey, ["ctrl", "s"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and target.read_text(encoding="utf-8-sig") != "hello world":
        time.sleep(0.2)
    assert target.read_text(encoding="utf-8-sig") == "hello world"

    tree = call(provider, provider.get_ui_tree, window.hwnd, 200)
    assert tree.window is not None and tree.window.hwnd == window.hwnd
    assert tree.elements, "interactive elements expected in Notepad"

    # Close only our tab (Ctrl+W) so any other restored Notepad tabs are left untouched.
    call(provider, provider.hotkey, ["ctrl", "w"])


def test_close_app_charmap(provider: WindowsUseProvider) -> None:
    launched = call(provider, provider.launch_app, "charmap.exe", [], 20.0, None)
    result = call(provider, provider.close_app, launched.window.hwnd, False, 8.0)
    assert result.closed
    windows = call(provider, provider.list_windows)
    assert launched.window.hwnd not in {w.hwnd for w in windows}
