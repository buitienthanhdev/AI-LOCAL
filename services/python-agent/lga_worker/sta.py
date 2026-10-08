"""A dedicated single-threaded COM apartment (STA) executor.

UI Automation (Windows-Use's `uia` layer) and AutoCAD COM must be called from one STA thread that
pumps messages. Thread pools deadlock UIA (see windows_use/agent/tree/service.py:150-160), so every
call for a given subsystem is funnelled through one `StaExecutor`.

Design adapted from AutoCAD-MCP `src/autocad_mcp/com_sta.py` (MIT, (c) 2024 AutoCAD MCP Server
Contributors): CoInitializeEx(COINIT_APARTMENTTHREADED) on the owning thread and message pumping
between calls. A COM call cannot be interrupted, so when a call exceeds its timeout the thread is
abandoned (it is a daemon) and a fresh STA thread takes over subsequent calls.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from lga_worker.errors import ErrorCode, ToolError

T = TypeVar("T")
logger = logging.getLogger(__name__)

_COINIT_APARTMENTTHREADED = 0x2
_QUEUE_POLL_SECONDS = 0.05

_WorkItem = tuple[Callable[[], Any], Future[Any]] | None


class StaExecutor:
    def __init__(self, name: str, pump_messages: bool = True) -> None:
        self._name = name
        self._pump_messages = pump_messages
        self._lock = threading.Lock()
        self._queue: queue.Queue[_WorkItem] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.abandoned_threads = 0
        self.last_abandon_reason: str | None = None

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._queue = queue.Queue()
            self._thread = threading.Thread(
                target=self._run, args=(self._queue,), name=f"sta-{self._name}", daemon=True
            )
            self._thread.start()

    def _abandon_current_thread(self, reason: str) -> None:
        with self._lock:
            old_queue, self._thread = self._queue, None
            self.abandoned_threads += 1
            self.last_abandon_reason = reason
        # Tell the old thread to exit as soon as its stuck call returns, if it ever does.
        old_queue.put(None)

    def shutdown(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            work_queue = self._queue
        if thread is not None:
            work_queue.put(None)

    def _run(self, work_queue: queue.Queue[_WorkItem]) -> None:
        import pythoncom

        pythoncom.CoInitializeEx(_COINIT_APARTMENTTHREADED)
        try:
            while True:
                try:
                    item = work_queue.get(timeout=_QUEUE_POLL_SECONDS)
                except queue.Empty:
                    if self._pump_messages:
                        pythoncom.PumpWaitingMessages()
                    continue
                if item is None:
                    return
                fn, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - re-raised in the awaiting coroutine
                    future.set_exception(exc)
                if self._pump_messages:
                    pythoncom.PumpWaitingMessages()
        finally:
            pythoncom.CoUninitialize()

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        self.start()
        future: Future[T] = Future()
        self._queue.put((lambda: fn(*args, **kwargs), future))
        return future

    async def run(self, timeout_s: float, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run `fn` on the STA thread and await its result.

        On timeout the stuck thread is abandoned, a new thread serves later calls, and TIMEOUT is raised.
        """
        future = self.submit(fn, *args, **kwargs)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_s)
        except TimeoutError as exc:
            reason = f"{getattr(fn, '__qualname__', repr(fn))} exceeded {timeout_s:.1f}s"
            logger.error("%s: %s; abandoning the STA thread", self._name, reason)
            self._abandon_current_thread(reason)
            raise ToolError(ErrorCode.TIMEOUT, reason, retryable=True) from exc

    def call_sync(self, timeout_s: float, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Blocking variant for code that is not running inside the event loop (tests, doctor)."""
        future = self.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout_s)
        except TimeoutError as exc:
            reason = f"{getattr(fn, '__qualname__', repr(fn))} exceeded {timeout_s:.1f}s"
            self._abandon_current_thread(reason)
            raise ToolError(ErrorCode.TIMEOUT, reason, retryable=True) from exc
