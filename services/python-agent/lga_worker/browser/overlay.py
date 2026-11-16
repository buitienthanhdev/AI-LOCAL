"""Agent overlay on the agent's own Chrome: blue frame, status pill, progress line and a Stop button.

While a task drives the browser, every page tab of OUR Chrome shows the overlay and swallows the user's
mouse/keyboard input (a full-page DOM shield plus capture-phase key/wheel listeners). The shield is lowered
only for the duration of one agent action (`agent_input()`), because CDP-dispatched input goes through the
same hit-testing as a real mouse.

This is a UX layer, NOT isolation or security: the page can read or remove the overlay, the Chrome window
frame (tabs, address bar) still takes input, and the user's own Chrome is a different process and profile.

Stop (the overlay button, exposed to the page through a CDP binding) does, in order:
1. writes `memory/tasks/<task>.cancel` - the core polls it (500 ms), cancels the in-flight worker request
   and finishes the task as cancelled;
2. cancels the in-flight browser tool calls of this worker immediately (`on_stop` callback);
3. stops loading in every tab and lowers the shield, so the browser is left idle and usable;
4. refuses every later browser call of that task (`stopped_task`).

The controller owns its own raw CDP websocket (independent of browser-use), attaches to every page target
(existing tabs, new tabs, popups) and re-injects on navigation/reload via
Page.addScriptToEvaluateOnNewDocument. If the websocket drops (Chrome crashed or was relaunched) the loop
reconnects to the current endpoint with backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lga_worker.browser import chrome as chrome_mod

logger = logging.getLogger(__name__)

BINDING = "__lgaAgentStop"
STATUS_POLL_S = 1.0
RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 5.0)
CDP_COMMAND_TIMEOUT_S = 5.0
STOPPED_LINGER_S = 8.0
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "needs_user"})
PROGRESS_CHARS = 110

# Injected into every document (top frame only). Idempotent; `window.__lgaOverlay` is its API.
OVERLAY_JS = r"""
(() => {
  if (window.top !== window) return;
  if (window.__lgaOverlay && window.__lgaOverlay.alive()) return;
  const BINDING = "__BINDING__";
  let host = null, root = null, agent = false, state = {headline: "Agent đang chạy – đã khoá điều khiển",
      progress: "Đang chuẩn bị…", stopped: false};
  const swallow = (e) => {
    if (agent || !host) return;
    // Pointer events aimed at the overlay go on to the shield / Stop button (stopped inside the shadow root).
    if (e.target === host && POINTER.includes(e.type)) return;
    e.stopImmediatePropagation(); e.preventDefault();
  };
  const POINTER = ["click", "mousedown", "mouseup", "pointerdown", "pointerup", "dblclick", "contextmenu", "auxclick"];
  const EVENTS = ["keydown", "keyup", "keypress", "beforeinput", "input", "paste", "cut", "copy", "drop",
                  "wheel", "touchstart", "touchmove", ...POINTER];
  const OPTS = {capture: true, passive: false};
  const mount = () => {
    if (host && host.isConnected) return;
    host = document.createElement("lga-agent-overlay");
    host.setAttribute("data-browser-use-exclude", "true");
    host.style.cssText = "all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    root = host.attachShadow({mode: "open"});
    root.innerHTML = `
      <style>
        .shield{position:fixed;inset:0;pointer-events:auto;cursor:not-allowed;background:transparent}
        .shield.off{pointer-events:none;cursor:auto}
        .frame{position:fixed;inset:0;pointer-events:none;border-radius:6px;
               box-shadow:inset 0 0 0 3px #3b82f6, inset 0 0 38px 8px rgba(59,130,246,.55)}
        .stopped .frame{box-shadow:inset 0 0 0 3px #9ca3af, inset 0 0 30px 6px rgba(156,163,175,.45)}
        .dock{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);display:flex;flex-direction:column;
              align-items:center;gap:10px;pointer-events:none;font:15px/1.35 "Segoe UI",system-ui,sans-serif}
        .pill{background:#2563eb;color:#fff;padding:8px 14px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.25)}
        .stopped .pill{background:#4b5563}
        .bar{display:flex;align-items:center;gap:14px;background:#1f2937;color:#e5e7eb;padding:9px 10px 9px 16px;
             border-radius:10px;min-width:240px;max-width:min(560px,90vw);box-shadow:0 4px 16px rgba(0,0,0,.3)}
        .progress{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px}
        button{all:unset;pointer-events:auto;cursor:pointer;width:30px;height:30px;border-radius:50%;
               background:#374151;display:grid;place-items:center}
        button:hover{background:#4b5563}
        button i{display:block;width:12px;height:12px;border-radius:2px;background:#ef4444}
        button[disabled]{opacity:.4;cursor:default}
      </style>
      <div class="wrap"><div class="shield"></div><div class="frame"></div>
        <div class="dock"><div class="pill"></div>
          <div class="bar"><span class="progress"></span>
            <button title="Dừng tác vụ" aria-label="Dừng tác vụ"><i></i></button></div></div></div>`;
    for (const t of POINTER) root.querySelector(".wrap").addEventListener(t, (e) => {
      e.stopPropagation();                       // the page never sees clicks on the overlay
      if (!(e.target.closest && e.target.closest("button"))) e.preventDefault();
    });
    root.querySelector("button").addEventListener("click", (e) => {
      if (state.stopped) return;
      try { window[BINDING](JSON.stringify({at: Date.now()})); } catch (_) {}
      render(Object.assign(state, {progress: "Đang dừng…", stopped: true}));
    });
    (document.documentElement || document).appendChild(host);
    render(state);
  };
  const render = (s) => {
    if (!root) return;
    root.querySelector(".wrap").classList.toggle("stopped", !!s.stopped);
    root.querySelector(".pill").textContent = s.headline;
    root.querySelector(".progress").textContent = s.progress;
    root.querySelector(".shield").classList.toggle("off", agent || !!s.stopped);
    root.querySelector("button").disabled = !!s.stopped;
  };
  for (const t of EVENTS) window.addEventListener(t, swallow, OPTS);
  const api = {
    alive: () => !!host,
    update: (s) => { Object.assign(state, s); mount(); render(state); if (state.stopped) agent = true; },
    setAgent: (on) => { agent = !!on || state.stopped; mount(); render(state); },
    remove: () => {
      for (const t of EVENTS) window.removeEventListener(t, swallow, OPTS);
      if (host) host.remove();
      host = null; root = null; delete window.__lgaOverlay;
    },
  };
  Object.defineProperty(window, "__lgaOverlay", {value: api, configurable: true});
  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount, {once: true});
})();
""".replace("__BINDING__", BINDING)


def describe_progress(record: dict[str, Any] | None) -> tuple[str, bool]:
    """(progress line, finished) for the task record JSON written by the core."""
    if not record:
        return "Đang chuẩn bị…", False
    status = str(record.get("status") or "")
    steps = record.get("steps") or []
    if status in TERMINAL_STATUSES:
        return {"completed": "Đã xong", "failed": "Tác vụ thất bại", "cancelled": "Đã dừng",
                "needs_user": "Đang chờ bạn trả lời"}[status], True
    if not steps:
        return ("Đang lập kế hoạch…" if status in ("planning", "created", "") else f"Trạng thái: {status}"), False
    last = steps[-1]
    tool = str(last.get("tool") or "")
    running = not last.get("finished_at")
    thought = " ".join(str(last.get("thought") or "").split())
    if thought.startswith("(continuing the batch)"):
        thought = thought[len("(continuing the batch)"):].strip()
    head = f"Bước {len(steps)} · {tool}" + (" · đang chạy" if running else "")
    if status == "planning" and not running:
        head = f"Bước {len(steps)} xong · đang nghĩ bước tiếp"
    line = f"{head} — {thought}" if thought else head
    return (line[:PROGRESS_CHARS - 1] + "…") if len(line) > PROGRESS_CHARS else line, False


def read_record(tasks_dir: Path, task_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((tasks_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_cancel_marker(tasks_dir: Path, task_id: str) -> Path:
    """Same marker as `agent cancel <id>` (core/rust-agent/src/cancel.rs)."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"{task_id}.cancel"
    path.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8")
    return path


class CdpConnection:
    """Minimal flat-session CDP client over the browser websocket."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read(), name="overlay-cdp-reader")

    @classmethod
    async def open(cls, cdp_url: str) -> CdpConnection:
        from websockets.asyncio.client import connect  # dependency of cdp-use, like chrome.close_gracefully

        info = await asyncio.to_thread(chrome_mod.probe_endpoint, cdp_url)
        if not info or not info.get("webSocketDebuggerUrl"):
            raise ConnectionError(f"no CDP endpoint at {cdp_url}")
        ws = await connect(info["webSocketDebuggerUrl"], max_size=None, proxy=None)
        return cls(ws)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    self.events.put_nowait(message)
        except Exception:  # noqa: BLE001 - a closed socket ends the reader; waiters are failed below
            logger.debug("overlay CDP reader stopped", exc_info=True)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("CDP websocket closed"))
            self._pending.clear()
            self.events.put_nowait({"method": "__closed__"})

    @property
    def closed(self) -> bool:
        return self._reader.done()

    async def send(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> Any:
        if self.closed:
            raise ConnectionError("CDP websocket closed")
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._next] = future
        await self._ws.send(json.dumps(message))
        reply = await asyncio.wait_for(future, CDP_COMMAND_TIMEOUT_S)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    async def close(self) -> None:
        with suppress(Exception):
            await self._ws.close()
        self._reader.cancel()
        with suppress(BaseException):
            await self._reader


@dataclass
class _Page:
    target_id: str
    session_id: str
    script_id: str = ""


class OverlayController:
    """One per BrowserProvider. `ensure(cdp_url, task_id)` before every browser call of a task."""

    def __init__(self, tasks_dir: Path, on_stop: Callable[[str], None]) -> None:
        self.tasks_dir = tasks_dir
        self._on_stop = on_stop
        self._cdp_url = ""
        self._task_id = ""
        self._loop_task: asyncio.Task[None] | None = None
        self._conn: CdpConnection | None = None
        self._pages: dict[str, _Page] = {}
        self._agent_depth = 0
        self._stopped_at: float | None = None
        self._last_payload: dict[str, Any] = {}
        self.stopped_task = ""
        self.ready = asyncio.Event()

    # ------------------------------------------------------------------ public

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def page_count(self) -> int:
        return len(self._pages)

    async def ensure(self, cdp_url: str, task_id: str) -> None:
        """Show the overlay for `task_id` on the Chrome at `cdp_url` (restarts when either changed)."""
        if not task_id or not cdp_url:
            return
        running = self._loop_task is not None and not self._loop_task.done()
        if running and cdp_url == self._cdp_url and task_id == self._task_id:
            return
        await self.deactivate()
        self._cdp_url, self._task_id = cdp_url, task_id
        self._stopped_at = None
        self.ready = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run(), name=f"overlay-{task_id}")
        with suppress(TimeoutError):
            await asyncio.wait_for(self.ready.wait(), CDP_COMMAND_TIMEOUT_S)

    async def deactivate(self) -> None:
        """Remove the overlay from every tab and release input. Safe to call repeatedly."""
        await self._teardown_pages()  # while the connection is still open
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with suppress(BaseException):
                await task
        self._task_id = ""

    @asynccontextmanager
    async def agent_input(self) -> AsyncIterator[None]:
        """Lower the shield while the agent acts (nested calls are counted)."""
        self._agent_depth += 1
        if self._agent_depth == 1:
            await self._broadcast("window.__lgaOverlay && window.__lgaOverlay.setAgent(true)")
        try:
            yield
        finally:
            self._agent_depth -= 1
            if self._agent_depth == 0:
                await self._broadcast("window.__lgaOverlay && window.__lgaOverlay.setAgent(false)")

    async def stop(self, reason: str = "overlay") -> None:
        """The Stop button: cancel the task for real and leave the browser idle."""
        task_id = self._task_id
        if not task_id or self.stopped_task == task_id:
            return
        logger.warning("Stop pressed for task %s (%s)", task_id, reason)
        self.stopped_task = task_id
        self._stopped_at = time.monotonic()
        try:
            write_cancel_marker(self.tasks_dir, task_id)
        except OSError:
            logger.exception("could not write the cancel marker for %s", task_id)
        try:
            self._on_stop(task_id)
        except Exception:  # noqa: BLE001 - stop must continue to the browser cleanup
            logger.exception("on_stop callback failed")
        for page in list(self._pages.values()):
            with suppress(Exception):
                await self._conn.send("Page.stopLoading", session_id=page.session_id)  # type: ignore[union-attr]
        await self._push({"headline": "Đã dừng tác vụ", "progress": "Đã dừng – trình duyệt đã nhả điều khiển",
                          "stopped": True})

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                self._conn = await CdpConnection.open(self._cdp_url)
                attempt = 0
                await self._serve(self._conn)
                return  # task finished: overlay removed by _serve
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Chrome crashed/relaunched: retry with backoff
                logger.info("overlay connection lost (%s); retrying", exc)
            finally:
                if self._conn is not None:
                    await self._conn.close()
                self._conn = None
                self._pages.clear()
            await asyncio.sleep(RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)])
            attempt += 1

    async def _serve(self, conn: CdpConnection) -> None:
        await conn.send("Target.setDiscoverTargets", {"discover": True})
        targets = (await conn.send("Target.getTargets")).get("targetInfos", [])
        for info in targets:
            await self._attach(conn, info)
        self.ready.set()
        next_poll = 0.0
        while True:
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + STATUS_POLL_S
                if await self._refresh_status():
                    await self._teardown_pages()
                    return
            try:
                event = await asyncio.wait_for(conn.events.get(), max(0.05, next_poll - time.monotonic()))
            except TimeoutError:
                continue
            await self._handle(conn, event)

    async def _handle(self, conn: CdpConnection, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params", {})
        if method == "__closed__":
            raise ConnectionError("CDP websocket closed")
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            await self._attach(conn, params.get("targetInfo", {}))
        elif method == "Target.targetDestroyed":
            self._pages.pop(params.get("targetId", ""), None)
        elif method == "Target.detachedFromTarget":
            for key, page in list(self._pages.items()):
                if page.session_id == params.get("sessionId"):
                    self._pages.pop(key, None)
        elif method == "Runtime.bindingCalled" and params.get("name") == BINDING:
            await self.stop("button")

    async def _attach(self, conn: CdpConnection, info: dict[str, Any]) -> None:
        target_id = info.get("targetId", "")
        if info.get("type") != "page" or not target_id or target_id in self._pages:
            return
        url = str(info.get("url") or "")
        if url.startswith(("devtools://", "chrome-extension://")):
            return
        try:
            session_id = (await conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
            page = _Page(target_id, session_id)
            self._pages[target_id] = page
            await conn.send("Runtime.enable", session_id=session_id)  # bindingCalled is a Runtime event
            await conn.send("Runtime.addBinding", {"name": BINDING}, session_id)
            await conn.send("Page.enable", session_id=session_id)
            page.script_id = (await conn.send("Page.addScriptToEvaluateOnNewDocument",
                                              {"source": OVERLAY_JS}, session_id)).get("identifier", "")
            await conn.send("Runtime.evaluate", {"expression": OVERLAY_JS}, session_id)
            if self._last_payload:
                await self._eval(page, self._update_expr(self._last_payload))
            if self._agent_depth:
                await self._eval(page, "window.__lgaOverlay && window.__lgaOverlay.setAgent(true)")
        except Exception:  # noqa: BLE001 - a tab that closed while attaching; nothing to show there
            logger.debug("overlay could not attach to %s", target_id, exc_info=True)
            self._pages.pop(target_id, None)

    async def _refresh_status(self) -> bool:
        """Push the task's progress; True when the overlay should go away."""
        if self._stopped_at is not None:
            record = await asyncio.to_thread(read_record, self.tasks_dir, self._task_id)
            finished = bool(record) and str(record.get("status")) in TERMINAL_STATUSES
            return finished or time.monotonic() - self._stopped_at > STOPPED_LINGER_S
        record = await asyncio.to_thread(read_record, self.tasks_dir, self._task_id)
        progress, finished = describe_progress(record)
        if finished:
            return True
        await self._push({"headline": "Agent đang chạy – đã khoá điều khiển", "progress": progress, "stopped": False})
        return False

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _update_expr(payload: dict[str, Any]) -> str:
        return f"window.__lgaOverlay && window.__lgaOverlay.update({json.dumps(payload, ensure_ascii=False)})"

    async def _push(self, payload: dict[str, Any]) -> None:
        if payload == self._last_payload:
            return
        self._last_payload = dict(payload)
        await self._broadcast(self._update_expr(payload))

    async def _eval(self, page: _Page, expression: str) -> None:
        if self._conn is None:
            return
        with suppress(Exception):
            await self._conn.send("Runtime.evaluate", {"expression": expression}, page.session_id)

    async def _broadcast(self, expression: str) -> None:
        pages = list(self._pages.values())
        if pages:
            await asyncio.gather(*(self._eval(page, expression) for page in pages))

    async def _teardown_pages(self) -> None:
        conn = self._conn
        for page in list(self._pages.values()):
            if conn is None or conn.closed:
                break
            with suppress(Exception):
                if page.script_id:
                    await conn.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": page.script_id},
                                    page.session_id)
                await conn.send("Runtime.evaluate",
                                {"expression": "window.__lgaOverlay && window.__lgaOverlay.remove()"}, page.session_id)
                await conn.send("Target.detachFromTarget", {"sessionId": page.session_id})
        self._pages.clear()
        self._last_payload = {}
