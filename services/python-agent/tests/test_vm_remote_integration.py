"""Integration: a REAL guest worker process (same code that runs in the VM) on loopback TCP, driven by the host
router through the real worker protocol. Only read-only tools are used (no input on this desktop).

What a VM adds on top (hv socket transport, separate desktop) is covered by tests/vm/test_vm_e2e.py, which
needs the provisioned VM.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from lga_common.config import load_config
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.protocol import Request
from lga_worker.remote.channel import new_key
from lga_worker.remote.host import VmRouter, VmSettings

PY = Path(sys.executable)


def _deadline(seconds: float) -> int:
    return int((time.time() + seconds) * 1000)


class Guest:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.key = tmp / "vm.key"
        new_key(self.key)
        self.ready = tmp / "port"
        self.proc: subprocess.Popen[bytes] | None = None
        cfg = tmp / "guest.toml"
        workspace = (tmp / "guest-ws").as_posix()
        cfg.write_text(f'[vm]\nenabled = false\nguest_workspace = "{workspace}"\n', encoding="utf-8")
        self.env = {**os.environ, "LGA_CONFIG": str(cfg), "PYTHONIOENCODING": "utf-8"}

    def start(self, port: int = 0) -> int:
        self.ready.unlink(missing_ok=True)
        self.proc = subprocess.Popen(
            [str(PY), "-m", "lga_worker.remote.guest", "--listen", f"tcp:127.0.0.1:{port}", "--key-file", str(self.key),
             "--ready-file", str(self.ready)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=open(self.tmp / f"guest-{time.time_ns()}.log", "wb"))
        for _ in range(300):
            if self.ready.exists() and self.ready.read_text():
                return int(self.ready.read_text())
            assert self.proc.poll() is None, "guest worker exited at start"
            time.sleep(0.1)
        raise AssertionError("guest worker did not start")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


@pytest.fixture
def guest(tmp_path: Path) -> Iterator[Guest]:
    g = Guest(tmp_path)
    yield g
    g.kill()


def _router(tmp: Path, port: int, key: Path) -> VmRouter:
    config = load_config()
    return VmRouter(VmSettings(enabled=True, endpoint=f"tcp:127.0.0.1:{port}", key_file=key,
                               tools=["desktop.*", "file.info"], host_workspace=tmp / "host-ws",
                               host_artifacts=tmp / "host-art", audit_path=tmp / "host-audit.jsonl"))


def _req(tool: str, args: dict, seconds: float = 60, task: str = "t-int") -> Request:
    return Request(uuid.uuid4().hex, task, "s1", tool, args, _deadline(seconds))


def test_call_shared_file_artifacts_cancel_timeout_disconnect(guest: Guest, tmp_path: Path) -> None:
    port = guest.start()
    router = _router(tmp_path, port, guest.key)

    async def run() -> None:
        # 1. a shared file: host workspace -> guest workspace (audited), path rewritten, tool runs in the guest
        (tmp_path / "host-ws").mkdir(exist_ok=True)
        shared = tmp_path / "host-ws" / "báo cáo.txt"
        shared.write_text("hello", encoding="utf-8")
        reply = await router.forward(_req("file.info", {"path": str(shared)}), 60)
        assert reply["status"] == "ok", reply
        assert "guest-ws" in reply["result"]["observation"] or "guest-ws" in str(reply["result"]["data"])
        assert (tmp_path / "guest-ws" / "báo cáo.txt").read_text(encoding="utf-8") == "hello"
        assert reply["metrics"]["backend"].startswith("vm:")

        # 2. a host path outside the workspace never reaches the VM
        with pytest.raises(ToolError) as exc:
            await router.forward(_req("file.info", {"path": str(PY)}), 30)
        assert exc.value.code == ErrorCode.POLICY_DENIED

        # 3. artifacts come back to the host (desktop.screenshot = read-only capture of the guest's screen)
        reply = await router.forward(_req("desktop.screenshot", {}), 60)
        assert reply["status"] == "ok", reply
        art = reply["result"]["artifacts"][0]
        assert Path(art["path"]).is_file() and str(tmp_path / "host-art") in art["path"]

        # 4. cancel: a long wait in the guest ends within the ack timeout, guest confirms CANCELLED
        long = asyncio.create_task(router.forward(
            _req("desktop.wait_for_window", {"title_contains": f"no-such-{uuid.uuid4().hex}", "timeout_ms": 60000}), 90))
        await asyncio.sleep(2)
        started = time.monotonic()
        long.cancel()
        with pytest.raises(asyncio.CancelledError):
            await long
        assert time.monotonic() - started < 6

        # 5. deadline: guest reports TIMEOUT for its own deadline
        reply = await router.forward(_req("desktop.wait_for_window",
                                          {"title_contains": f"no-such-{uuid.uuid4().hex}", "timeout_ms": 1000}), 30)
        assert reply["status"] == "error"

        # 6. the guest dies during a call -> retryable BACKEND_UNAVAILABLE, then it is picked up again
        long = asyncio.create_task(router.forward(
            _req("desktop.wait_for_window", {"title_contains": f"no-such-{uuid.uuid4().hex}", "timeout_ms": 60000}), 90))
        await asyncio.sleep(2)
        guest.kill()
        with pytest.raises(ToolError) as exc:
            await long
        assert exc.value.code == ErrorCode.BACKEND_UNAVAILABLE and exc.value.retryable
        guest.start(port)
        reply = None
        for _ in range(20):
            try:
                reply = await router.forward(_req("desktop.list_windows", {}), 60)
                break
            except ToolError as err:
                assert err.code == ErrorCode.BACKEND_UNAVAILABLE
                await asyncio.sleep(1)
        assert reply is not None and reply["status"] == "ok"
        await router.close()

    asyncio.run(run())
    audit = (tmp_path / "host-audit.jsonl").read_text(encoding="utf-8")
    assert '"verdict": "denied"' in audit and '"verdict": "ok"' in audit


def test_wrong_key_cannot_drive_the_guest(guest: Guest, tmp_path: Path) -> None:
    port = guest.start()
    other = tmp_path / "other.key"
    new_key(other)
    router = _router(tmp_path, port, other)

    async def run() -> None:
        with pytest.raises(ToolError) as exc:
            await router.forward(_req("desktop.list_windows", {}), 30)
        assert exc.value.code == ErrorCode.BACKEND_UNAVAILABLE and "authentication" in exc.value.message

    asyncio.run(run())
