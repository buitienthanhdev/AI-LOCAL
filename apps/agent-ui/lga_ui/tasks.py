"""Task control shared by the front-ends (popup chat, web UI).

Like the web UI, this owns no agent logic: tasks run in the real `agent` CLI; progress is read from the
task record (`memory/tasks/<id>.json`) and the audit log (`logs/tasks/<id>.jsonl`); confirmations and
questions are answered through `<id>.request.json` / `<id>.response.json`.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lga_common.config import Config

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CONTROL_TIMEOUT_S = 30.0
VALID_MODES = ("normal", "trusted", "full_access")
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,80}$")


def find_agent_exe(root: Path) -> Path:
    for candidate in (root / "target" / "release" / "agent.exe", root / "target" / "debug" / "agent.exe"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("agent.exe not found; run `cargo build --release` in the repository root")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@dataclass
class TaskClient:
    config: Config
    agent_exe: Path

    @property
    def tasks_dir(self) -> Path:
        return self.config.path("memory.tasks_dir")

    def _audit(self, task_id: str) -> Path:
        return self.config.path("paths.logs_dir") / "tasks" / f"{task_id}.jsonl"

    def _check(self, task_id: str) -> None:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"invalid task id {task_id!r}")

    def _spawn(self, args: list[str], log_name: str) -> None:
        log_dir = self.config.path("paths.logs_dir") / "ui"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / log_name).open("ab") as out:
            subprocess.Popen([str(self.agent_exe), *args], cwd=str(self.config.root), stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, close_fds=True)

    def start(self, text: str, mode: str) -> str:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode {mode!r}")
        task_id = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self._spawn(["run", "--ui-channel", "--task-id", task_id, "--mode", mode, text], f"{task_id}.out.log")
        return task_id

    def resume(self, task_id: str) -> None:
        self._check(task_id)
        self._spawn(["resume", "--ui-channel", task_id], f"{task_id}.resume.log")

    def control(self, task_id: str, action: str) -> tuple[bool, str]:
        """cancel | pause | unpause, through the CLI."""
        self._check(task_id)
        if action not in ("cancel", "pause", "unpause"):
            raise ValueError(f"invalid action {action!r}")
        result = subprocess.run([str(self.agent_exe), action, task_id], cwd=str(self.config.root),
                                capture_output=True, timeout=CONTROL_TIMEOUT_S, creationflags=CREATE_NO_WINDOW)
        output = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
        return result.returncode == 0, output

    def record(self, task_id: str) -> dict[str, Any] | None:
        self._check(task_id)
        data = _read_json(self.tasks_dir / f"{task_id}.json")
        return data if isinstance(data, dict) else None

    def pending_request(self, task_id: str) -> dict[str, Any] | None:
        self._check(task_id)
        data = _read_json(self.tasks_dir / f"{task_id}.request.json")
        return data if isinstance(data, dict) else None

    def respond(self, task_id: str, approve: bool | None = None, answer: str | None = None) -> bool:
        pending = self.pending_request(task_id)
        if pending is None:
            return False
        reply: dict[str, Any] = {"id": pending.get("id")}
        if pending.get("kind") == "confirm":
            reply["approve"] = bool(approve)
        else:
            reply["answer"] = answer or ""
        target = self.tasks_dir / f"{task_id}.response.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(reply), encoding="utf-8")
        tmp.replace(target)
        return True

    def events(self, task_id: str, after: int) -> tuple[list[dict[str, Any]], int]:
        """Audit events after line `after`; returns (events, new cursor)."""
        self._check(task_id)
        path = self._audit(task_id)
        events: list[dict[str, Any]] = []
        cursor = after
        if not path.is_file():
            return events, cursor
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if number <= after:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # line still being written; read it next time
                cursor = number
        return events, cursor
