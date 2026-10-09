"""Worker main loop: reads protocol messages from stdin, runs tools concurrently, writes responses to stdout.

stdout is reserved for the protocol. `sys.stdout` is redirected to stderr at startup so that any
library `print()` cannot corrupt the stream.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, BinaryIO

from lga_common.config import ConfigError, load_config
from lga_common.logs import setup_logging
from lga_common.redact import Redactor

from lga_worker import __version__
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.protocol import PROTOCOL_VERSION, Cancel, Ping, ProtocolError, Request, Shutdown, encode, parse_message
from lga_worker.registry import ToolContext, ToolRegistry, load_specs
from lga_worker.services import Services

logger = logging.getLogger("lga_worker")


@dataclass
class InFlight:
    task: asyncio.Task[None]
    cancel_event: threading.Event


# Contract tools the Rust core executes itself (core/rust-agent/src/native); the worker never sees them.
CORE_NATIVE_TOOLS = {"shell.exec", "filesystem.read", "filesystem.write", "filesystem.list", "filesystem.stat",
                     "system.environment", "system.open", "knowledge.status", "knowledge.search",
                     "knowledge.get_context", "knowledge.read_document", "knowledge.query", "knowledge.update",
                     "web.search", "web.fetch", "document.read", "memory.remember", "memory.forget"}


def build_registry(tools_dir) -> ToolRegistry:
    from lga_worker.tools import desktop

    registry = ToolRegistry(load_specs(tools_dir))
    desktop.register(registry)
    # shell.exec, filesystem.*, system.* and knowledge.* run in the Rust core (core/rust-agent/src/native).
    for optional in ("browser", "vision", "autocad"):
        try:
            module = __import__(f"lga_worker.tools.{optional}", fromlist=["register"])
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("lga_worker.tools"):
                continue  # subsystem not part of this build
            raise
        module.register(registry)
    return registry


class Worker:
    def __init__(self, registry: ToolRegistry, services: Services, out: BinaryIO, redactor: Redactor) -> None:
        self._registry = registry
        self._services = services
        self._out = out
        self._redactor = redactor
        self._write_lock = threading.Lock()
        self._inflight: dict[str, InFlight] = {}

    def send(self, message: dict[str, Any]) -> None:
        data = encode(message)
        with self._write_lock:
            self._out.write(data)
            self._out.flush()

    async def handle_request(self, request: Request, cancel_event: threading.Event) -> None:
        started_unix_ms = int(time.time() * 1000)
        started = time.monotonic()
        remaining_s = (request.deadline_unix_ms - started_unix_ms) / 1000.0
        ctx = ToolContext(request.request_id, request.task_id, request.step_id, started + max(0.0, remaining_s),
                          self._services.config, self._services, cancel_event)
        backend = ""
        try:
            if remaining_s <= 0:
                raise ToolError(ErrorCode.TIMEOUT, "deadline already passed when the request arrived")
            spec, outcome = await self._registry.invoke(ctx, request.tool, request.args)
            backend = outcome.backend
            response = {
                "status": "ok",
                "result": {
                    "data": outcome.data,
                    "verification": {"status": outcome.verification_status(spec.verification),
                                     "checks": [c.__dict__ for c in outcome.checks]},
                    "artifacts": outcome.artifacts,
                    "observation": self._redactor(outcome.observation),
                },
                "error": None,
            }
        except ToolError as exc:
            response = {"status": "error", "result": None, "error": exc.to_dict()}
        except TimeoutError:
            response = {"status": "error", "result": None,
                        "error": ToolError(ErrorCode.TIMEOUT, f"{request.tool} exceeded its deadline").to_dict()}
        except asyncio.CancelledError:
            response = {"status": "error", "result": None,
                        "error": ToolError(ErrorCode.CANCELLED, "cancelled by the core").to_dict()}
        except Exception as exc:  # noqa: BLE001 - reported as INTERNAL with traceback logged, never as success
            logger.error("tool %s crashed:\n%s", request.tool, traceback.format_exc(),
                         extra={"task_id": request.task_id, "tool": request.tool})
            response = {"status": "error", "result": None,
                        "error": ToolError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}").to_dict()}
        if response["error"] is not None:
            response["error"]["message"] = self._redactor(response["error"]["message"])
        duration_ms = int((time.monotonic() - started) * 1000)
        self.send({"type": "response", "request_id": request.request_id, **response,
                   "metrics": {"started_unix_ms": started_unix_ms, "duration_ms": duration_ms, "backend": backend}})
        logger.info("%s %s in %dms", request.tool, response["status"], duration_ms,
                    extra={"task_id": request.task_id, "step_id": request.step_id, "request_id": request.request_id,
                           "tool": request.tool})

    def _start(self, request: Request) -> None:
        if request.request_id in self._inflight:
            self.send({"type": "response", "request_id": request.request_id, "status": "error", "result": None,
                       "error": ToolError(ErrorCode.CONFLICT, "duplicate request_id").to_dict(), "metrics": {}})
            return
        cancel_event = threading.Event()
        task = asyncio.create_task(self.handle_request(request, cancel_event), name=request.request_id)
        self._inflight[request.request_id] = InFlight(task, cancel_event)
        task.add_done_callback(lambda _: self._inflight.pop(request.request_id, None))

    def _cancel(self, request_id: str) -> None:
        inflight = self._inflight.get(request_id)
        if inflight is None:
            return
        inflight.cancel_event.set()
        inflight.task.cancel()

    async def run(self, lines: asyncio.Queue[bytes | None]) -> None:
        self.send({"type": "ready", "protocol": PROTOCOL_VERSION, "worker_version": __version__, "pid": os.getpid(),
                   "tools": self._registry.available_tools()})
        while True:
            line = await lines.get()
            if line is None:
                break
            if not line.strip():
                continue
            try:
                message = parse_message(line)
            except ProtocolError as exc:
                logger.error("protocol error: %s", exc)
                continue
            if isinstance(message, Request):
                self._start(message)
            elif isinstance(message, Cancel):
                self._cancel(message.request_id)
            elif isinstance(message, Ping):
                self.send({"type": "pong", "request_id": message.request_id})
            elif isinstance(message, Shutdown):
                break
        for request_id in list(self._inflight):
            self._cancel(request_id)
        if self._inflight:
            await asyncio.gather(*(f.task for f in self._inflight.values()), return_exceptions=True)
        await self._services.close()


def _stdin_reader(protocol_in: BinaryIO, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[bytes | None]) -> None:
    for line in protocol_in:
        loop.call_soon_threadsafe(queue.put_nowait, line)
    loop.call_soon_threadsafe(queue.put_nowait, None)


async def _main(protocol_in: BinaryIO, protocol_out: BinaryIO) -> None:
    config = load_config()
    redactor = Redactor(config.get("observability.redact_env"))
    setup_logging("worker", config.path("paths.logs_dir"), str(config.get("observability.log_level")), redactor)
    registry = build_registry(config.path("paths.tools_dir"))
    unbound = [t for t in registry.unbound_tools() if t not in CORE_NATIVE_TOOLS]
    if unbound:
        logger.warning("tools in the contract without a handler in this worker: %s", ", ".join(unbound))
    worker = Worker(registry, Services(config), protocol_out, redactor)
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    threading.Thread(target=_stdin_reader, args=(protocol_in, asyncio.get_running_loop(), queue), daemon=True,
                     name="stdin-reader").start()
    await worker.run(queue)


def run() -> None:
    # Diagnostics for stalls: LGA_DUMP_STACKS=<seconds> writes every thread's stack to stderr periodically.
    if os.environ.get("LGA_DUMP_STACKS", "").isdigit():
        import faulthandler

        faulthandler.dump_traceback_later(int(os.environ["LGA_DUMP_STACKS"]), repeat=True, file=sys.stderr)
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    sys.stdout = sys.stderr  # nothing but protocol messages may reach the real stdout
    # The protocol is read from a private duplicate of stdin, and stdin itself is pointed at NUL. On Windows a
    # thread blocked in ReadFile on a synchronous pipe makes every other call on that pipe's handle wait for
    # the read to finish; native libraries query the standard handles while they load (numpy/OpenBLAS did,
    # inside browser-use), which froze the event loop until the core happened to send the next message.
    protocol_in = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)  # also updates the process's STD_INPUT_HANDLE
    os.close(null_fd)
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 - lives for the whole process
    try:
        asyncio.run(_main(protocol_in, protocol_out))
    except ConfigError as exc:
        print(f"worker configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
