"""Handlers for the activity.* tools (contract: tools/activity.json): what the person is doing, live and over time.

Tracking is opt-in and local (see lga_worker/activity/store.py for the privacy rules). `activity.now` works without
the tracker: it reads the window in front right now. The rest answer from the sessions the tracker recorded.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lga_worker.activity import store, tracker
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "activity"
DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW


def _root(ctx: ToolContext) -> Path:
    return ctx.config.root


def _running(root: Path) -> int | None:
    path = store.pid_path(root)
    if not path.is_file():
        return None
    try:
        import psutil

        pid = int(path.read_text("utf-8").strip())
        proc = psutil.Process(pid)
        if "python" in proc.name().lower() and any("lga_worker.activity" in a for a in proc.cmdline()):
            return pid
    except Exception:  # noqa: BLE001
        return None
    return None


def start_tracker(root: Path) -> dict[str, Any]:
    store.flag_path(root).write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    pid = _running(root)
    if pid:
        return {"running": True, "pid": pid, "already": True}
    env = {**os.environ, "LGA_ROOT": str(root), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "lga_worker.activity"], cwd=str(root / "services" / "python-agent"), env=env,
                            creationflags=DETACHED, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True)
    time.sleep(0.8)
    return {"running": proc.poll() is None, "pid": proc.pid, "already": False}


def stop_tracker(root: Path) -> dict[str, Any]:
    try:
        store.flag_path(root).unlink()
    except OSError:
        pass
    pid = _running(root)
    if pid:
        deadline = time.time() + 8
        while time.time() < deadline and _running(root):
            time.sleep(0.3)  # the tracker leaves by itself once the flag is gone
        if _running(root):
            try:
                import psutil

                psutil.Process(pid).terminate()
            except Exception:  # noqa: BLE001
                pass
    return {"running": False}


def _fmt(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h} giờ {m} phút" if h else f"{m} phút" if m else f"{s} giây"


def register(registry: ToolRegistry) -> None:
    @registry.handler("activity.status")
    async def status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        root = _root(ctx)
        pid = _running(root)
        db = store.db_path(root)
        first = last = None
        n = 0
        if db.is_file():
            conn = store.connect(root, readonly=True)
            row = conn.execute("SELECT MIN(start) a, MAX(end) b, COUNT(*) n FROM sessions").fetchone()
            conn.close()
            first, last, n = row["a"], row["b"], row["n"]
        enabled = store.flag_path(root).is_file()
        data = {"enabled": enabled, "running": pid is not None, "pid": pid, "sessions": n, "first": first, "last": last,
                "retention_days": store.RETENTION_DAYS}
        return ToolOutcome(data=data, observation=(
            f"Theo dõi công việc: {'đang bật' if enabled and pid else 'đang tắt'}; {n} phiên"
            + (f", từ {time.strftime('%d/%m %H:%M', time.localtime(first))}" if first else "") + "."), backend=BACKEND)

    @registry.handler("activity.control")
    async def control(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        root = _root(ctx)
        action = args["action"]
        result = start_tracker(root) if action == "start" else stop_tracker(root)
        ok = result["running"] if action == "start" else not _running(root)
        return ToolOutcome(
            data={"action": action, **result}, checks=[Check("tracker_state", ok, f"action {action}")],
            observation=("Đã bật theo dõi công việc (chỉ ghi chương trình và tiêu đề cửa sổ đang mở, lưu trên máy này, giữ "
                         f"{store.RETENTION_DAYS} ngày)." if action == "start" else "Đã tắt theo dõi công việc."), backend=BACKEND)

    @registry.handler("activity.now")
    async def now(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        root = _root(ctx)
        fg = tracker.foreground()
        idle = tracker.idle_seconds()
        hide = store.hide_patterns(root)
        process, title = (fg or ("", ""))
        shown = tracker.normalise(process, title, hide) if fg else ""
        data: dict[str, Any] = {"process": process, "title": shown, "idle_seconds": round(idle), "idle": idle >= store.IDLE_AFTER_S,
                                "tracking": store.flag_path(root).is_file()}
        if args.get("screenshot") and shown != store.HIDDEN:
            from PIL import ImageGrab

            out = ctx.artifact_dir("activity") / f"now-{int(time.time() * 1000)}.png"
            ImageGrab.grab(all_screens=True).save(out)
            data["screenshot"] = str(out)
        recent = store.summary(root, time.time() - 600) if store.db_path(root).is_file() else None
        if recent and recent["apps"]:
            data["last_10_minutes"] = recent["apps"][:5]
        text = (f"Đang dùng {process or 'không có cửa sổ'}" + (f" — “{shown}”" if shown and shown != store.HIDDEN else "")
                + (f"; đã nghỉ {round(idle / 60)} phút" if data["idle"] else ""))
        return ToolOutcome(data=data, observation=text + ".", artifacts=[{"kind": "screenshot", "path": data["screenshot"]}]
                           if "screenshot" in data else [], backend=BACKEND)

    @registry.handler("activity.summary")
    async def summary(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        root = _root(ctx)
        since = store.parse_since(args.get("since"))
        until = store.parse_since(args["until"]) if args.get("until") else None
        s = store.summary(root, since, until, top=int(args.get("top") or 12))
        if s["records"] == 0:
            enabled = store.flag_path(root).is_file()
            raise ToolError(ErrorCode.NOT_FOUND, "chưa có dữ liệu theo dõi trong khoảng này"
                            + ("" if enabled else "; theo dõi đang tắt (activity.control start để bật)"), retryable=False)
        lines = [f"{a['process']}: {_fmt(a['seconds'])} ({a['percent_of_active']}% thời gian làm việc)" for a in s["apps"][:8]]
        text = (f"Từ {time.strftime('%d/%m %H:%M', time.localtime(s['since']))}: làm việc {_fmt(s['active_seconds'])}, "
                f"nghỉ {_fmt(s['idle_seconds'])}. " + "; ".join(lines))
        return ToolOutcome(data=s, observation=text, backend=BACKEND)

    @registry.handler("activity.timeline")
    async def timeline(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        root = _root(ctx)
        since = store.parse_since(args.get("since") or "2h")
        rows = store.timeline(root, since, int(args.get("limit") or 40))
        if not rows:
            raise ToolError(ErrorCode.NOT_FOUND, "chưa có dữ liệu theo dõi trong khoảng này", retryable=False)
        merged: list[dict[str, Any]] = []
        for r in rows:
            if r["end"] - r["start"] < 5:
                continue
            merged.append({"from": time.strftime("%H:%M", time.localtime(r["start"])), "to": time.strftime("%H:%M", time.localtime(r["end"])),
                           "process": r["process"], "title": r["title"], "state": r["state"], "seconds": round(r["end"] - r["start"])})
        text = "; ".join(f"{m['from']}–{m['to']} {m['process']}" + (f" ({m['title'][:50]})" if m["title"] else "") for m in merged[-15:])
        return ToolOutcome(data={"sessions": merged}, observation=text, backend=BACKEND)
