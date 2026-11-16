"""E2E on the provisioned agent VM (acceptance 3-5): Notepad and Paint driven INSIDE the VM while the host
desktop is untouched.

Run explicitly, with the VM running and `[vm] enabled = true`:
    uv run pytest --run-vm tests/test_vm_e2e.py -v
Host-side proofs per step: the host foreground window, cursor position and the set of top-level host windows
do not change (no input conflict), and the tool's result comes from the guest (backend "vm:...").
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import win32api
import win32gui

from lga_common.config import load_config
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.protocol import Request
from lga_worker.remote.host import VmRouter, VmSettings

pytestmark = pytest.mark.vm


def _host_state() -> tuple[int, tuple[int, int], frozenset[int]]:
    hwnds: set[int] = set()
    win32gui.EnumWindows(lambda h, _: hwnds.add(h) if win32gui.IsWindowVisible(h) else None, None)
    return win32gui.GetForegroundWindow(), win32api.GetCursorPos(), frozenset(hwnds)


def _req(tool: str, args: dict, seconds: float = 120, task: str = "vm-e2e") -> Request:
    return Request(uuid.uuid4().hex, task, "s", tool, args, int((time.time() + seconds) * 1000))


@pytest.fixture(scope="module")
def router() -> VmRouter:
    settings = VmSettings.from_config(load_config())
    if not settings.enabled:
        pytest.skip("[vm] enabled = false")
    return VmRouter(settings)


def test_notepad_and_paint_in_vm_without_touching_host(router: VmRouter) -> None:
    async def run() -> None:
        before = _host_state()
        launched = await router.forward(_req("desktop.launch_app", {"target": "notepad"}), 120)
        assert launched["status"] == "ok", launched
        assert launched["metrics"]["backend"].startswith("vm:")
        hwnd = launched["result"]["data"]["window"]["hwnd"]
        typed = await router.forward(_req("desktop.type", {"text": "xin chào từ VM\ndòng hai"}), 120)
        assert typed["status"] == "ok", typed
        text = await router.forward(_req("desktop.read_window_text", {"hwnd": hwnd}), 60)
        assert "xin chào từ VM" in str(text["result"]), text
        await router.forward(_req("desktop.close_app", {"hwnd": hwnd, "discard": True}), 60)
        drawn = await router.forward(_req("paint.draw", {"template": "house"}, 600), 600)
        assert drawn["status"] == "ok", drawn
        after = _host_state()
        assert after[0] == before[0], "host foreground window changed"
        assert after[1] == before[1], "host mouse cursor moved"
        assert not (after[2] - before[2]), "new windows appeared on the host desktop"

    asyncio.run(run())


def test_stop_during_vm_action_leaves_nothing_running(router: VmRouter) -> None:
    async def run() -> None:
        long = asyncio.create_task(router.forward(_req("paint.draw", {"template": "house"}, 600), 600))
        await asyncio.sleep(8)
        started = time.monotonic()
        long.cancel()
        with pytest.raises(asyncio.CancelledError):
            await long
        assert time.monotonic() - started < 6
        # the guest accepts new work right away: nothing of the cancelled call is still holding the desktop
        listed = await router.forward(_req("desktop.list_windows", {}), 60)
        assert listed["status"] == "ok"

    asyncio.run(run())


def test_vm_paused_is_reported_then_recovered(router: VmRouter) -> None:
    """Pause the VM from the host (Suspend-VM) during a call: BACKEND_UNAVAILABLE / TIMEOUT, never a hang."""
    import subprocess

    name = "LGA-Agent"

    async def run() -> None:
        call = asyncio.create_task(router.forward(_req("desktop.list_windows", {}, 20), 20))
        subprocess.run(["powershell", "-NoProfile", "-c", f"Suspend-VM -Name {name}"], check=True)
        try:
            with pytest.raises(ToolError) as exc:
                await asyncio.wait_for(call, 60)
            assert exc.value.code in (ErrorCode.TIMEOUT, ErrorCode.BACKEND_UNAVAILABLE)
        finally:
            subprocess.run(["powershell", "-NoProfile", "-c", f"Resume-VM -Name {name}"], check=True)
        for _ in range(30):
            try:
                ok = await router.forward(_req("desktop.list_windows", {}), 60)
                assert ok["status"] == "ok"
                return
            except ToolError:
                await asyncio.sleep(2)
        raise AssertionError("the VM channel did not come back after Resume-VM")

    asyncio.run(run())
