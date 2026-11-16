"""The only files the agent VM and the host exchange: one workspace directory per side, with an audit log.

* Paths are relative to the workspace root; absolute paths, drive letters, UNC paths, `..`, alternate data
  streams and reserved device names are refused, and the resolved path must stay inside the root (symlinks
  and junctions that point outside are refused too).
* Size limit per file; SHA-256 is checked on arrival.
* Every transfer and every refusal is appended to an audit JSONL (who, what, size, hash, task, verdict).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from lga_worker.errors import ErrorCode, ToolError

MAX_FILE_BYTES = 32 * 1024 * 1024
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)


class Workspace:
    def __init__(self, root: Path, audit_path: Path, side: str) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_path = audit_path
        self.side = side
        self._lock = threading.Lock()

    def audit(self, action: str, rel: str, verdict: str, **extra: Any) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "side": self.side, "action": action, "path": rel,
                 "verdict": verdict, **extra}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def resolve(self, rel: str, action: str = "resolve", task_id: str = "") -> Path:
        reason = self._reject_reason(rel)
        if reason is None:
            target = (self.root / rel).resolve()
            if target != self.root and self.root not in target.parents:
                reason = "resolves outside the workspace (link or junction)"
        if reason is not None:
            self.audit(action, rel, "denied", reason=reason, task_id=task_id)
            raise ToolError(ErrorCode.POLICY_DENIED, f"workspace path {rel!r} refused: {reason}")
        return target

    @staticmethod
    def _reject_reason(rel: str) -> str | None:
        if not isinstance(rel, str) or not rel.strip():
            return "empty path"
        win, posix = PureWindowsPath(rel), PurePosixPath(rel.replace("\\", "/"))
        if win.drive or win.root or posix.is_absolute() or rel.startswith(("\\\\", "//")):
            return "absolute paths are not allowed; use a path relative to the workspace"
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts:
            return "empty path"
        for part in parts:
            if part == "..":
                return "'..' is not allowed"
            if ":" in part:
                return "':' (drive or alternate data stream) is not allowed"
            if _RESERVED.match(part):
                return f"reserved device name {part!r}"
            if part != part.rstrip(" ."):
                return "names ending in a space or dot are not allowed"
        return None

    def write(self, rel: str, data: bytes, sha256: str, task_id: str = "", source: str = "") -> Path:
        target = self.resolve(rel, "write", task_id)
        if len(data) > MAX_FILE_BYTES:
            self.audit("write", rel, "denied", reason="too large", bytes=len(data), task_id=task_id)
            raise ToolError(ErrorCode.POLICY_DENIED, f"{rel}: {len(data)} bytes exceeds {MAX_FILE_BYTES}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            self.audit("write", rel, "denied", reason="sha256 mismatch", task_id=task_id)
            raise ToolError(ErrorCode.VERIFICATION_FAILED, f"{rel}: checksum mismatch in transfer")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        self.audit("write", rel, "ok", bytes=len(data), sha256=digest, task_id=task_id, source=source)
        return target

    def read(self, rel: str, task_id: str = "") -> tuple[bytes, str]:
        target = self.resolve(rel, "read", task_id)
        if not target.is_file():
            self.audit("read", rel, "denied", reason="not a file", task_id=task_id)
            raise ToolError(ErrorCode.NOT_FOUND, f"{rel} is not a file in the workspace")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            self.audit("read", rel, "denied", reason="too large", bytes=size, task_id=task_id)
            raise ToolError(ErrorCode.POLICY_DENIED, f"{rel}: {size} bytes exceeds {MAX_FILE_BYTES}")
        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.audit("read", rel, "ok", bytes=len(data), sha256=digest, task_id=task_id)
        return data, digest

    def relative(self, path: Path) -> str | None:
        """`path` relative to the root (posix form), or None when it is outside."""
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None


def pack(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unpack(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)
