"""Who owns the Chrome on the adapter's profile.

Chrome the adapter launched itself is *owned*: the adapter closes it on a normal shutdown. Chrome it merely
found already running on the profile (started by a person, a script or another tool) is *external* and is
never closed by the adapter.

Ownership is a small record next to the profile, written at launch and removed when Chrome is closed. It
survives an adapter restart (so a restarted adapter still knows the Chrome it launched earlier is its own)
and is only honoured while the recorded browser PID is the one actually serving the profile, so a stale
record cannot make the adapter close somebody else's browser.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

OWNER_FILE = "chrome-gemini-owner.json"
SERVICE = "chrome_gemini"


def _path(profile_dir: Path) -> Path:
    return profile_dir / OWNER_FILE


def claim(profile_dir: Path, chrome_pid: int, launched_at: float | None = None) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    record = {"service": SERVICE, "chrome_pid": chrome_pid, "launched_at": launched_at or time.time()}
    _path(profile_dir).write_text(json.dumps(record), encoding="utf-8")


def release(profile_dir: Path) -> None:
    _path(profile_dir).unlink(missing_ok=True)


def owns(profile_dir: Path, chrome_pid: int) -> bool:
    try:
        record = json.loads(_path(profile_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("service") == SERVICE and record.get("chrome_pid") == chrome_pid
