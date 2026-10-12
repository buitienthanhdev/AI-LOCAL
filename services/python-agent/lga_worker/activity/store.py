"""Activity store: what the person worked on, as sessions in a local SQLite file (memory/activity/activity.db).

Privacy rules, in code and not in a prompt: the file never leaves the machine; window titles that look private
(password managers, private browsing, banking, ...) are stored as "(ẩn)" with the program name only; rows older than
`RETENTION_DAYS` are deleted; tracking runs only while `enabled.flag` exists (the person turns it on and off).
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

RETENTION_DAYS = 30
IDLE_AFTER_S = 120.0
HIDDEN = "(ẩn)"
DEFAULT_HIDE = (
    r"password|mật khẩu|keepass|bitwarden|1password|lastpass|dashlane|credential|passkey",
    r"inprivate|incognito|riêng tư|private browsing",
    r"ngân hàng|banking|vietcombank|techcombank|bidv|vietinbank|mbbank|acb\b|tpbank|vpbank|momo|zalopay",
    r"đăng nhập|sign in|log in|login|otp|xác thực|verification code",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, process TEXT NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'      -- active | idle
);
CREATE INDEX IF NOT EXISTS sessions_start ON sessions(start);
"""


def activity_dir(root: Path) -> Path:
    d = root / "memory" / "activity"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(root: Path) -> Path:
    return activity_dir(root) / "activity.db"


def flag_path(root: Path) -> Path:
    return activity_dir(root) / "enabled.flag"


def pid_path(root: Path) -> Path:
    return activity_dir(root) / "tracker.pid"


def hide_patterns(root: Path) -> list[re.Pattern[str]]:
    patterns = list(DEFAULT_HIDE)
    extra = activity_dir(root) / "hide.txt"
    if extra.is_file():
        patterns += [l.strip() for l in extra.read_text("utf-8", errors="replace").splitlines() if l.strip() and not l.startswith("#")]
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue
    return out


def connect(root: Path, readonly: bool = False) -> sqlite3.Connection:
    path = db_path(root)
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=10)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def parse_since(text: str | None, now: float | None = None) -> float:
    """'today', 'yesterday', '30m', '2h', '7d' or an ISO date/time -> unix seconds."""
    now = now or time.time()
    t = (text or "today").strip().lower()
    if t in ("today", "hôm nay", "hom nay"):
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if t in ("yesterday", "hôm qua", "hom qua"):
        return parse_since("today", now) - 86400
    m = re.fullmatch(r"(\d+)\s*(m|min|h|d|w)", t)
    if m:
        return now - int(m.group(1)) * {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(t).timestamp()
    except ValueError:
        return parse_since("today", now)


def summary(root: Path, since: float, until: float | None = None, top: int = 12) -> dict[str, Any]:
    """Time per program and per window since `since`; the tree adds up to the tracked time (100 %)."""
    until = until or time.time()
    if not db_path(root).is_file():
        return {"tracked_seconds": 0, "active_seconds": 0, "idle_seconds": 0, "apps": [], "windows": [], "hours": [],
                "since": since, "until": until, "records": 0}
    conn = connect(root, readonly=True)
    rows = conn.execute(
        "SELECT process, title, state, MAX(start, ?) AS s, MIN(end, ?) AS e FROM sessions WHERE end > ? AND start < ?",
        (since, until, since, until)).fetchall()
    conn.close()
    apps: dict[str, float] = {}
    windows: dict[tuple[str, str], float] = {}
    hours: dict[int, float] = {}
    active = idle = 0.0
    for r in rows:
        dur = max(0.0, r["e"] - r["s"])
        if dur <= 0:
            continue
        if r["state"] == "idle":
            idle += dur
            continue
        active += dur
        apps[r["process"]] = apps.get(r["process"], 0.0) + dur
        windows[(r["process"], r["title"])] = windows.get((r["process"], r["title"]), 0.0) + dur
        h = int(r["s"] // 3600)
        hours[h] = hours.get(h, 0.0) + dur
    total = active + idle
    pct = lambda x, whole: round(100.0 * x / whole, 1) if whole > 0 else 0.0  # noqa: E731
    return {
        "since": since, "until": until, "records": len(rows), "tracked_seconds": round(total), "active_seconds": round(active),
        "idle_seconds": round(idle),
        "apps": [{"process": p, "seconds": round(s), "percent_of_active": pct(s, active), "percent_of_tracked": pct(s, total)}
                 for p, s in sorted(apps.items(), key=lambda kv: -kv[1])[:top]],
        "windows": [{"process": p, "title": t, "seconds": round(s)} for (p, t), s in sorted(windows.items(), key=lambda kv: -kv[1])[:top]],
        "hours": [{"hour_start": h * 3600, "seconds": round(s)} for h, s in sorted(hours.items())],
    }


def timeline(root: Path, since: float, limit: int = 60) -> list[dict[str, Any]]:
    if not db_path(root).is_file():
        return []
    conn = connect(root, readonly=True)
    rows = conn.execute("SELECT start, end, process, title, state FROM sessions WHERE end > ? ORDER BY start DESC LIMIT ?",
                        (since, int(limit))).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def purge(conn: sqlite3.Connection, now: float | None = None) -> int:
    cutoff = (now or time.time()) - RETENTION_DAYS * 86400
    n = conn.execute("DELETE FROM sessions WHERE end < ?", (cutoff,)).rowcount
    conn.commit()
    return n
