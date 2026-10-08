"""Core <-> worker IPC protocol: one JSON object per line over stdio (UTF-8).

core -> worker
  {"type": "request", "request_id", "task_id", "step_id", "tool", "args", "deadline_unix_ms"}
  {"type": "cancel", "request_id"}
  {"type": "ping", "request_id"}
  {"type": "shutdown"}
worker -> core
  {"type": "ready", "protocol": 1, "worker_version", "pid", "tools": [...]}
  {"type": "response", "request_id", "status": "ok" | "error", "result", "error", "metrics"}
  {"type": "pong", "request_id"}

`result` = {"data", "verification": {"status", "checks"}, "artifacts", "observation"}
`error`  = {"code", "message", "retryable", "details"}
`metrics`= {"started_unix_ms", "duration_ms", "backend"}
Nothing else is ever written to stdout; logs go to stderr and logs/worker/.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    request_id: str
    task_id: str
    step_id: str
    tool: str
    args: dict[str, Any]
    deadline_unix_ms: int


@dataclass(frozen=True)
class Cancel:
    request_id: str


@dataclass(frozen=True)
class Ping:
    request_id: str


@dataclass(frozen=True)
class Shutdown:
    pass


Message = Request | Cancel | Ping | Shutdown


def _str(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"field {key!r} must be a non-empty string")
    return value


def parse_message(line: bytes) -> Message:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object")
    kind = obj.get("type")
    if kind == "request":
        args = obj.get("args")
        if not isinstance(args, dict):
            raise ProtocolError("field 'args' must be an object")
        deadline = obj.get("deadline_unix_ms")
        if not isinstance(deadline, int):
            raise ProtocolError("field 'deadline_unix_ms' must be an integer")
        return Request(_str(obj, "request_id"), _str(obj, "task_id"), str(obj.get("step_id") or ""),
                       _str(obj, "tool"), args, deadline)
    if kind == "cancel":
        return Cancel(_str(obj, "request_id"))
    if kind == "ping":
        return Ping(_str(obj, "request_id"))
    if kind == "shutdown":
        return Shutdown()
    raise ProtocolError(f"unknown message type {kind!r}")


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
