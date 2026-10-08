"""GeminiGateway: primary/fallback routing with circuit breakers, bounded retries and health monitoring.

Routing for every request:
  for backend in [primary, fallback]:
      skip if its breaker is open
      try up to `retry.max_attempts` times (only for RATE_LIMITED / TRANSIENT errors, with backoff)
      CLIENT_ERROR  -> stop and return the error (another backend would reject it too)
      other errors  -> record on the breaker (except CAPABILITY) and fail over
All attempts share one deadline; nothing retries forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from lga_gateway.attachments import Attachment, AttachmentStore
from lga_gateway.backends.base import Backend
from lga_gateway.errors import (
    BREAKER_PENALTY_KINDS,
    RETRY_SAME_BACKEND_KINDS,
    BackendError,
    ErrorKind,
    GatewayError,
)
from lga_gateway.provider import ChatProvider
from lga_gateway.resilience import BackoffPolicy, BreakerState, CircuitBreaker
from lga_gateway.supervisor import ManagedProcess
from lga_gateway.types import (
    Attempt,
    BackendHealth,
    BackendState,
    ChatRequest,
    ChatResult,
    ModelInfo,
    StreamDelta,
)

logger = logging.getLogger(__name__)

STARTUP_POLL_INTERVAL_S = 0.5


@dataclass
class Route:
    backend: Backend
    breaker: CircuitBreaker
    process: ManagedProcess | None
    startup_timeout_s: float
    health: BackendHealth | None = None


class GeminiGateway(ChatProvider):
    def __init__(self, routes: list[Route], attachments: AttachmentStore, retry: BackoffPolicy,
                 request_timeout_s: float, health_interval_s: float) -> None:
        if not routes:
            raise ValueError("at least one backend route is required")
        self._routes = routes
        self._attachments = attachments
        self._retry = retry
        self._request_timeout_s = request_timeout_s
        self._health_interval_s = health_interval_s
        self._monitor_task: asyncio.Task[None] | None = None
        self._restart_locks = {route.backend.name: asyncio.Lock() for route in routes}
        self._background: set[asyncio.Task[None]] = set()

    @property
    def routes(self) -> list[Route]:
        return self._routes

    # ------------------------------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------------------------------
    async def start(self) -> None:
        for route in self._routes:
            await route.backend.start()
        await asyncio.gather(*(self._ensure_process(route) for route in self._routes))
        await self.health(refresh=True)
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="health-monitor")

    async def close(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass  # expected: we cancelled it
        for route in self._routes:
            await route.backend.close()
            if route.process is not None:
                await asyncio.to_thread(route.process.stop)

    async def _ensure_process(self, route: Route) -> None:
        """Start (or restart) a managed backend whose process is not running, then wait until it answers."""
        process = route.process
        if process is None or process.is_running():
            return
        async with self._restart_locks[route.backend.name]:
            if process.is_running():
                return
            try:
                probe = await route.backend.probe()
                if probe.state is not BackendState.UNAVAILABLE:
                    logger.info("%s already running outside the gateway; adopting it", route.backend.name)
                    return
            except BackendError:
                pass  # not reachable: start it below
            try:
                await asyncio.to_thread(process.start)
            except (RuntimeError, FileNotFoundError, OSError) as exc:
                logger.error("cannot start %s: %s", route.backend.name, exc)
                route.breaker.force_open()
                return
            deadline = time.monotonic() + route.startup_timeout_s
            while time.monotonic() < deadline:
                if not process.is_running():
                    logger.error("%s exited during startup (code %s)", route.backend.name, process.last_exit_code)
                    route.breaker.force_open()
                    return
                try:
                    probe = await route.backend.probe()
                    if probe.state is not BackendState.UNAVAILABLE:
                        logger.info("%s is up: %s", route.backend.name, probe.detail)
                        return
                except BackendError:
                    pass  # still starting
                await asyncio.sleep(STARTUP_POLL_INTERVAL_S)
            logger.error("%s did not become reachable within %.0fs", route.backend.name, route.startup_timeout_s)

    def _restart_if_dead(self, route: Route, exc: BackendError) -> None:
        """A managed backend that died is restarted right away instead of at the next health tick."""
        if exc.kind is ErrorKind.BACKEND_DOWN and route.process is not None and not route.process.is_running():
            task = asyncio.create_task(self._ensure_process(route), name=f"restart-{route.backend.name}")
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_interval_s)
            try:
                await self.health(refresh=True)
            except Exception:  # noqa: BLE001 - the monitor must survive; the failure is logged with traceback
                logger.exception("health monitor iteration failed")

    async def _probe(self, route: Route) -> BackendHealth:
        if route.process is not None and not route.process.is_running():
            await self._ensure_process(route)
        try:
            result = await route.backend.probe()
            state, detail, caps = result.state, result.detail, result.capabilities
        except BackendError as exc:
            state, detail, caps = BackendState.UNAVAILABLE, exc.message, {"chat": False, "images": False}
        if state is BackendState.UNAVAILABLE and route.breaker.state is BreakerState.CLOSED:
            route.breaker.record_failure()
        route.health = BackendHealth(
            name=route.backend.name, kind=route.backend.kind, state=state, detail=detail, capabilities=caps,
            breaker=str(route.breaker.state), checked_at=time.time(),
            process=route.process.status() if route.process else {"managed": False},
        )
        return route.health

    # ------------------------------------------------------------------------------------------
    # ChatProvider
    # ------------------------------------------------------------------------------------------
    async def health(self, refresh: bool = False) -> list[BackendHealth]:
        if refresh or any(route.health is None for route in self._routes):
            return list(await asyncio.gather(*(self._probe(route) for route in self._routes)))
        for route in self._routes:
            assert route.health is not None
            route.health.breaker = str(route.breaker.state)
        return [route.health for route in self._routes if route.health is not None]

    async def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        for route in self._routes:
            try:
                models.extend(await route.backend.list_models())
            except BackendError as exc:
                logger.warning("listing models of %s failed: %s", route.backend.name, exc.message)
        return models

    async def upload(self, *, path: Path | None = None, data: bytes | None = None,
                     filename: str = "image.png", mime_type: str | None = None) -> Attachment:
        if path is not None:
            return await asyncio.to_thread(self._attachments.put_file, path)
        if data is None:
            raise ValueError("upload needs a path or data")
        return self._attachments.put(data, filename, mime_type)

    def _deadline(self, request: ChatRequest) -> float:
        return time.monotonic() + (request.timeout_s or self._request_timeout_s)

    async def chat(self, request: ChatRequest) -> ChatResult:
        deadline = self._deadline(request)
        started = time.monotonic()
        attempts: list[Attempt] = []
        last_error: BackendError | None = None
        for route in self._routes:
            name = route.backend.name
            if not route.breaker.allow():
                attempts.append(Attempt(name, False, "breaker_open",
                                        f"circuit open for {route.breaker.seconds_until_probe():.0f}s more", 0))
                continue
            for attempt_no in range(1, self._retry.max_attempts + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GatewayError(ErrorKind.BACKEND_DOWN, "request deadline exceeded", attempts)
                t0 = time.monotonic()
                try:
                    reply = await route.backend.chat(request, remaining)
                except BackendError as exc:
                    attempts.append(Attempt(name, False, str(exc.kind), exc.message,
                                            int((time.monotonic() - t0) * 1000), exc.status))
                    logger.warning("%s attempt %d failed: %s", name, attempt_no, exc.message,
                                   extra={"backend": name, "task_id": request.metadata.get("task_id")})
                    if exc.kind is ErrorKind.CLIENT_ERROR:
                        route.breaker.record_success()  # the backend answered; it is healthy
                        raise GatewayError(ErrorKind.CLIENT_ERROR, exc.message, attempts) from exc
                    if exc.kind in RETRY_SAME_BACKEND_KINDS and attempt_no < self._retry.max_attempts:
                        delay = self._retry.delay(attempt_no)
                        if time.monotonic() + delay < deadline:
                            await asyncio.sleep(delay)
                            continue
                    if exc.kind in BREAKER_PENALTY_KINDS:
                        route.breaker.record_failure()
                    else:
                        route.breaker.release_probe()
                    self._restart_if_dead(route, exc)
                    last_error = exc
                    break
                else:
                    route.breaker.record_success()
                    attempts.append(Attempt(name, True, None, None, int((time.monotonic() - t0) * 1000), 200))
                    return ChatResult(content=reply.content, reasoning=reply.reasoning, model=reply.model,
                                      backend=name, usage=reply.usage,
                                      latency_ms=int((time.monotonic() - started) * 1000), attempts=attempts)
        kind = last_error.kind if last_error else ErrorKind.BACKEND_DOWN
        raise GatewayError(kind, "no Gemini backend could serve the request", attempts)

    async def stream(self, request: ChatRequest) -> AsyncIterator[tuple[str, StreamDelta]]:
        deadline = self._deadline(request)
        attempts: list[Attempt] = []
        last_error: BackendError | None = None
        for route in self._routes:
            name = route.backend.name
            if not route.breaker.allow():
                attempts.append(Attempt(name, False, "breaker_open", "circuit open", 0))
                continue
            emitted = False
            t0 = time.monotonic()
            try:
                async for delta in route.backend.stream(request, max(0.0, deadline - time.monotonic())):
                    emitted = True
                    yield name, delta
            except BackendError as exc:
                attempts.append(Attempt(name, False, str(exc.kind), exc.message,
                                        int((time.monotonic() - t0) * 1000), exc.status))
                if exc.kind is ErrorKind.CLIENT_ERROR:
                    route.breaker.record_success()
                    raise GatewayError(ErrorKind.CLIENT_ERROR, exc.message, attempts) from exc
                if exc.kind in BREAKER_PENALTY_KINDS:
                    route.breaker.record_failure()
                else:
                    route.breaker.release_probe()
                self._restart_if_dead(route, exc)
                if emitted:
                    # Part of the answer already reached the caller; switching backends would splice two answers.
                    raise GatewayError(exc.kind, f"stream from {name} broke mid-answer: {exc.message}", attempts) from exc
                last_error = exc
                continue
            route.breaker.record_success()
            return
        kind = last_error.kind if last_error else ErrorKind.BACKEND_DOWN
        raise GatewayError(kind, "no Gemini backend could serve the stream", attempts)
