"""Read-only detection of installed software (registry and file metadata; nothing is launched)."""

from __future__ import annotations

import ctypes
import os
import winreg
from pathlib import Path
from typing import Any

import psutil
import win32api

_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
_AUTOCAD_ROOT = r"SOFTWARE\Autodesk\AutoCAD"
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN, _SM_CMONITORS = 76, 77, 78, 79, 80


def file_version(path: Path) -> str | None:
    try:
        info = win32api.GetFileVersionInfo(str(path), "\\")
    except win32api.error:
        return None
    ms, ls = info["ProductVersionMS"], info["ProductVersionLS"]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _registry_default(hive: int, key: str) -> str | None:
    try:
        with winreg.OpenKey(hive, key) as handle:
            value, _ = winreg.QueryValueEx(handle, "")
            return str(value)
    except OSError:
        return None


def find_chrome(configured: str = "") -> Path | None:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        registered = _registry_default(hive, rf"{_APP_PATHS}\chrome.exe")
        if registered:
            candidates.append(registered.strip('"'))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for candidate in candidates:
        path = Path(os.path.expandvars(candidate))
        if path.is_file():
            return path
    return None


def autocad_installations() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _AUTOCAD_ROOT)
    except OSError:
        return found
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            release = winreg.EnumKey(root, i)
            with winreg.OpenKey(root, release) as release_key:
                for j in range(winreg.QueryInfoKey(release_key)[0]):
                    product = winreg.EnumKey(release_key, j)
                    try:
                        with winreg.OpenKey(release_key, product) as product_key:
                            location, _ = winreg.QueryValueEx(product_key, "AcadLocation")
                    except OSError:
                        continue  # sub-keys such as InstalledProducts have no AcadLocation
                    exe = Path(str(location)) / "acad.exe"
                    if exe.is_file():
                        found.append({"release": release, "product": product, "location": str(location),
                                      "executable": str(exe), "version": file_version(exe)})
    return found


def com_registered(prog_id: str) -> bool:
    return _registry_default(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\CLSID") is not None


def process_running(image_name: str) -> bool:
    lowered = image_name.lower()
    return any((p.info.get("name") or "").lower() == lowered for p in psutil.process_iter(["name"]))


def screen_layout() -> dict[str, int]:
    metric = ctypes.windll.user32.GetSystemMetrics
    return {"virtual_left": metric(_SM_XVIRTUALSCREEN), "virtual_top": metric(_SM_YVIRTUALSCREEN),
            "virtual_width": metric(_SM_CXVIRTUALSCREEN), "virtual_height": metric(_SM_CYVIRTUALSCREEN),
            "monitors": metric(_SM_CMONITORS)}


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())
