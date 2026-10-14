"""One adapter per Chrome profile.

Two adapters on the same profile would share one Chrome and interleave prompts in one conversation tab, which
the per-adapter transaction lock cannot prevent. The profile directory therefore carries an OS-level byte
lock held for the adapter's whole life. The OS drops it when the process ends, however it ends, so a crashed
adapter never leaves a stale lock (unlike a PID file) and its replacement can start at once.
"""

from __future__ import annotations

import msvcrt
import os
import time
from pathlib import Path

LOCK_FILE = "chrome-gemini-adapter.lock"
EXIT_CODE_PROFILE_BUSY = 3

_held: list[object] = []  # keeps the file object (and so the lock) alive for the process lifetime


class ProfileBusy(RuntimeError):
    """Another adapter already serves this profile."""


def acquire(profile_dir: Path, wait_s: float = 5.0) -> None:
    """Take the profile's adapter lock, waiting briefly for a previous adapter that is still exiting."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    handle = open(profile_dir / LOCK_FILE, "a+b")  # noqa: SIM115 - held on purpose
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ProfileBusy(f"another chrome_gemini adapter already serves {profile_dir}") from None
            time.sleep(0.1)
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode())  # informational only; the lock is the truth
    handle.flush()
    _held.append(handle)
