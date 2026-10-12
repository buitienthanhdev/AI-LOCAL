"""The tracker process: samples the foreground window every few seconds and keeps sessions.

Runs as `python -m lga_worker.activity` (detached, hidden). It stops by itself when `enabled.flag` disappears, so
turning tracking off never needs a kill. Cheap: no screenshots, no keystrokes, no clipboard; only which program and
which window title is in front, and whether the person is idle (no input for 2 minutes).
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from lga_worker.activity import store

SAMPLE_S = 3.0
FLUSH_EVERY_S = 6.0
PURGE_EVERY_S = 6 * 3600.0


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    info = _LastInput()
    info.cbSize = ctypes.sizeof(_LastInput)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


def foreground() -> tuple[str, str] | None:
    """(process name, window title) of the window in front, None when nothing usable is (lock screen, desktop)."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil

        name = psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001 - a process that vanished or is protected
        name = f"pid{pid.value}"
    return name, buf.value


def normalise(process: str, title: str, hide: list) -> str:
    t = " ".join(title.split())[:200]
    probe = f"{process} {t}"
    if any(p.search(probe) for p in hide):
        return store.HIDDEN
    return t


def run(root: Path, sample_s: float = SAMPLE_S) -> None:
    flag = store.flag_path(root)
    pidfile = store.pid_path(root)
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    conn = store.connect(root)
    hide = store.hide_patterns(root)
    current: dict | None = None  # {"id", "process", "title", "state"}
    last_flush = last_purge = 0.0
    store.purge(conn)
    try:
        while flag.is_file():
            now = time.time()
            fg = foreground()
            idle = idle_seconds()
            state = "idle" if idle >= store.IDLE_AFTER_S else "active"
            if fg is None:
                process, title = "(không có cửa sổ)", ""
                state = "idle"
            else:
                process, title = fg[0], normalise(fg[0], fg[1], hide)
            if state == "idle":
                process, title = "(nghỉ)", ""
            key = (process, title, state)
            if current is None or (current["process"], current["title"], current["state"]) != key:
                cur = conn.execute("INSERT INTO sessions(start, end, process, title, state) VALUES (?, ?, ?, ?, ?)",
                                   (now - (idle if state == "idle" and idle < 3600 else 0), now, process, title, state))
                current = {"id": cur.lastrowid, "process": process, "title": title, "state": state}
                conn.commit()
                last_flush = now
            elif now - last_flush >= FLUSH_EVERY_S:
                conn.execute("UPDATE sessions SET end = ? WHERE id = ?", (now, current["id"]))
                conn.commit()
                last_flush = now
            if now - last_purge >= PURGE_EVERY_S:
                store.purge(conn)
                last_purge = now
            time.sleep(sample_s)
    finally:
        if current is not None:
            try:
                conn.execute("UPDATE sessions SET end = ? WHERE id = ?", (time.time(), current["id"]))
                conn.commit()
            except Exception:  # noqa: BLE001
                pass
        conn.close()
        try:
            pidfile.unlink()
        except OSError:
            pass


def main(argv: list[str]) -> int:
    root = Path(os.environ.get("LGA_ROOT") or Path(__file__).resolve().parents[4])
    if not store.flag_path(root).is_file():
        return 0
    run(root)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
