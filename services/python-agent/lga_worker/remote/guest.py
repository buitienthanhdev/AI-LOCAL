"""Worker server inside the agent VM: the normal worker (same tools, same protocol) behind the authenticated channel.

    python -m lga_worker.remote.guest --listen hv:00000000-0000-0000-0000-000000000000:<service-guid>
                                      --key-file C:\\lga\\vm.key

It must run in the VM's interactive desktop session (scheduled task "at logon" of the agent's local user),
because GUI automation needs that desktop. One host at a time; when the host disconnects (crash, timeout,
VM paused) every in-flight tool call is cancelled and the providers are closed, which releases held keys and
mouse buttons - the desktop is left idle, then the next connection gets a fresh worker.

Besides the worker protocol it serves:
  {"type": "put_file", "request_id", "path", "data", "sha256", "task_id"}  -> workspace write
  {"type": "get_file", "request_id", "path", "area": "workspace"|"artifacts", "task_id"} -> file bytes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from lga_common.config import Config, load_config
from lga_common.redact import Redactor

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.remote.channel import ChannelError, SecureChannel, load_key
from lga_worker.remote.transport import Endpoint, Listener
from lga_worker.remote.workspace import Workspace, pack, unpack
from lga_worker.registry import ToolRegistry
from lga_worker.server import Worker, build_registry
from lga_worker.services import Services

logger = logging.getLogger("lga_worker.guest")
WORKSPACE_PREFIX = "vm-workspace:"


class _ChannelOut:
    """`Worker.send` writes encoded protocol lines here; they go out over the channel in order."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()

    def write(self, data: bytes) -> None:
        self._loop.call_soon_threadsafe(self.queue.put_nowait, json.loads(data))

    def flush(self) -> None:
        pass


def guest_args(value: Any, work: Workspace) -> Any:
    """`vm-workspace:<rel>` (how the host names shared files) -> absolute path in this VM's workspace."""
    if isinstance(value, dict):
        return {k: guest_args(v, work) for k, v in value.items()}
    if isinstance(value, list):
        return [guest_args(v, work) for v in value]
    if isinstance(value, str) and value.startswith(WORKSPACE_PREFIX):
        return str(work.resolve(value[len(WORKSPACE_PREFIX):], "use"))
    return value


def workspaces(config: Config) -> tuple[Workspace, Workspace]:
    vm = config.section("vm")
    audit = config.path("paths.logs_dir") / "vm" / "guest-audit.jsonl"
    work = Workspace(config.root / str(vm.get("guest_workspace", "runtime/vm-workspace")), audit, "guest")
    artifacts = Workspace(config.path("paths.artifacts_dir"), audit, "guest-artifacts")
    return work, artifacts


async def serve_connection(channel: SecureChannel, config: Config, registry: ToolRegistry) -> None:
    out = _ChannelOut()
    worker = Worker(registry, Services(config), out, Redactor(config.get("observability.redact_env")))  # type: ignore[arg-type]
    work, artifacts = workspaces(config)
    lines: asyncio.Queue[bytes | None] = asyncio.Queue()
    run = asyncio.create_task(worker.run(lines), name="guest-worker")

    async def pump_out() -> None:
        while True:
            await channel.send(await out.queue.get())

    async def file_op(body: dict[str, Any]) -> dict[str, Any]:
        rid, task = str(body.get("request_id") or ""), str(body.get("task_id") or "")
        try:
            if body["type"] == "put_file":
                path = await asyncio.to_thread(work.write, str(body.get("path")), unpack(str(body.get("data"))),
                                               str(body.get("sha256")), task, "host")
                return {"type": "file_ok", "request_id": rid, "path": work.relative(path)}
            area, rel = work, str(body.get("path"))
            if body.get("area") == "artifacts":  # the host asks for an artifact path a tool reported
                area, rel = artifacts, artifacts.relative(Path(rel)) or rel
            data, digest = await asyncio.to_thread(area.read, rel, task)
            return {"type": "file_data", "request_id": rid, "data": pack(data), "sha256": digest}
        except ToolError as exc:
            return {"type": "file_error", "request_id": rid, "error": exc.to_dict()}
        except (KeyError, ValueError) as exc:
            return {"type": "file_error", "request_id": rid,
                    "error": ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)).to_dict()}

    pump = asyncio.create_task(pump_out(), name="guest-out")
    try:
        while not run.done():
            receive = asyncio.ensure_future(channel.recv())
            done, _ = await asyncio.wait({receive, run, pump}, return_when=asyncio.FIRST_COMPLETED)
            if receive not in done:
                receive.cancel()
                break
            body = receive.result()
            if body.get("type") in ("put_file", "get_file"):
                out.queue.put_nowait(await file_op(body))
            else:
                if body.get("type") == "request":
                    try:
                        body["args"] = guest_args(body.get("args") or {}, work)
                    except ToolError as exc:
                        out.queue.put_nowait({"type": "response", "request_id": body.get("request_id"),
                                              "status": "error", "result": None, "error": exc.to_dict(),
                                              "metrics": {}})
                        continue
                lines.put_nowait(json.dumps(body).encode("utf-8"))
    except ChannelError as exc:
        logger.warning("host connection ended: %s", exc)
    finally:
        lines.put_nowait(None)  # Worker.run cancels in-flight calls and closes the providers
        try:
            await asyncio.wait_for(run, 30)
        except Exception:  # noqa: BLE001 - shutting down after a dropped host
            logger.exception("guest worker did not shut down cleanly")
        # flush what the worker still had to say (CANCELLED replies) before closing, best effort
        while not out.queue.empty():
            try:
                await asyncio.wait_for(channel.send(out.queue.get_nowait()), 1)
            except Exception:  # noqa: BLE001
                break
        pump.cancel()
        await channel.close()


async def serve(listen: str, key_file: Path, once: bool = False, ready_file: Path | None = None) -> None:
    config = load_config()
    key = load_key(key_file)
    registry = build_registry(config.path("paths.tools_dir"))
    listener = Listener(Endpoint.parse(listen))
    if ready_file is not None:
        ready_file.write_text(str(listener.port), encoding="ascii")
    logger.info("guest worker listening on %s", listen)
    try:
        while True:
            reader, writer = await listener.accept()
            channel = SecureChannel(reader, writer, "guest")
            try:
                await channel.handshake(key)
            except ChannelError as exc:
                logger.warning("rejected a connection: %s", exc)
                await channel.close()
                continue
            logger.info("host connected")
            await serve_connection(channel, config, registry)
            if once:
                return
    finally:
        listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--once", action="store_true", help="exit after the first host disconnects (tests)")
    parser.add_argument("--ready-file", type=Path, help="write the bound TCP port here once listening (tests)")
    parser.add_argument("--log-file", type=Path, help="log here instead of stderr (pythonw.exe has no console)")
    args = parser.parse_args()
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, filename=args.log_file, encoding="utf-8", format=fmt)
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format=fmt)
    asyncio.run(serve(args.listen, args.key_file, args.once, args.ready_file))


if __name__ == "__main__":
    main()
