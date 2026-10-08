"""Lazily created execution providers shared by all tool handlers in one worker process."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lga_common.config import Config

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.windows.windows_use_provider import WindowsUseProvider

logger = logging.getLogger(__name__)

PROVIDER_INIT_TIMEOUT_S = 60.0


class Services:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._desktop: WindowsUseProvider | None = None
        self._extra: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def desktop(self) -> WindowsUseProvider:
        if self._desktop is not None:
            return self._desktop
        async with self._lock("desktop"):
            if self._desktop is None:
                provider = WindowsUseProvider(self.config)
                try:
                    await provider.executor.run(PROVIDER_INIT_TIMEOUT_S, provider.initialize)
                except ToolError:
                    raise
                except Exception as exc:
                    raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"Windows provider failed to start: {exc}") from exc
                self._desktop = provider
                logger.info("desktop provider ready (Windows-Use)")
        return self._desktop

    async def get_or_create(self, name: str, factory) -> Any:
        """Create an optional provider (browser, vision, AutoCAD) once; `factory` is an async callable."""
        if name in self._extra:
            return self._extra[name]
        async with self._lock(name):
            if name not in self._extra:
                self._extra[name] = await factory()
        return self._extra[name]

    def peek(self, name: str) -> Any | None:
        return self._extra.get(name)

    def forget(self, name: str) -> None:
        self._extra.pop(name, None)

    async def close(self) -> None:
        for name, provider in list(self._extra.items()):
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - shutdown must continue; failure is logged with traceback
                    logger.exception("closing %s failed", name)
        self._extra.clear()
        if self._desktop is not None:
            self._desktop.executor.shutdown()
            self._desktop = None
