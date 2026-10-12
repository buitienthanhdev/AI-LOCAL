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


def test_observe_texts_and_focused_typing(provider: WindowsUseProvider, tmp_path: Path) -> None:
    """The observation after an action: working window, text on screen, focused control; typing without an
    element id is verified against the focused control; a deliberate close is no surprise."""
    title = f"LGA observe {uuid.uuid4().hex[:8]}"
    launched = call(provider, provider.launch_app, "powershell.exe",
                    [*FIXTURE_ARGS, str(FIXTURE_APP), "-Title", title, "-SavePath", str(tmp_path / "x.txt")], 30.0, title)
    window = launched.window
    editors = call(provider, provider.find_element, ElementQuery(window_hwnd=window.hwnd, control_type="Edit", max_results=1))
    call(provider, provider.focus_element, editors[0].element_id)
    call(provider, provider.type_text, "quan sat")

    focused = call(provider, provider.focused_control_info)
    assert focused and "quan sat" in (focused.get("value") or focused.get("text") or "")
    texts = call(provider, provider.window_texts, window.hwnd, 40, 2000, 2.0)
    assert any("quan sat" in t for t in texts), texts
    assert any("Status" in t for t in texts), texts

    seen = call(provider, provider.observe, tmp_path / "obs.png", 60)
    assert seen["working"]["hwnd"] == window.hwnd
    assert seen["surprise"] is None, seen["surprise"]
    assert seen["screenshot"] is not None and seen["screenshot"].path.is_file()
    assert any("quan sat" in t for t in seen["texts"])

    tree = call(provider, provider.get_ui_tree, window.hwnd, 200)
    assert any("focused" in e.state for e in tree.elements), [e.state for e in tree.elements]

    result = call(provider, provider.close_dialog_aware, window.hwnd, False, False, 8.0)
    assert result.closed
    seen = call(provider, provider.observe, tmp_path / "obs2.png", 20)
    assert seen["working"] is None and seen["surprise"] is None  # closed on purpose: nothing to flag


def test_save_as_and_discard_in_notepad(provider: WindowsUseProvider, tmp_path: Path) -> None:
    """Windows 11 Notepad: save through its Save As dialog, then a close that must discard unsaved text.
    Opens its own Notepad window only."""
    target = tmp_path / "ghi.txt"
    launched = call(provider, provider.launch_app, "notepad.exe", [], 30.0, None)
    window = launched.window
    assert "untitled" in window.title.lower(), launched.note
    call(provider, provider.type_text, "xin chào\nhai")
    saved = call(provider, provider.save_as, window.hwnd, target, False, 30.0)
    assert saved["size"] > 0 and target.read_text(encoding="utf-8-sig").splitlines() == ["xin chào", "hai"]
    with pytest.raises(Exception, match="already exists"):
        call(provider, provider.save_as, window.hwnd, target, False, 30.0)
    call(provider, provider.type_text, "\nba")
    result = call(provider, provider.close_dialog_aware, window.hwnd, False, False, 4.0)
    if not result.closed:  # Notepad asked "Save changes?" (with session restore off); throw the change away
        assert any("save" in b.lower() or "lưu" in b.lower() for b in result.dialog_buttons), result.dialog_buttons
        result = call(provider, provider.close_dialog_aware, window.hwnd, False, True, 8.0)
    assert result.closed
    assert target.read_text(encoding="utf-8-sig").splitlines() == ["xin chào", "hai"]
