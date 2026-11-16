"""Host side of the agent VM: a client of the guest worker and the router that sends GUI tools there.

With `[vm] enabled = true`, tool calls whose name matches `[vm] tools` (desktop.*, paint.*, ...) are not run on
the user's desktop: the host worker forwards the request to the worker inside the VM and returns its reply.
The core (Rust) does not know the difference; cancellation, deadlines and errors keep their meaning.

Failure handling
* Not connected / handshake failed -> BACKEND_UNAVAILABLE (retryable) naming the cause; the next call retries
  the connection (bounded backoff), so a VM that was paused, restored or rebooted is picked up again.
* Connection lost during a call -> BACKEND_UNAVAILABLE, retryable, telling the planner the action may or may
  not have happened (observe first). The guest cancels everything it was doing when the host disappears.
* Cancel (Stop) -> `cancel` is forwarded and the guest's CANCELLED reply awaited (bounded), so no action of the
  cancelled task is still running in the VM when the core moves on.
* Deadline -> the guest enforces the same absolute deadline; the host adds a grace period, then gives up with
  TIMEOUT and cancels the guest call.

Files: path arguments that point into the host VM workspace are copied into the guest workspace (audited)
and rewritten; any other host path is refused - the VM never sees host drives. Artifacts the guest produced
(screenshots, saved files) are fetched back into the host artifacts directory and the paths rewritten.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import itertools
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lga_common.config import Config

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.protocol import Request
from lga_worker.remote.channel import ChannelError, SecureChannel, load_key
from lga_worker.remote.transport import Endpoint, connect
from lga_worker.remote.workspace import Workspace, pack, unpack

logger = logging.getLogger(__name__)

DEADLINE_GRACE_S = 5.0
CANCEL_ACK_TIMEOUT_S = 5.0
RECONNECT_BACKOFF_S = (0.0, 1.0, 2.0, 5.0, 10.0)
FILE_OP_TIMEOUT_S = 60.0
PATH_KEYS = frozenset({"path", "image_path", "svg_path", "output_path", "file", "file_path", "target_path"})
GUEST_WORKSPACE_PREFIX = "vm-workspace:"


@dataclass
class VmSettings:
    enabled: bool
    endpoint: str
    key_file: Path
    tools: list[str]
    host_workspace: Path
    host_artifacts: Path
    audit_path: Path

    @classmethod
    def from_config(cls, config: Config) -> VmSettings:
        vm = config.section("vm")
        return cls(
            enabled=bool(vm.get("enabled", False)),
            endpoint=str(vm.get("endpoint", "")),
            key_file=config.root / str(vm.get("key_file", "runtime/vm/agent-vm.key")),
            tools=[str(t) for t in vm.get("tools", [])],
            host_workspace=config.root / str(vm.get("host_workspace", "runtime/vm-workspace")),
            host_artifacts=config.path("paths.artifacts_dir") / "vm",
            audit_path=config.path("paths.logs_dir") / "vm" / "host-audit.jsonl",
        )


@dataclass
class _Pending:
    future: asyncio.Future[dict[str, Any]]
    kind: str = "response"


@dataclass
class RemoteWorker:
    """One authenticated connection to the guest worker, re-established on demand."""

    endpoint: Endpoint
    key_file: Path
    _channel: SecureChannel | None = None
    _reader: asyncio.Task[None] | None = None
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _failures: int = 0
    _next_try: float = 0.0
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    tools: list[str] = field(default_factory=list)
    last_error: str = ""

    @property
    def connected(self) -> bool:
        return self._channel is not None and self._reader is not None and not self._reader.done()

    async def ensure(self) -> SecureChannel:
        async with self._lock:
            if self.connected:
                return self._channel  # type: ignore[return-value]
            wait = self._next_try - time.monotonic()
            if wait > 0:
                raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                                f"agent VM unreachable ({self.last_error}); retrying in {wait:.0f} s", retryable=True)
            try:
                reader, writer = await connect(self.endpoint)
                channel = SecureChannel(reader, writer, "host")
                await channel.handshake(load_key(self.key_file))
                ready = await asyncio.wait_for(channel.recv(), 30)
                if ready.get("type") != "ready":
                    raise ChannelError(f"guest sent {ready.get('type')!r} instead of ready")
            except (OSError, TimeoutError, ChannelError) as exc:
                self._failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._next_try = time.monotonic() + RECONNECT_BACKOFF_S[
                    min(self._failures, len(RECONNECT_BACKOFF_S) - 1)]
                raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                                f"cannot reach the agent VM at {self.endpoint}: {self.last_error}. Is the VM running "
                                "and the guest worker started?", retryable=True) from exc
            self._failures, self.last_error = 0, ""
            self.tools = list(ready.get("tools") or [])
            self._channel = channel
            self._reader = asyncio.create_task(self._read(channel), name="vm-reader")
            logger.info("connected to the agent VM (%s, %d tools)", self.endpoint, len(self.tools))
            return channel

    async def _read(self, channel: SecureChannel) -> None:
        try:
            while True:
                body = await channel.recv()
                pending = self._pending.pop(str(body.get("request_id") or ""), None)
                if pending is not None and not pending.future.done():
                    pending.future.set_result(body)
        except ChannelError as exc:
            self.last_error = str(exc)
            logger.warning("agent VM connection lost: %s", exc)
        finally:
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(ToolError(
                        ErrorCode.BACKEND_UNAVAILABLE,
                        "lost the connection to the agent VM during this action; it may or may not have happened "
                        "- observe the VM screen before retrying", retryable=True))
            self._pending.clear()
            self._channel = None
            await channel.close()

    async def _exchange(self, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        channel = await self.ensure()
        rid = str(body["request_id"])
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(future)
        try:
            await channel.send(body)
        except ChannelError as exc:
            self._pending.pop(rid, None)
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"agent VM send failed: {exc}", retryable=True) from exc
        return await asyncio.wait_for(asyncio.shield(future), timeout_s)

    async def call(self, request: Request, timeout_s: float) -> dict[str, Any]:
        body = {"type": "request", "request_id": request.request_id, "task_id": request.task_id,
                "step_id": request.step_id, "tool": request.tool, "args": request.args,
                "deadline_unix_ms": request.deadline_unix_ms}
        try:
            return await self._exchange(body, timeout_s + DEADLINE_GRACE_S)
        except TimeoutError:
            await self.cancel(request.request_id, wait=False)
            raise ToolError(ErrorCode.TIMEOUT, f"{request.tool} in the agent VM exceeded its deadline") from None
        except asyncio.CancelledError:
            await asyncio.shield(self.cancel(request.request_id, wait=True))
            raise

    async def cancel(self, request_id: str, wait: bool) -> None:
        """Forward a cancel; with `wait`, block until the guest confirms the call ended (bounded)."""
        pending = self._pending.get(request_id)
        channel = self._channel
        if channel is None:
            return
        try:
            await channel.send({"type": "cancel", "request_id": request_id})
            if wait and pending is not None:
                await asyncio.wait_for(asyncio.shield(pending.future), CANCEL_ACK_TIMEOUT_S)
        except (ChannelError, ToolError, TimeoutError) as exc:
            logger.warning("cancel of %s in the VM not confirmed: %s", request_id, exc)

    async def put_file(self, rel: str, data: bytes, task_id: str) -> str:
        reply = await self._exchange({"type": "put_file", "request_id": f"f{next(self._ids)}", "path": rel,
                                      "data": pack(data), "sha256": hashlib.sha256(data).hexdigest(),
                                      "task_id": task_id}, FILE_OP_TIMEOUT_S)
        if reply.get("type") != "file_ok":
            error = reply.get("error") or {}
            raise ToolError(ErrorCode.POLICY_DENIED if error.get("code") == "POLICY_DENIED"
                            else ErrorCode.EXECUTION_FAILED, f"VM refused {rel}: {error.get('message', error)}")
        return str(reply["path"])

    async def get_file(self, rel: str, area: str, task_id: str) -> bytes:
        reply = await self._exchange({"type": "get_file", "request_id": f"f{next(self._ids)}", "path": rel,
                                      "area": area, "task_id": task_id}, FILE_OP_TIMEOUT_S)
        if reply.get("type") != "file_data":
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"VM refused {rel}: {reply.get('error')}")
        data = unpack(str(reply["data"]))
        if hashlib.sha256(data).hexdigest() != reply.get("sha256"):
            raise ToolError(ErrorCode.VERIFICATION_FAILED, f"{rel}: checksum mismatch from the VM")
        return data

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except BaseException:  # noqa: BLE001
                pass
        self._reader = None


class VmRouter:
    def __init__(self, settings: VmSettings) -> None:
        self.settings = settings
        self.remote = RemoteWorker(Endpoint.parse(settings.endpoint), settings.key_file)
        self.workspace = Workspace(settings.host_workspace, settings.audit_path, "host")

    def handles(self, tool: str) -> bool:
        return any(fnmatch.fnmatchcase(tool, pattern) for pattern in self.settings.tools)

    async def _translate_args(self, value: Any, key: str, task_id: str) -> Any:
        if isinstance(value, dict):
            return {k: await self._translate_args(v, k, task_id) for k, v in value.items()}
        if isinstance(value, list):
            return [await self._translate_args(v, key, task_id) for v in value]
        if key not in PATH_KEYS or not isinstance(value, str) or not value:
            return value
        if value.startswith(GUEST_WORKSPACE_PREFIX):
            return value  # already a VM path
        rel = self.workspace.relative(Path(value)) if Path(value).is_absolute() else None
        if rel is None:
            self.workspace.audit("share", value, "denied", reason="outside the host VM workspace", task_id=task_id)
            raise ToolError(ErrorCode.POLICY_DENIED,
                            f"{value!r} is on the host; the agent VM only sees files placed in "
                            f"{self.settings.host_workspace}. Copy the file there first.")
        if Path(value).is_file():
            data, _ = self.workspace.read(rel, task_id)
            await self.remote.put_file(rel, data, task_id)
        return f"{GUEST_WORKSPACE_PREFIX}{rel}"

    async def forward(self, request: Request, timeout_s: float) -> dict[str, Any]:
        args = await self._translate_args(request.args, "", request.task_id)
        routed = Request(request.request_id, request.task_id, request.step_id, request.tool, args,
                         request.deadline_unix_ms)
        reply = await self.remote.call(routed, timeout_s)
        result = reply.get("result") or {}
        fetched = []
        for artifact in result.get("artifacts") or []:
            path = str(artifact.get("path") or "")
            try:
                data = await self.remote.get_file(path, "artifacts", request.task_id)
            except ToolError as exc:
                logger.warning("artifact %s not fetched from the VM: %s", path, exc)
                continue
            local = self.settings.host_artifacts / request.task_id / Path(path.replace("\\", "/")).name
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            fetched.append({**artifact, "path": str(local), "vm_path": path})
        if fetched:
            result["artifacts"] = fetched
        reply.setdefault("metrics", {})["backend"] = f"vm:{(reply.get('metrics') or {}).get('backend', '')}"
        return reply

    async def close(self) -> None:
        await self.remote.close()
