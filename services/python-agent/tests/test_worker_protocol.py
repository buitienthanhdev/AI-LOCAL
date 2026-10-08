"""End-to-end tests of the worker process over its stdio JSON-lines protocol."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pytest

READY_TIMEOUT_S = 60


class WorkerProcess:
    def __init__(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "lga_worker"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.messages: Queue[dict[str, Any]] = Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.ready = self.next_message(READY_TIMEOUT_S)
        assert self.ready["type"] == "ready", self.ready

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.messages.put(json.loads(line))

    def next_message(self, timeout: float) -> dict[str, Any]:
        try:
            return self.messages.get(timeout=timeout)
        except Empty:
            raise AssertionError("worker did not answer in time") from None

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, tool: str, args: dict[str, Any], timeout_s: float = 30, wait: bool = True) -> Any:
        request_id = uuid.uuid4().hex
        self.send({"type": "request", "request_id": request_id, "task_id": "test-task", "step_id": "s1",
                   "tool": tool, "args": args, "deadline_unix_ms": int((time.time() + timeout_s) * 1000)})
        if not wait:
            return request_id
        return self.wait_for(request_id, timeout_s + 10)

    def wait_for(self, request_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            message = self.next_message(max(0.1, deadline - time.monotonic()))
            if message.get("request_id") == request_id:
                return message

    def close(self) -> None:
        self.send({"type": "shutdown"})
        self.proc.wait(timeout=30)


@pytest.fixture(scope="module")
def worker() -> Iterator[WorkerProcess]:
    w = WorkerProcess()
    yield w
    w.close()


def test_ready_lists_tools(worker: WorkerProcess) -> None:
    assert worker.ready["protocol"] == 1
    for tool in ("desktop.click", "shell.exec", "filesystem.write", "filesystem.stat"):
        assert tool in worker.ready["tools"]


def test_filesystem_write_is_self_verified(worker: WorkerProcess, tmp_path: Path) -> None:
    target = tmp_path / "out" / "hello.txt"
    response = worker.request("filesystem.write", {"path": str(target), "content": "hello world"})
    assert response["status"] == "ok", response
    assert response["result"]["verification"]["status"] == "passed"
    assert target.read_text() == "hello world"
    stat = worker.request("filesystem.stat", {"path": str(target)})
    assert stat["result"]["data"]["exists"] and stat["result"]["data"]["size"] == 11
    assert stat["result"]["verification"]["status"] == "not_applicable"


def test_invalid_arguments_and_unknown_tool(worker: WorkerProcess) -> None:
    bad = worker.request("filesystem.write", {"path": "C:\\x.txt"})
    assert bad["status"] == "error" and bad["error"]["code"] == "INVALID_ARGUMENT"
    relative = worker.request("filesystem.read", {"path": "relative.txt"})
    assert relative["error"]["code"] == "INVALID_ARGUMENT"
    unknown = worker.request("nope.tool", {})
    assert unknown["error"]["code"] == "UNKNOWN_TOOL"


def test_shell_exit_code_is_reported_not_success(worker: WorkerProcess) -> None:
    response = worker.request("shell.exec", {"command": "Write-Output hi; exit 4"}, timeout_s=60)
    assert response["status"] == "ok"
    assert response["result"]["data"]["exit_code"] == 4
    assert response["result"]["verification"]["status"] == "failed"


def test_cancel_long_running_shell(worker: WorkerProcess) -> None:
    request_id = worker.request("shell.exec", {"command": "Start-Sleep 60"}, timeout_s=120, wait=False)
    time.sleep(2)
    worker.send({"type": "cancel", "request_id": request_id})
    response = worker.wait_for(request_id, 30)
    assert response["status"] == "error" and response["error"]["code"] == "CANCELLED"


def test_deadline_enforced(worker: WorkerProcess) -> None:
    response = worker.request("shell.exec", {"command": "Start-Sleep 30", "timeout_ms": 60000}, timeout_s=2)
    assert response["status"] == "error" and response["error"]["code"] == "TIMEOUT"
    assert response["metrics"]["duration_ms"] < 15000


def test_ping(worker: WorkerProcess) -> None:
    worker.send({"type": "ping", "request_id": "p1"})
    assert worker.wait_for("p1", 10)["type"] == "pong"


@pytest.mark.desktop
def test_desktop_list_windows_over_protocol(worker: WorkerProcess) -> None:
    response = worker.request("desktop.list_windows", {}, timeout_s=60)
    assert response["status"] == "ok", response
    assert isinstance(response["result"]["data"]["windows"], list)
