"""Local web UI for the agent.

The UI owns no agent logic. It
* starts tasks by running `agent run --ui-channel --task-id <id> "<task>"` (the real CLI),
* controls them with `agent cancel|pause|unpause <id>`,
* reads progress from `memory/tasks/<id>.json` and the audit log `logs/tasks/<id>.jsonl`,
* answers confirmations/questions through `memory/tasks/<id>.request.json` / `.response.json`.

Security: binds to loopback only; every /api call needs the per-start token (sent by the page in the
`X-LGA-Token` header, which cross-origin pages can neither read nor send without a CORS preflight that
is refused) and a same-origin `Origin` header when one is present. Artifact downloads are limited to
files inside the artifacts directory.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from lga_common.config import Config

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_HEADER = "X-LGA-Token"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,80}$")
MAX_TASK_CHARS = 4000
MAX_EVENTS_PER_POLL = 500
VALID_MODES = {"normal", "trusted", "full_access"}
CONTROL_TIMEOUT_S = 30.0
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

CONFIG_KEY = web.AppKey("config", Config)
TOKEN_KEY = web.AppKey("token", str)
AGENT_KEY = web.AppKey("agent", Path)
ORIGINS_KEY = web.AppKey("origins", set)


def find_agent_exe(root: Path) -> Path:
    for candidate in (root / "target" / "release" / "agent.exe", root / "target" / "debug" / "agent.exe"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("agent.exe not found; run `cargo build --release` in the repository root")


def _tasks_dir(app: web.Application) -> Path:
    config = app[CONFIG_KEY]
    return config.path("memory.tasks_dir")


def _audit_path(app: web.Application, task_id: str) -> Path:
    return app[CONFIG_KEY].path("paths.logs_dir") / "tasks" / f"{task_id}.jsonl"


def _task_id(request: web.Request) -> str:
    task_id = request.match_info["task_id"]
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise web.HTTPBadRequest(text="invalid task id")
    return task_id


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@web.middleware
async def guard(request: web.Request, handler):
    if request.path.startswith("/api/"):
        token = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode(), request.app[TOKEN_KEY].encode()):
            raise web.HTTPUnauthorized(text="missing or invalid UI token")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in request.app[ORIGINS_KEY]:
            raise web.HTTPForbidden(text="cross-origin request refused")
    return await handler(request)


async def index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return web.Response(text=html.replace("__LGA_TOKEN__", request.app[TOKEN_KEY]), content_type="text/html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy":
                                 "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'"})


async def list_tasks(request: web.Request) -> web.Response:
    tasks = []
    for path in sorted(_tasks_dir(request.app).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name.endswith((".request.json", ".response.json")):
            continue
        record = _read_json(path)
        if isinstance(record, dict) and "task_id" in record:
            tasks.append({k: record.get(k) for k in ("task_id", "goal", "status", "mode", "updated_at")}
                         | {"steps": len(record.get("steps") or [])})
    return web.json_response({"tasks": tasks[:100]})


async def get_task(request: web.Request) -> web.Response:
    task_id = _task_id(request)
    record = _read_json(_tasks_dir(request.app) / f"{task_id}.json")
    if record is None:
        raise web.HTTPNotFound(text="unknown task")
    pending = _read_json(_tasks_dir(request.app) / f"{task_id}.request.json")
    paused = (_tasks_dir(request.app) / f"{task_id}.pause").exists()
    return web.json_response({"task": record, "pending_request": pending, "pause_requested": paused})


async def task_events(request: web.Request) -> web.Response:
    task_id = _task_id(request)
    after = int(request.query.get("after", "0"))
    events: list[Any] = []
    path = _audit_path(request.app, task_id)
    total = 0
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for total, line in enumerate(handle, start=1):
                if total <= after or len(events) >= MAX_EVENTS_PER_POLL:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a line being written right now; it is read on the next poll
    next_after = after + len(events)
    return web.json_response({"events": events, "next": next_after, "total": total})


async def start_task(request: web.Request) -> web.Response:
    body = await request.json()
    task = str(body.get("task") or "").strip()
    mode = str(body.get("mode") or "normal")
    if not task or len(task) > MAX_TASK_CHARS:
        raise web.HTTPBadRequest(text="task must be 1-4000 characters")
    if mode not in VALID_MODES:
        raise web.HTTPBadRequest(text="invalid mode")
    task_id = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    config = request.app[CONFIG_KEY]
    log_dir = config.path("paths.logs_dir") / "ui"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{task_id}.out.log").open("wb") as out:
        subprocess.Popen(
            [str(request.app[AGENT_KEY]), "run", "--ui-channel", "--task-id", task_id, "--mode", mode, task],
            cwd=str(config.root), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, close_fds=True,
        )
    return web.json_response({"task_id": task_id})


async def _agent_command(app: web.Application, *args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        str(app[AGENT_KEY]), *args, cwd=str(app[CONFIG_KEY].root), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=CONTROL_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        raise web.HTTPGatewayTimeout(text="agent command timed out") from None
    return process.returncode or 0, output.decode("utf-8", errors="replace").strip()


async def control(request: web.Request) -> web.Response:
    task_id = _task_id(request)
    action = request.match_info["action"]
    if action not in {"cancel", "pause", "unpause", "resume"}:
        raise web.HTTPNotFound()
    if action == "resume":
        config = request.app[CONFIG_KEY]
        log = config.path("paths.logs_dir") / "ui" / f"{task_id}.resume.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as out:
            subprocess.Popen([str(request.app[AGENT_KEY]), "resume", "--ui-channel", task_id], cwd=str(config.root),
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, close_fds=True)
        return web.json_response({"ok": True, "output": "resuming"})
    code, output = await _agent_command(request.app, action, task_id)
    return web.json_response({"ok": code == 0, "output": output}, status=200 if code == 0 else 409)


async def respond(request: web.Request) -> web.Response:
    task_id = _task_id(request)
    pending = _read_json(_tasks_dir(request.app) / f"{task_id}.request.json")
    if not isinstance(pending, dict):
        raise web.HTTPConflict(text="no pending request")
    body = await request.json()
    answer: dict[str, Any] = {"id": pending.get("id")}
    if pending.get("kind") == "confirm":
        answer["approve"] = bool(body.get("approve"))
    else:
        answer["answer"] = str(body.get("answer") or "")
    target = _tasks_dir(request.app) / f"{task_id}.response.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(answer), encoding="utf-8")
    tmp.replace(target)
    return web.json_response({"ok": True})


async def artifact(request: web.Request) -> web.FileResponse:
    root = request.app[CONFIG_KEY].path("paths.artifacts_dir").resolve()
    requested = Path(request.query.get("path", "")).resolve()
    if not requested.is_relative_to(root) or not requested.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(requested, headers={"Cache-Control": "no-store"})


def build_app(config: Config, agent_exe: Path, host: str, port: int, token: str | None = None) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[CONFIG_KEY] = config
    app[TOKEN_KEY] = token or secrets.token_urlsafe(32)
    app[AGENT_KEY] = agent_exe
    app[ORIGINS_KEY] = {f"http://{host}:{port}", f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    app.router.add_get("/", index)
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_post("/api/tasks", start_task)
    app.router.add_get("/api/tasks/{task_id}", get_task)
    app.router.add_get("/api/tasks/{task_id}/events", task_events)
    app.router.add_post("/api/tasks/{task_id}/respond", respond)
    app.router.add_post("/api/tasks/{task_id}/{action}", control)
    app.router.add_get("/api/artifact", artifact)
    return app
