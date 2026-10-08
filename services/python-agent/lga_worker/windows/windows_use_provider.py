"""`WindowsControlProvider` implemented on top of Windows-Use (https://github.com/Jeomon/Windows-Use).

Reused from Windows-Use (imported, not copied): the `Desktop` primitives (`bring_window_to_top`), the
cached UIA tree builder (`Desktop.tree`), and the vendored `windows_use.uia` layer for mouse, keyboard
and UI Automation patterns.

Added here because Windows-Use lacks them or behaves unsuitably for an autonomous agent: close_app,
find_element / read_element / write_element / focus_element with stable element IDs, dialog-aware
window listing, launch with post-condition (a window must appear), exact single clicks
(`Desktop.click` defaults to a double click), modifier-safe hotkeys, PowerShell with Job Object
kill-on-timeout, and filesystem operations.

Every method runs on `self.executor` (one STA thread); see provider.py.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import win32api
import win32con
import win32gui
from fuzzywuzzy import fuzz, process
from PIL import ImageGrab

from lga_common.config import Config
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.filesystem import ops as fs_ops
from lga_worker.shell.powershell import resolve_powershell, run_powershell
from lga_worker.sta import StaExecutor
from lga_worker.windows import _upstream, win32
from lga_worker.windows.elements import CacheEntry, ElementCache, matches_query
from lga_worker.windows.provider import (
    CloseResult,
    DirectoryEntry,
    ElementContent,
    ElementLocator,
    ElementQuery,
    FileContent,
    LaunchResult,
    MouseButton,
    Rect,
    ScreenshotResult,
    ScrollDirection,
    ShellResult,
    UIElement,
    UISnapshot,
    WindowInfo,
    WindowsControlProvider,
    WriteResult,
)

logger = logging.getLogger(__name__)

# Fuzzy-match score (0-100) required to accept a Start-menu app for a requested name.
START_APP_MATCH_THRESHOLD = 80
START_APPS_CACHE_TTL_S = 300.0
WINDOW_POLL_INTERVAL_S = 0.2
# Single-instance apps hand the launch to their running instance; accept that instance's window only
# after this delay so a genuinely new window gets the chance to appear first.
EXISTING_WINDOW_GRACE_S = 1.5
KEY_EVENT_GAP_S = 0.02
WHEEL_INTERVAL_S = 0.05
MAX_ELEMENT_TEXT_CHARS = 200_000
START_APPS_QUERY_TIMEOUT_S = 30.0

_EXTRA_KEY_ALIASES: dict[str, str] = {
    "cmd": "WIN", "super": "WIN", "meta": "WIN", "windows": "WIN", "command": "WIN",
    "return": "ENTER", "esc": "ESC", "pgup": "PAGEUP", "pgdn": "PAGEDOWN",
    "backspace": "BACK", "capslock": "CAPITAL", "scrolllock": "SCROLL", "option": "ALT",
    "arrowup": "UP", "arrowdown": "DOWN", "arrowleft": "LEFT", "arrowright": "RIGHT",
    "printscreen": "SNAPSHOT", "prtsc": "SNAPSHOT", "spacebar": "SPACE",
}
_OEM_KEYS: dict[str, int] = {
    "plus": 0xBB, "=": 0xBB, "minus": 0xBD, "-": 0xBD, "comma": 0xBC, ",": 0xBC,
    "period": 0xBE, ".": 0xBE, "semicolon": 0xBA, ";": 0xBA, "slash": 0xBF, "/": 0xBF,
    "backslash": 0xDC, "\\": 0xDC, "quote": 0xDE, "'": 0xDE, "backquote": 0xC0, "`": 0xC0,
    "bracketleft": 0xDB, "[": 0xDB, "bracketright": 0xDD, "]": 0xDD,
}


@dataclass(frozen=True)
class DesktopSettings:
    settle_s: float
    focus_timeout_s: float
    ui_tree_max_elements: int
    find_max_nodes: int
    find_max_depth: int
    type_interval_s: float
    element_cache_entries: int
    shell_executable: str
    shell_max_output_bytes: int
    fs_max_write_bytes: int

    @classmethod
    def from_config(cls, config: Config) -> DesktopSettings:
        return cls(
            settle_s=config.seconds("desktop.settle_ms"),
            focus_timeout_s=config.seconds("desktop.focus_timeout_ms"),
            ui_tree_max_elements=int(config.get("desktop.ui_tree_max_elements")),
            find_max_nodes=int(config.get("desktop.find_max_nodes")),
            find_max_depth=int(config.get("desktop.find_max_depth")),
            type_interval_s=config.seconds("desktop.type_interval_ms"),
            element_cache_entries=int(config.get("desktop.element_cache_snapshots"))
            * int(config.get("desktop.ui_tree_max_elements")),
            shell_executable=str(config.get("shell.executable")),
            shell_max_output_bytes=int(config.get("shell.max_output_bytes")),
            fs_max_write_bytes=int(config.get("filesystem.max_write_bytes")),
        )


# CreateProcess fails with ERROR_ACCESS_DENIED when an enclosing job forbids CREATE_BREAKAWAY_FROM_JOB.
_ERROR_ACCESS_DENIED = 5


def _spawn_user_app(argv: list[str], cwd: Path) -> int:
    """Start an application for the user so that it outlives the agent: detached, outside the worker's
    kill-on-close job when the job permits it, and without inheriting the worker's handles."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    def spawn(extra: int) -> int:
        return subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags | extra, close_fds=True).pid

    try:
        return spawn(subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
            raise
        logger.warning("job does not allow breakaway; %s will close when the agent exits", argv[0])
        return spawn(0)


def _is_com_error(exc: BaseException) -> bool:
    import _ctypes

    return isinstance(exc, _ctypes.COMError | OSError)


class WindowsUseProvider(WindowsControlProvider):
    def __init__(self, config: Config) -> None:
        self.executor = StaExecutor("desktop")
        self._settings = DesktopSettings.from_config(config)
        self._elements = ElementCache(self._settings.element_cache_entries)
        self._snapshot_seq = 0
        self._desktop: Any = None
        self._uia: Any = None
        self._start_apps: dict[str, str] = {}
        self._start_apps_loaded_at = 0.0
        self._powershell: str | None = None

    # ------------------------------------------------------------------------------------------
    # lifecycle (runs on the STA thread)
    # ------------------------------------------------------------------------------------------
    def initialize(self) -> None:
        """Import Windows-Use (sets Per-Monitor-V2 DPI awareness) and create its Desktop."""
        if self._desktop is not None:
            return
        desktop_service, uia = _upstream.load()
        self._uia = uia
        self._desktop = desktop_service.Desktop(use_vision=False, use_annotation=False, use_accessibility=True)
        uia.GetRootControl()  # creates the UIA client on this STA thread

    @property
    def uia(self) -> Any:
        if self._uia is None:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "Windows provider is not initialized")
        return self._uia

    def _shell(self) -> str:
        if self._powershell is None:
            self._powershell = resolve_powershell(self._settings.shell_executable)
        return self._powershell

    # ------------------------------------------------------------------------------------------
    # applications and windows
    # ------------------------------------------------------------------------------------------
    def _start_menu_apps(self) -> dict[str, str]:
        if self._start_apps and time.monotonic() - self._start_apps_loaded_at < START_APPS_CACHE_TTL_S:
            return self._start_apps
        result = self.run_powershell(
            "Get-StartApps | ConvertTo-Csv -NoTypeInformation", None, START_APPS_QUERY_TIMEOUT_S
        )
        if result.exit_code != 0:
            raise ToolError(ErrorCode.EXECUTION_FAILED, "Get-StartApps failed",
                            details={"stderr": result.stderr[-2000:]})
        apps: dict[str, str] = {}
        for row in csv.DictReader(io.StringIO(result.stdout.strip())):
            name, app_id = row.get("Name"), row.get("AppID")
            if name and app_id:
                apps[name] = app_id
        self._start_apps = apps
        self._start_apps_loaded_at = time.monotonic()
        return apps

    def _match_start_app(self, target: str) -> tuple[str, str] | None:
        apps = self._start_menu_apps()
        if not apps:
            return None
        lowered = {name.lower(): name for name in apps}
        exact = lowered.get(target.lower())
        if exact:
            return exact, apps[exact]
        best = process.extractOne(target, list(apps.keys()), scorer=fuzz.token_set_ratio,
                                  score_cutoff=START_APP_MATCH_THRESHOLD)
        if best is None:
            return None
        return best[0], apps[best[0]]

    def launch_app(
        self, target: str, arguments: list[str], timeout_s: float, expect_title: str | None = None
    ) -> LaunchResult:
        before = {w.hwnd for w in win32.list_user_windows()}
        pid: int | None = None
        expected_process: str | None = None
        display_name = target
        executable = win32.resolve_executable(target)
        if executable is not None:
            resolved = str(executable)
            if executable.suffix.lower() in {".exe", ".com"}:
                pid = _spawn_user_app([str(executable), *arguments], executable.parent)
                expected_process = executable.name.lower()
            else:
                win32api.ShellExecute(0, "open", str(executable), subprocess.list2cmdline(arguments) or None,
                                      None, win32con.SW_SHOWNORMAL)
            display_name = executable.stem
        else:
            match = self._match_start_app(target)
            if match is None:
                raise ToolError(ErrorCode.NOT_FOUND,
                                f"no executable, App Paths entry or Start-menu app matches {target!r}")
            if arguments:
                raise ToolError(ErrorCode.INVALID_ARGUMENT,
                                f"{match[0]!r} is a Start-menu app; arguments cannot be passed to it")
            display_name, app_id = match
            resolved = f"shell:AppsFolder\\{app_id}"
            os.startfile(resolved)  # noqa: S606 - launching a registered Start-menu app is the purpose
        window, kind = self._wait_for_launched_window(
            before, pid, expected_process, display_name, expect_title, time.monotonic() + timeout_s
        )
        return LaunchResult(pid=pid if pid else window.pid, window=window, match_kind=kind,
                            resolved_target=resolved)

    def _wait_for_launched_window(
        self,
        before: set[int],
        pid: int | None,
        expected_process: str | None,
        display_name: str,
        expect_title: str | None,
        deadline: float,
    ) -> tuple[WindowInfo, str]:
        started = time.monotonic()

        def belongs(w: WindowInfo) -> bool:
            if expect_title and expect_title.lower() not in w.title.lower():
                return False
            if pid is not None and w.pid == pid:
                return True
            if expected_process and w.process_name.lower() == expected_process:
                return True
            if pid is None and expected_process is None:
                return (fuzz.partial_ratio(display_name.lower(), w.title.lower()) >= START_APP_MATCH_THRESHOLD
                        or fuzz.partial_ratio(display_name.lower(), w.process_name.lower())
                        >= START_APP_MATCH_THRESHOLD)
            return False

        while time.monotonic() < deadline:
            windows = win32.list_user_windows()
            fresh = [w for w in windows if w.hwnd not in before and belongs(w)]
            if fresh:
                return fresh[0], "new_window"
            if time.monotonic() - started >= EXISTING_WINDOW_GRACE_S:
                existing = [w for w in windows if w.is_foreground and belongs(w)]
                if existing:
                    return existing[0], "existing_window"
            time.sleep(WINDOW_POLL_INTERVAL_S)
        details: dict[str, Any] = {"pid": pid, "expected_process": expected_process}
        if pid is not None:
            try:
                import psutil

                details["process_running"] = psutil.pid_exists(pid)
            except OSError as exc:
                details["process_check_error"] = str(exc)
        raise ToolError(ErrorCode.TIMEOUT, f"no window for {display_name!r} appeared before the deadline",
                        details=details)

    def close_app(self, hwnd: int, force: bool, timeout_s: float) -> CloseResult:
        if not win32.is_window(hwnd):
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"window {hwnd} does not exist")
        info = win32.window_info(hwnd)
        deadline = time.monotonic() + timeout_s
        if force:
            import psutil

            try:
                proc = psutil.Process(info.pid)
                proc.terminate()
                proc.wait(timeout=timeout_s)
            except psutil.NoSuchProcess:
                pass  # already gone: the desired end state
            except psutil.AccessDenied as exc:
                raise ToolError(ErrorCode.ELEVATION_REQUIRED,
                                f"terminating {info.process_name} (pid {info.pid}) requires elevation",
                                details={"pid": info.pid}) from exc
            except psutil.TimeoutExpired as exc:
                raise ToolError(ErrorCode.TIMEOUT, f"{info.process_name} did not exit after terminate") from exc
            return CloseResult(closed=not win32.is_window(hwnd), blocking_dialog=None)
        win32.post_close(hwnd)
        while time.monotonic() < deadline:
            if not win32.is_window(hwnd):
                return CloseResult(closed=True, blocking_dialog=None)
            time.sleep(WINDOW_POLL_INTERVAL_S)
        dialog = self._find_blocking_dialog(hwnd, info.pid)
        return CloseResult(closed=False, blocking_dialog=dialog)

    def _find_blocking_dialog(self, hwnd: int, pid: int) -> WindowInfo | None:
        owned = win32.owned_windows(hwnd)
        if owned:
            return win32.window_info(owned[0])
        foreground = win32.foreground_hwnd()
        if foreground and foreground != hwnd and win32.window_pid(foreground) == pid:
            return win32.window_info(foreground)
        return None

    def focus_window(self, hwnd: int, timeout_s: float) -> WindowInfo:
        if not win32.is_window(hwnd):
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"window {hwnd} does not exist")
        win32.allow_set_foreground()
        self._desktop.bring_window_to_top(hwnd)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            foreground = win32.foreground_hwnd()
            # A modal dialog owned by the window counts: the window is active behind its dialog.
            if foreground == hwnd or (foreground and win32gui.GetWindow(foreground, win32con.GW_OWNER) == hwnd):
                return win32.window_info(hwnd)
            time.sleep(WINDOW_POLL_INTERVAL_S)
        raise ToolError(ErrorCode.EXECUTION_FAILED,
                        "Windows refused to move the window to the foreground (focus-stealing prevention)",
                        retryable=True, details={"foreground_hwnd": win32.foreground_hwnd()})

    def list_windows(self) -> list[WindowInfo]:
        return win32.list_user_windows()

    def foreground_window(self) -> WindowInfo | None:
        hwnd = win32.foreground_hwnd()
        return win32.window_info(hwnd) if hwnd else None

    # ------------------------------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------------------------------
    def _check_point(self, x: int, y: int) -> None:
        left, top, width, height = self.uia.GetVirtualScreenRect()
        if not (left <= x < left + width and top <= y < top + height):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"point ({x},{y}) is outside the virtual screen",
                            details={"virtual_screen": {"left": left, "top": top, "width": width, "height": height}})

    def click(self, x: int, y: int, button: MouseButton) -> None:
        self._check_point(x, y)
        if button == "left":
            self.uia.Click(x, y, waitTime=self._settings.settle_s)
        elif button == "right":
            self.uia.RightClick(x, y, waitTime=self._settings.settle_s)
        else:
            self.uia.MiddleClick(x, y, waitTime=self._settings.settle_s)

    def double_click(self, x: int, y: int) -> None:
        self._check_point(x, y)
        self.uia.DoubleClick(x, y, waitTime=self._settings.settle_s)

    def right_click(self, x: int, y: int) -> None:
        self.click(x, y, "right")

    def move_mouse(self, x: int, y: int) -> tuple[int, int]:
        self._check_point(x, y)
        self.uia.SetCursorPos(x, y)
        time.sleep(self._settings.settle_s)
        return self.uia.GetCursorPos()

    def drag(self, start: tuple[int, int], end: tuple[int, int], duration_s: float) -> None:
        self._check_point(*start)
        self._check_point(*end)
        self.uia.DragTo(start[0], start[1], end[0], end[1], duration=duration_s)
        time.sleep(self._settings.settle_s)

    def scroll(self, x: int | None, y: int | None, direction: ScrollDirection, amount: int) -> None:
        if x is not None and y is not None:
            self._check_point(x, y)
            self.uia.SetCursorPos(x, y)
        wheel = {"up": self.uia.WheelUp, "down": self.uia.WheelDown,
                 "left": self.uia.WheelLeft, "right": self.uia.WheelRight}[direction]
        wheel(amount, interval=WHEEL_INTERVAL_S, waitTime=self._settings.settle_s)

    def cursor_position(self) -> tuple[int, int]:
        return self.uia.GetCursorPos()

    # ------------------------------------------------------------------------------------------
    # keyboard
    # ------------------------------------------------------------------------------------------
    def _virtual_key(self, key: str) -> int:
        name = key.strip()
        if not name:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "empty key name")
        lowered = name.lower()
        if lowered in _OEM_KEYS:
            return _OEM_KEYS[lowered]
        if len(name) == 1:
            upper = name.upper()
            if upper.isascii() and upper.isalnum():
                return ord(upper)
            scan = win32api.VkKeyScan(name)
            if scan != -1:
                return scan & 0xFF
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"character {name!r} has no key on this keyboard layout")
        canonical = _EXTRA_KEY_ALIASES.get(lowered, name).upper()
        vk = self.uia.SpecialKeyNames.get(canonical)
        if vk is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"unknown key {key!r}",
                            details={"examples": ["enter", "tab", "esc", "ctrl", "alt", "shift", "win",
                                                  "f1", "up", "pagedown", "delete", "a", "1"]})
        return vk

    def type_text(self, text: str) -> None:
        if text:
            self.uia.TypeText(text, interval=self._settings.type_interval_s)
            time.sleep(self._settings.settle_s)

    def press_key(self, key: str, repeat: int) -> None:
        vk = self._virtual_key(key)
        for _ in range(repeat):
            self.uia.SendKey(vk, waitTime=KEY_EVENT_GAP_S)
        time.sleep(self._settings.settle_s)

    def hotkey(self, keys: list[str]) -> None:
        codes = [self._virtual_key(k) for k in keys]
        pressed: list[int] = []
        try:
            for vk in codes:
                self.uia.PressKey(vk, waitTime=KEY_EVENT_GAP_S)
                pressed.append(vk)
        finally:
            # Release in reverse order even if a press failed, so no modifier stays stuck.
            for vk in reversed(pressed):
                self.uia.ReleaseKey(vk, waitTime=KEY_EVENT_GAP_S)
        time.sleep(self._settings.settle_s)

    # ------------------------------------------------------------------------------------------
    # perception
    # ------------------------------------------------------------------------------------------
    def take_screenshot(self, path: Path, region: Rect | None, window_hwnd: int | None) -> ScreenshotResult:
        if window_hwnd is not None:
            if not win32.is_window(window_hwnd):
                raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"window {window_hwnd} does not exist")
            if win32gui.IsIconic(window_hwnd):
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "cannot capture a minimized window; focus it first")
            region = win32.window_rect(window_hwnd)
        image = ImageGrab.grab(all_screens=True)
        v_left, v_top, _, _ = self.uia.GetVirtualScreenRect()
        origin_x, origin_y = v_left, v_top
        if region is not None:
            box = (max(region.left - v_left, 0), max(region.top - v_top, 0),
                   min(region.right - v_left, image.width), min(region.bottom - v_top, image.height))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "screenshot region is outside the screen")
            image = image.crop(box)
            origin_x, origin_y = box[0] + v_left, box[1] + v_top
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")
        return ScreenshotResult(path=path, width=image.width, height=image.height,
                                origin_x=origin_x, origin_y=origin_y)

    def _runtime_id(self, control: Any) -> tuple[int, ...]:
        try:
            return tuple(int(v) for v in (control.GetRuntimeId() or ()))
        except Exception as exc:  # COMError when the element vanished
            if _is_com_error(exc):
                return ()
            raise

    def _to_element(
        self, control: Any, window_hwnd: int, window_title: str,
        rect: Rect | None = None, value: str | None = None, with_patterns: bool = False,
    ) -> UIElement:
        if rect is None:
            r = control.BoundingRectangle
            rect = Rect(r.left, r.top, r.right, r.bottom)
        name = control.Name or ""
        control_type = control.ControlTypeName or ""
        automation_id = control.AutomationId or ""
        class_name = control.ClassName or ""
        locator = ElementLocator(window_hwnd, name, control_type, automation_id, class_name)
        element_id = self._elements.register(control, self._runtime_id(control), locator)
        patterns: list[str] = []
        if with_patterns:
            pid = self.uia.PatternId
            for label, pattern_id in (("invoke", pid.InvokePattern), ("value", pid.ValuePattern),
                                      ("toggle", pid.TogglePattern), ("text", pid.TextPattern),
                                      ("expand_collapse", pid.ExpandCollapsePattern),
                                      ("selection_item", pid.SelectionItemPattern),
                                      ("scroll", pid.ScrollPattern)):
                if control.GetPattern(pattern_id) is not None:
                    patterns.append(label)
        return UIElement(
            element_id=element_id, name=name, control_type=control_type, automation_id=automation_id,
            class_name=class_name, rect=rect, window_hwnd=window_hwnd, window_title=window_title,
            is_enabled=bool(control.IsEnabled), is_offscreen=bool(control.IsOffscreen),
            has_focus=bool(control.HasKeyboardFocus), value=value, patterns=patterns,
        )

    def _target_window(self, window_hwnd: int | None) -> int:
        hwnd = window_hwnd or win32.foreground_hwnd()
        if not hwnd or not win32.is_window(hwnd):
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND,
                            f"window {window_hwnd} does not exist" if window_hwnd else "there is no foreground window")
        return hwnd

    def get_ui_tree(self, window_hwnd: int | None, max_elements: int) -> UISnapshot:
        hwnd = self._target_window(window_hwnd)
        info = win32.window_info(hwnd)
        tree_state = self._desktop.tree.get_state(hwnd, [])
        if not tree_state.status:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"UI Automation could not read window {hwnd}; the app may still be loading",
                            retryable=True)
        self._snapshot_seq += 1
        elements: list[UIElement] = []
        nodes = list(tree_state.interactive_nodes or [])
        for node in nodes:
            if len(elements) >= max_elements:
                break
            box = node.bounding_box
            rect = Rect(box.left, box.top, box.right, box.bottom)
            if rect.is_empty() or node.control is None:
                continue  # upstream clips off-screen / other-monitor elements to an empty box
            value = node.metadata.get("value") if node.metadata else None
            try:
                elements.append(self._to_element(node.control, hwnd, info.title, rect=rect,
                                                 value=str(value) if value not in (None, "") else None))
            except Exception as exc:
                if not _is_com_error(exc):
                    raise
                continue  # element disappeared while reading; the snapshot is still consistent
        return UISnapshot(snapshot_id=self._snapshot_seq, window=info, elements=elements,
                          truncated=len(nodes) > len(elements) and len(elements) >= max_elements)

    def find_element(self, query: ElementQuery) -> list[UIElement]:
        hwnd = self._target_window(query.window_hwnd)
        title = win32gui.GetWindowText(hwnd)
        root = self.uia.ControlFromHandle(hwnd)
        if root is None:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"UI Automation has no element for window {hwnd}")
        results: list[UIElement] = []
        frontier: list[tuple[Any, int]] = [(root, 0)]
        visited = 0
        while frontier and visited < self._settings.find_max_nodes and len(results) < query.max_results:
            control, depth = frontier.pop(0)
            visited += 1
            try:
                if matches_query(query, name=control.Name or "", control_type=control.ControlTypeName or "",
                                 automation_id=control.AutomationId or "", class_name=control.ClassName or ""):
                    if query.include_offscreen or not control.IsOffscreen:
                        results.append(self._to_element(control, hwnd, title, with_patterns=True))
                if depth < self._settings.find_max_depth:
                    frontier.extend((child, depth + 1) for child in control.GetChildren())
            except Exception as exc:
                if not _is_com_error(exc):
                    raise
                continue  # subtree vanished during the walk
        return results

    def _resolve(self, element_id: str) -> CacheEntry:
        entry = self._elements.get(element_id)
        if entry is None:
            raise ToolError(ErrorCode.STALE_ELEMENT,
                            f"unknown element id {element_id!r}; inspect the UI again to get fresh ids",
                            retryable=False)
        current = self._runtime_id(entry.control)
        if current and (not entry.runtime_id or current == entry.runtime_id):
            return entry
        locator = entry.locator
        if not win32.is_window(locator.window_hwnd):
            raise ToolError(ErrorCode.STALE_ELEMENT, f"window of element {element_id} is gone",
                            details={"locator": locator.__dict__})
        candidates = self.find_element(ElementQuery(
            window_hwnd=locator.window_hwnd, name=locator.name or None, control_type=locator.control_type or None,
            automation_id=locator.automation_id or None, class_name=locator.class_name or None,
            include_offscreen=True, max_results=2,
        ))
        if len(candidates) != 1:
            raise ToolError(ErrorCode.STALE_ELEMENT,
                            f"element {element_id} no longer exists and could not be re-identified "
                            f"({len(candidates)} candidates)",
                            details={"locator": locator.__dict__})
        fresh = self._elements.get(candidates[0].element_id)
        if fresh is None:
            raise ToolError(ErrorCode.INTERNAL, "element cache lost a freshly registered entry")
        self._elements.rebind(element_id, fresh.control, fresh.runtime_id)
        rebound = self._elements.get(element_id)
        if rebound is None:
            raise ToolError(ErrorCode.INTERNAL, "element cache lost a rebound entry")
        return rebound

    def _describe(self, entry: CacheEntry, with_patterns: bool = True) -> UIElement:
        title = win32gui.GetWindowText(entry.locator.window_hwnd) if win32.is_window(entry.locator.window_hwnd) else ""
        element = self._to_element(entry.control, entry.locator.window_hwnd, title, with_patterns=with_patterns)
        if element.element_id != entry.element_id:
            element.element_id = entry.element_id
        return element

    def describe_element(self, element_id: str) -> UIElement:
        return self._describe(self._resolve(element_id))

    def element_center(self, element_id: str) -> tuple[int, int]:
        entry = self._resolve(element_id)
        r = entry.control.BoundingRectangle
        rect = Rect(r.left, r.top, r.right, r.bottom)
        if rect.is_empty() or entry.control.IsOffscreen:
            raise ToolError(ErrorCode.ELEMENT_NOT_FOUND,
                            f"element {element_id} is not visible on screen (scroll or focus its window)",
                            retryable=True)
        return rect.center

    def focus_element(self, element_id: str) -> UIElement:
        entry = self._resolve(element_id)
        if not entry.control.SetFocus():
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"element {element_id} refused keyboard focus", retryable=True)
        time.sleep(self._settings.settle_s)
        return self._describe(entry)

    def read_element(self, element_id: str) -> ElementContent:
        entry = self._resolve(element_id)
        control = entry.control
        pid = self.uia.PatternId
        value: str | None = None
        read_only: bool | None = None
        value_pattern = control.GetPattern(pid.ValuePattern)
        if value_pattern is not None:
            value = value_pattern.Value
            read_only = bool(value_pattern.IsReadOnly)
        else:
            legacy = control.GetPattern(pid.LegacyIAccessiblePattern)
            if legacy is not None and legacy.Value:
                value = legacy.Value
        text: str | None = None
        text_pattern = control.GetPattern(pid.TextPattern)
        if text_pattern is not None:
            text = text_pattern.DocumentRange.GetText(MAX_ELEMENT_TEXT_CHARS)
        toggle: str | None = None
        toggle_pattern = control.GetPattern(pid.TogglePattern)
        if toggle_pattern is not None:
            toggle = {0: "off", 1: "on", 2: "indeterminate"}.get(int(toggle_pattern.ToggleState), "unknown")
        return ElementContent(element=self._describe(entry), value=value, text=text,
                              toggle_state=toggle, is_read_only=read_only)

    def write_element(self, element_id: str, value: str) -> ElementContent:
        entry = self._resolve(element_id)
        pattern = entry.control.GetPattern(self.uia.PatternId.ValuePattern)
        if pattern is None:
            raise ToolError(ErrorCode.UNSUPPORTED,
                            f"element {element_id} does not support setting a value; click it and type instead")
        if pattern.IsReadOnly:
            raise ToolError(ErrorCode.UNSUPPORTED, f"element {element_id} is read-only")
        pattern.SetValue(value, waitTime=self._settings.settle_s)
        return self.read_element(element_id)

    # ------------------------------------------------------------------------------------------
    # shell and filesystem
    # ------------------------------------------------------------------------------------------
    def run_powershell(self, script: str, cwd: Path | None, timeout_s: float,
                       cancel_event: threading.Event | None = None) -> ShellResult:
        if cwd is not None and not cwd.is_dir():
            raise ToolError(ErrorCode.NOT_FOUND, f"working directory {cwd} does not exist")
        return run_powershell(script, executable=self._shell(), cwd=cwd, timeout_s=timeout_s,
                              max_output_bytes=self._settings.shell_max_output_bytes, cancel_event=cancel_event)

    def read_file(self, path: Path, max_bytes: int, encoding: str | None) -> FileContent:
        return fs_ops.read_file(path, max_bytes, encoding)

    def write_file(self, path: Path, content: str, encoding: str, overwrite: bool, create_dirs: bool) -> WriteResult:
        return fs_ops.write_file(path, content, encoding, overwrite, create_dirs, self._settings.fs_max_write_bytes)

    def list_directory(self, path: Path, max_entries: int) -> tuple[list[DirectoryEntry], bool]:
        return fs_ops.list_directory(path, max_entries)
