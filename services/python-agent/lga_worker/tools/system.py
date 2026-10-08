"""Handlers for shell.exec and filesystem.* (contract: tools/system.json).

These never touch UI Automation, so they run on worker threads, not on the desktop STA thread.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.filesystem import ops
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.shell.powershell import looks_like_elevation_failure, resolve_powershell, run_powershell

STAT_HASH_MAX_BYTES = 256 * 1024 * 1024
OUTPUT_TAIL_CHARS = 4000


def absolute_path(raw: str) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute():
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"path must be absolute: {raw!r}")
    return Path(os.path.normpath(expanded))


def register(registry: ToolRegistry) -> None:
    @registry.handler("shell.exec")
    async def shell_exec(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        cwd = absolute_path(args["cwd"]) if args.get("cwd") else None
        if cwd is not None and not cwd.is_dir():
            raise ToolError(ErrorCode.NOT_FOUND, f"working directory {cwd} does not exist")
        timeout = ctx.timeout_s(args.get("timeout_ms"), ctx.config.seconds("shell.default_timeout_ms"))
        timeout = min(timeout, ctx.config.seconds("shell.max_timeout_ms"))
        executable = resolve_powershell(str(ctx.config.get("shell.executable")))
        result = await asyncio.to_thread(
            run_powershell, args["command"], executable=executable, cwd=cwd, timeout_s=timeout,
            max_output_bytes=int(ctx.config.get("shell.max_output_bytes")), cancel_event=ctx.cancel_event,
        )
        ctx.check_cancelled()
        data = {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr,
                "timed_out": result.timed_out, "truncated": result.truncated, "duration_ms": result.duration_ms}
        if result.timed_out:
            raise ToolError(ErrorCode.TIMEOUT, f"command exceeded {timeout:.0f}s and was terminated with its children",
                            details={"stdout_tail": result.stdout[-OUTPUT_TAIL_CHARS:],
                                     "stderr_tail": result.stderr[-OUTPUT_TAIL_CHARS:]})
        if looks_like_elevation_failure(result):
            raise ToolError(
                ErrorCode.ELEVATION_REQUIRED,
                "the command needs Administrator rights; the agent never elevates by itself",
                details={"escalation_request": {"kind": "uac_elevation", "tool": "shell.exec",
                                                "command": args["command"], "reason": result.stderr[-OUTPUT_TAIL_CHARS:],
                                                "how_to_grant": "run the command yourself in an elevated "
                                                                "PowerShell, or start the agent elevated"}},
            )
        stdout_tail = result.stdout[-OUTPUT_TAIL_CHARS:]
        stderr_tail = result.stderr[-OUTPUT_TAIL_CHARS:]
        return ToolOutcome(
            data=data, backend="powershell",
            checks=[Check("exit_code_zero", result.exit_code == 0, f"exit code {result.exit_code}")],
            observation=f"exit={result.exit_code} in {result.duration_ms}ms\nstdout:\n{stdout_tail}"
                        + (f"\nstderr:\n{stderr_tail}" if stderr_tail.strip() else ""),
        )

    @registry.handler("filesystem.read")
    async def fs_read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = absolute_path(args["path"])
        limit = min(int(args.get("max_bytes") or ctx.config.get("filesystem.max_read_bytes")),
                    int(ctx.config.get("filesystem.max_read_bytes")))
        content = await asyncio.to_thread(ops.read_file, path, limit, args.get("encoding"))
        return ToolOutcome(
            data={"path": str(content.path), "size": content.size, "encoding": content.encoding,
                  "content": content.content, "truncated": content.truncated, "sha256": content.sha256},
            observation=f"{path} ({content.size} bytes, {content.encoding}{', truncated' if content.truncated else ''})",
            backend="filesystem",
        )

    @registry.handler("filesystem.write")
    async def fs_write(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = absolute_path(args["path"])
        result = await asyncio.to_thread(ops.write_file, path, args["content"], args["encoding"], args["overwrite"],
                                         args["create_dirs"], int(ctx.config.get("filesystem.max_write_bytes")))
        return ToolOutcome(
            data={"path": str(result.path), "size": result.size, "sha256": result.sha256, "created": result.created},
            checks=[Check("read_back_matches", True, f"{result.size} bytes sha256={result.sha256[:12]}")],
            artifacts=[{"kind": "file", "path": str(result.path)}],
            observation=f"Wrote {result.size} bytes to {result.path} ({'created' if result.created else 'replaced'}).",
            backend="filesystem",
        )

    @registry.handler("filesystem.list")
    async def fs_list(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = absolute_path(args["path"])
        limit = min(int(args.get("max_entries") or ctx.config.get("filesystem.max_list_entries")),
                    int(ctx.config.get("filesystem.max_list_entries")))
        entries, truncated = await asyncio.to_thread(ops.list_directory, path, limit)
        lines = [f"{'[dir] ' if e.is_dir else ''}{e.name}{'' if e.is_dir else f' ({e.size} B)'}" for e in entries]
        return ToolOutcome(data={"entries": [e.__dict__ for e in entries], "truncated": truncated},
                           observation=f"{path}:\n" + ("\n".join(lines) or "(empty)"), backend="filesystem")

    @registry.handler("filesystem.stat")
    async def fs_stat(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = absolute_path(args["path"])

        def stat() -> dict[str, Any]:
            if not path.exists():
                return {"path": str(path), "exists": False}
            st = path.stat()
            digest = ops.sha256_file(path) if path.is_file() and st.st_size <= STAT_HASH_MAX_BYTES else None
            return {"path": str(path), "exists": True, "is_dir": path.is_dir(), "size": st.st_size,
                    "modified_unix": st.st_mtime, "sha256": digest}

        data = await asyncio.to_thread(stat)
        summary = (f"{path} exists ({'directory' if data['is_dir'] else str(data['size']) + ' bytes'})"
                   if data["exists"] else f"{path} does not exist")
        return ToolOutcome(data=data, observation=summary, backend="filesystem")


def register_environment(registry: ToolRegistry) -> None:
    from lga_worker import inventory
    from lga_worker.shell.powershell import resolve_powershell as _resolve

    @registry.handler("system.environment")
    async def environment(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        def collect() -> dict[str, Any]:
            chrome = inventory.find_chrome(str(ctx.config.get("browser.chrome_path")))
            shell = _resolve(str(ctx.config.get("shell.executable")))
            return {
                "chrome": {"found": chrome is not None, "path": str(chrome) if chrome else None,
                           "version": inventory.file_version(chrome) if chrome else None},
                "autocad": {"installations": inventory.autocad_installations(),
                            "com_registered": inventory.com_registered(str(ctx.config.get("autocad.prog_id"))),
                            "running": inventory.process_running("acad.exe")},
                "powershell": {"path": shell, "version": inventory.file_version(Path(shell))},
                "screen": inventory.screen_layout(),
                "user": {"name": os.environ.get("USERNAME", ""), "is_admin": inventory.is_admin()},
            }

        data = await asyncio.to_thread(collect)
        acad = data["autocad"]["installations"]
        summary = [
            f"Chrome: {data['chrome']['version'] or 'not found'}",
            "AutoCAD: " + (", ".join(f"{a['release']} at {a['location']}" for a in acad) or "not installed")
            + f" (COM {'registered' if data['autocad']['com_registered'] else 'not registered'},"
              f" {'running' if data['autocad']['running'] else 'not running'})",
            f"Screen: {data['screen']}",
            f"User: {data['user']['name']} (admin: {data['user']['is_admin']})",
        ]
        return ToolOutcome(data=data, observation="\n".join(summary), backend="registry")
