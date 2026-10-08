"""Win32 window and process helpers used by the Windows provider.

Windows-Use's `Desktop.get_windows` keeps only windows that can both minimise and maximise, which
drops most dialogs (Save As, message boxes). The agent must see dialogs, so windows are enumerated
here with `EnumWindows` and the usual Alt-Tab visibility rules (visible, not DWM-cloaked, titled,
non-empty rectangle, not a tool window).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import winreg
from ctypes import wintypes
from pathlib import Path

import psutil
import win32con
import win32gui
import win32process

from lga_worker.windows.provider import Rect, WindowInfo, WindowState

_DWMWA_CLOAKED = 14
_dwmapi = ctypes.WinDLL("dwmapi")
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    result = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), wintypes.DWORD(_DWMWA_CLOAKED), ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and cloaked.value != 0


def window_rect(hwnd: int) -> Rect:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return Rect(left, top, right, bottom)


def window_state(hwnd: int) -> WindowState:
    if win32gui.IsIconic(hwnd):
        return "minimized"
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMAXIMIZED:
        return "maximized"
    return "normal"


def window_pid(hwnd: int) -> int:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def window_info(hwnd: int, foreground: int | None = None) -> WindowInfo:
    pid = window_pid(hwnd)
    fg = win32gui.GetForegroundWindow() if foreground is None else foreground
    return WindowInfo(
        hwnd=hwnd,
        title=win32gui.GetWindowText(hwnd),
        class_name=win32gui.GetClassName(hwnd),
        pid=pid,
        process_name=process_name(pid),
        rect=window_rect(hwnd),
        state=window_state(hwnd),
        is_foreground=(hwnd == fg),
    )


def is_user_window(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd) or is_cloaked(hwnd):
        return False
    if not win32gui.GetWindowText(hwnd):
        return False
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & win32con.WS_EX_TOOLWINDOW and not ex_style & win32con.WS_EX_APPWINDOW:
        return False
    if win32gui.IsIconic(hwnd):
        return True
    return not window_rect(hwnd).is_empty()


def list_user_windows() -> list[WindowInfo]:
    """Top-level user-visible windows in Z order (foreground-most first)."""
    handles: list[int] = []

    def _collect(hwnd: int, _: object) -> bool:
        if is_user_window(hwnd):
            handles.append(hwnd)
        return True

    win32gui.EnumWindows(_collect, None)
    foreground = win32gui.GetForegroundWindow()
    windows: list[WindowInfo] = []
    for hwnd in handles:
        if not win32gui.IsWindow(hwnd):
            continue  # closed between enumeration and inspection
        windows.append(window_info(hwnd, foreground))
    return windows


def foreground_hwnd() -> int | None:
    hwnd = win32gui.GetForegroundWindow()
    return hwnd or None


def is_window(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))


def post_close(hwnd: int) -> None:
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def owned_windows(owner_hwnd: int) -> list[int]:
    """Visible windows owned by `owner_hwnd` (modal dialogs such as 'Save changes?')."""
    found: list[int] = []

    def _collect(hwnd: int, _: object) -> bool:
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER) == owner_hwnd and is_user_window(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_collect, None)
    return found


def allow_set_foreground() -> None:
    """Let our process move other windows to the foreground (ASFW_ANY)."""
    _user32.AllowSetForegroundWindow(wintypes.DWORD(0xFFFFFFFF))


# --- application resolution -------------------------------------------------------------------


def _app_paths_lookup(name: str) -> Path | None:
    exe = name if name.lower().endswith(".exe") else f"{name}.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"{_APP_PATHS_KEY}\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue  # not registered in this hive
        candidate = Path(os.path.expandvars(str(value).strip('"')))
        if candidate.is_file():
            return candidate
    return None


def resolve_executable(target: str) -> Path | None:
    """Resolve an explicit path, an App Paths registration, or a PATH lookup to an executable."""
    expanded = Path(os.path.expandvars(target.strip('"')))
    if expanded.is_file():
        return expanded
    registered = _app_paths_lookup(target)
    if registered is not None:
        return registered
    found = shutil.which(target)
    return Path(found) if found else None
