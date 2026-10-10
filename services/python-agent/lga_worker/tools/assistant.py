"""Handlers for the assistant's own helpers (contract: tools/assistant.json):

* schedule.*: run a request later or regularly ("every morning at 8, summarise ..."): a Windows scheduled task
  (folder \\LGA\\) starts `agent run` with the request at that time and shows a notification when it ends.
* notify.show: a Windows notification for the user.
* clipboard.*: read or set the clipboard text.
"""

from __future__ import annotations

import base64
import csv
import io
import re
import subprocess
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "windows"
TASK_FOLDER = "LGA"
NO_WINDOW = 0x08000000
NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,60}$")
DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _powershell(script: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(("$ProgressPreference='SilentlyContinue';" + script).encode("utf-16-le")).decode()
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def toast(title: str, message: str) -> bool:
    """A Windows notification (toast) through the WinRT API; no module needed."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode({_ps_quote(title)})) | Out-Null;"
        f"$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode({_ps_quote(message)})) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($n)"
    )
    return _powershell(script).returncode == 0


def _schedule_dir(ctx: ToolContext) -> Path:
    directory = ctx.config.root / "runtime" / "schedules"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_runner(ctx: ToolContext, name: str, request: str) -> Path:
    """A .cmd the scheduled task runs: the request through `agent run`, then a notification with the outcome."""
    agent = ctx.config.root / "target" / "release" / "agent.exe"
    if not agent.is_file():
        raise ToolError(ErrorCode.NOT_FOUND, f"{agent} is missing (build the release binaries)")
    safe = request.replace('"', "'").replace("%", "%%").replace("\r", " ").replace("\n", " ")
    runner = _schedule_dir(ctx) / f"{name}.cmd"
    notify = (
        'powershell -NoProfile -WindowStyle Hidden -Command "'
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
        f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('Trợ lý: {name}'))|Out-Null;"
        "$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode($env:LGA_RESULT))|Out-Null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($t))\""
    )
    runner.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        f'cd /d "{ctx.config.root}"\r\n'
        f'"{agent}" run --mode full_access "{safe}" > "{_schedule_dir(ctx) / (name + ".last.log")}" 2>&1\r\n'
        "if errorlevel 1 (set LGA_RESULT=Chưa xong - xem nhật ký trong cửa sổ Trợ lý) else (set LGA_RESULT=Đã làm xong)\r\n"
        f"{notify}\r\n",
        encoding="utf-8",
    )
    (_schedule_dir(ctx) / f"{name}.txt").write_text(request, encoding="utf-8")
    return runner


def _list_tasks() -> list[dict[str, str]]:
    result = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/V"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    out = []
    for row in rows:
        task_name = row.get("TaskName", "")
        if task_name.startswith(f"\\{TASK_FOLDER}\\") and row.get("TaskName") != "TaskName":
            out.append({"name": task_name.split("\\")[-1], "next_run": row.get("Next Run Time", ""),
                        "last_run": row.get("Last Run Time", ""), "last_result": row.get("Last Result", ""),
                        "schedule": f"{row.get('Schedule Type', '')} {row.get('Start Time', '')} {row.get('Days', '')}".strip(),
                        "status": row.get("Status", "")})
    unique: dict[str, dict[str, str]] = {}
    for item in out:
        unique.setdefault(item["name"], item)
    return list(unique.values())


def register(registry: ToolRegistry) -> None:
    @registry.handler("schedule.create")
    async def schedule_create(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        name = args["name"].strip()
        if not NAME_RE.match(name):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "name: letters, digits, space, - and _ only (max 60)")
        when = args["repeat"]
        runner = _write_runner(ctx, name, args["request"])
        command = ["schtasks", "/Create", "/F", "/TN", f"{TASK_FOLDER}\\{name}", "/TR", f'"{runner}"']
        start_time = args.get("time", "")
        if when == "once":
            if not args.get("date") or not start_time:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "repeat=once needs date (YYYY-MM-DD) and time (HH:MM)")
            y, m, d = args["date"].split("-")
            command += ["/SC", "ONCE", "/SD", f"{d}/{m}/{y}", "/ST", start_time]
        elif when == "daily":
            command += ["/SC", "DAILY", "/ST", start_time or "08:00"]
        elif when == "weekly":
            days = [d.lower()[:3] for d in args.get("days", [])]
            if not days or not set(days) <= DAY_NAMES:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "repeat=weekly needs days, e.g. [\"mon\", \"thu\"]")
            command += ["/SC", "WEEKLY", "/D", ",".join(d.upper() for d in days), "/ST", start_time or "08:00"]
        elif when == "every_minutes":
            minutes = int(args.get("every_minutes", 60))
            command += ["/SC", "MINUTE", "/MO", str(max(5, min(minutes, 1439)))]
        else:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"unknown repeat {when!r}")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if result.returncode != 0:
            # Day/month order of /SD follows the system locale; retry month/day.
            if when == "once" and "/SD" in command:
                i = command.index("/SD") + 1
                d, m, y = command[i].split("/")
                command[i] = f"{m}/{d}/{y}"
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            if result.returncode != 0:
                raise ToolError(ErrorCode.EXECUTION_FAILED, f"schtasks: {(result.stderr or result.stdout).strip()}")
        listed = next((t for t in _list_tasks() if t["name"] == name), None)
        return ToolOutcome(data={"name": name, "task": listed, "runner": str(runner)}, backend=BACKEND,
                           checks=[Check("task_registered", listed is not None, f"next run {listed['next_run'] if listed else '?'}")],
                           observation=f"Scheduled '{name}' ({when}); next run: {listed['next_run'] if listed else 'unknown'}. "
                           f"It runs the request with the assistant and shows a notification when done.")

    @registry.handler("schedule.list")
    async def schedule_list(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        tasks = _list_tasks()
        for task in tasks:
            request = _schedule_dir(ctx) / f"{task['name']}.txt"
            task["request"] = request.read_text(encoding="utf-8") if request.is_file() else ""
        lines = [f"- {t['name']}: {t['schedule']} | next {t['next_run']} | last {t['last_run']} ({t['last_result']}) | {t['request'][:80]}" for t in tasks]
        return ToolOutcome(data={"tasks": tasks}, backend=BACKEND,
                           observation=("Scheduled requests:\n" + "\n".join(lines)) if lines else "No scheduled requests.")

    @registry.handler("schedule.delete")
    async def schedule_delete(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        name = args["name"].strip()
        result = subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"{TASK_FOLDER}\\{name}"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        for suffix in (".cmd", ".txt"):
            (_schedule_dir(ctx) / f"{name}{suffix}").unlink(missing_ok=True)
        gone = all(t["name"] != name for t in _list_tasks())
        if result.returncode != 0 and not gone:
            raise ToolError(ErrorCode.NOT_FOUND, f"no scheduled request named {name!r}")
        return ToolOutcome(data={"name": name, "deleted": gone}, backend=BACKEND, checks=[Check("task_gone", gone, name)],
                           observation=f"Deleted the scheduled request '{name}'.")

    @registry.handler("notify.show")
    async def notify_show(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        shown = toast(args["title"], args["message"])
        return ToolOutcome(data={"shown": shown}, backend=BACKEND, checks=[Check("toast_shown", shown, "Windows accepted the notification")],
                           observation=f"Notification '{args['title']}' shown." if shown else "Windows refused the notification.")

    @registry.handler("clipboard.read")
    async def clipboard_read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else ""
        finally:
            win32clipboard.CloseClipboard()
        return ToolOutcome(data={"text": text}, backend=BACKEND,
                           observation=f"Clipboard ({len(text)} characters):\n{text[:6000]}" if text else "The clipboard holds no text.")

    @registry.handler("clipboard.write")
    async def clipboard_write(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        import win32clipboard
        import win32con

        text = args["text"]
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        win32clipboard.OpenClipboard()
        try:
            back = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return ToolOutcome(data={"chars": len(text)}, backend=BACKEND, checks=[Check("clipboard_read_back", back == text, f"{len(back)} chars")],
                           observation=f"Put {len(text)} characters on the clipboard.")
