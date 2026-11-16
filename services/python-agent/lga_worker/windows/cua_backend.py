"""Mouse, keyboard and screen capture through Cua Driver (https://github.com/trycua/cua).

The driver is a Rust runtime loaded in-process from the `cua-driver` wheel. Its SDK is async, so a
private event loop runs on a daemon thread and every call here blocks until the driver answers.
All actions use desktop scope (screen-absolute coordinates) and foreground delivery: the same real
mouse/keyboard behaviour the old UIA SendInput path had, so the provider's safety checks still apply.

Set LGA_INPUT_BACKEND=uia to go back to the old uiautomation input path.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import threading
from typing import Any

from lga_worker.errors import ErrorCode, ToolError

SESSION = "lga-desktop"
CALL_TIMEOUT_S = 60.0


def enabled() -> bool:
    return os.environ.get("LGA_INPUT_BACKEND", "cua").strip().lower() != "uia"


class CuaBackend:
    def __init__(self) -> None:
        from cua_driver import CuaDriver  # imported lazily: the wheel loads a native DLL

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="cua-driver", daemon=True)
        self._thread.start()
        self._driver = self._run(self._create(CuaDriver))
        self.call("start_session", {"capture_scope": "desktop"})
        try:  # the agent-cursor overlay would show up in screenshots and pixel verification
            self.call("set_agent_cursor_enabled", {"enabled": False})
        except ToolError:
            pass

    @staticmethod
    async def _create(cls: Any) -> Any:
        return cls.create()

    def _run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(CALL_TIMEOUT_S)

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        payload = {"session": SESSION, **args}
        result = self._run(self._driver.call_tool(tool, json.dumps(payload)))
        if result.is_error:
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"cua-driver {tool} failed: {''.join(result.text or '')}",
                            details={"error_code": result.error_code})
        return result

    def _desktop(self, tool: str, **args: Any) -> Any:
        return self.call(tool, {"scope": "desktop", "delivery_mode": "foreground", **args})

    # mouse -------------------------------------------------------------------------------------
    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self._desktop("click", x=x, y=y, button=button, count=count)

    def move(self, x: int, y: int) -> None:
        self.call("move_cursor", {"scope": "desktop", "x": x, "y": y})

    def drag(self, start: tuple[int, int], end: tuple[int, int], duration_s: float) -> None:
        self._desktop("drag", from_x=start[0], from_y=start[1], to_x=end[0], to_y=end[1],
                      duration_ms=max(int(duration_s * 1000), 1))

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        self._desktop("scroll", x=x, y=y, direction=direction, amount=amount, by="line")

    def cursor_position(self) -> tuple[int, int]:
        data = json.loads(self.call("get_cursor_position", {}).structured_json or "{}")
        return int(data["x"]), int(data["y"])

    # keyboard ----------------------------------------------------------------------------------
    def type_text(self, text: str, delay_ms: int) -> None:
        # The driver types "\n" as nothing; line breaks are sent as Enter like the old TypeText did.
        for i, line in enumerate(text.replace("\r\n", "\n").split("\n")):
            if i:
                self.press_key("enter")
            if line:
                self._desktop("type_text", text=line, delay_ms=delay_ms)

    def press_key(self, key: str) -> None:
        self._desktop("press_key", key=key)

    def hotkey(self, keys: list[str]) -> None:
        self._desktop("hotkey", keys=keys)

    # screen ------------------------------------------------------------------------------------
    def screenshot(self) -> Any:
        """Full desktop as a PIL image."""
        from PIL import Image

        result = self.call("get_desktop_state", {})
        if not result.images:
            raise ToolError(ErrorCode.EXECUTION_FAILED, "cua-driver returned no screenshot")
        return Image.open(io.BytesIO(base64.b64decode(result.images[0].data_base64))).convert("RGB")
