"""TEST 6 (live): kill the primary Gemini backend and verify the gateway fails over and recovers.

Uses the real configuration and real backend processes. Run:
    uv run pytest -m gateway --run-gateway -s tests/test_gateway_live.py
Prerequisite: scripts/setup-gateway.ps1. No other gateway may be running (ports are fixed by config).

Outcome depends on whether the fallback (WebAI-to-API) has a signed-in Gemini session:
* signed in: the request is served by the fallback;
* not signed in: the failover attempt is made and rejected by the fallback with "not ready"; the test
  then asserts the structured error and reports the fallback as BLOCKED on user sign-in.
In both cases the dead primary must be restarted automatically and serve the next request.
"""

from __future__ import annotations

import asyncio
import os
import time

import psutil
import pytest

from lga_common.config import load_config
from lga_common.redact import Redactor
from lga_gateway.app import assemble
from lga_gateway.errors import GatewayError
from lga_gateway.types import BackendState, ChatRequest, Message, TextPart

pytestmark = pytest.mark.gateway
RESTART_WAIT_S = 60.0


def _ping() -> ChatRequest:
    return ChatRequest(messages=(Message("user", (TextPart("Reply with exactly one word: pong"),)),),
                       timeout_s=120, metadata={"task_id": "test6"})


async def test_kill_primary_fails_over_and_recovers(capsys) -> None:
    pytest.importorskip("aiohttp")
    if os.environ.get("CI"):
        pytest.skip("live Gemini test")
    assembly = assemble(load_config(), Redactor())
    gateway = assembly.gateway
    await gateway.start()
    try:
        primary_route, fallback_route = gateway.routes
        assert primary_route.process is not None and primary_route.process.is_running(), "primary did not start"
        before = await gateway.chat(_ping())
        assert before.backend == "primary"

        pid = primary_route.process.pid
        assert pid is not None
        psutil.Process(pid).kill()
        psutil.Process(pid).wait(timeout=10) if psutil.pid_exists(pid) else None

        fallback_health = (await gateway.health(refresh=False))[1]
        try:
            served = await gateway.chat(_ping())
            assert served.backend == "fallback"
            assert served.attempts[0].backend == "primary" and not served.attempts[0].ok
            outcome = "fallback served the request"
        except GatewayError as exc:
            backends_tried = [a.backend for a in exc.attempts]
            assert "primary" in backends_tried and "fallback" in backends_tried, exc.attempts
            assert fallback_health.state is not BackendState.READY
            outcome = f"BLOCKED: fallback not signed in to Gemini ({fallback_health.detail})"

        deadline = time.monotonic() + RESTART_WAIT_S
        while time.monotonic() < deadline and not primary_route.process.is_running():
            await asyncio.sleep(0.5)
        assert primary_route.process.is_running(), "primary was not restarted"
        assert primary_route.process.pid != pid

        recovered = None
        while time.monotonic() < deadline:
            try:
                recovered = await gateway.chat(_ping())
                if recovered.backend == "primary":
                    break
            except GatewayError:
                pass  # primary still starting / breaker half-open; retry until the deadline
            await asyncio.sleep(1.0)
        assert recovered is not None and recovered.backend == "primary", "primary did not recover"
        with capsys.disabled():
            print(f"\nTEST 6: primary killed -> {outcome}; primary restarted (pid {pid} -> "
                  f"{primary_route.process.pid}) and served again")
    finally:
        await gateway.close()
