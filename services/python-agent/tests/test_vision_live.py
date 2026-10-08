"""TEST 3 (live): screenshot -> locate target visually -> click -> verify. Drives the real desktop.

Run: uv run pytest -m desktop --run-desktop -v -s tests/test_vision_live.py
Target: the "File" menu of a Notepad window opened on a temporary file. Locating uses Windows OCR on a
screenshot (no UI Automation); verification uses UI Automation (the File menu's items must appear).
vision.locate with a model is exercised when an image-capable backend is ready, otherwise reported as
BLOCKED with the reason.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from lga_common.config import Config
from lga_worker.registry import ToolContext
from lga_worker.server import build_registry
from lga_worker.services import Services

pytestmark = pytest.mark.desktop
MENU_ITEMS = {"new tab", "new window", "open", "save", "save as", "print", "exit", "new"}


def _ctx(config: Config, services: Services) -> ToolContext:
    return ToolContext(uuid.uuid4().hex, "test-vision", "t3", time.monotonic() + 240, config, services,
                       threading.Event())


def test_vision_locate_click_verify(config: Config, tmp_path: Path, capsys) -> None:
    async def scenario() -> list[str]:
        registry = build_registry(config.path("paths.tools_dir"))
        services = Services(config)
        report: list[str] = []

        async def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            _, outcome = await registry.invoke(_ctx(config, services), tool, args)
            return outcome.data

        try:
            target = tmp_path / "vision-test.txt"
            target.write_text("vision test", encoding="utf-8")
            window = (await call("desktop.launch_app", {"target": "notepad.exe", "arguments": [str(target)],
                                                        "expect_title": "vision-test"}))["window"]
            await call("desktop.focus_window", {"hwnd": window["hwnd"]})
            shot = await call("desktop.screenshot", {"hwnd": window["hwnd"]})
            assert Path(shot["path"]).is_file()

            found = await call("vision.ocr", {"hwnd": window["hwnd"], "find_text": "File"})
            menu_lines = [line for line in found["lines"] if line["text"].strip().lower() == "file"]
            assert menu_lines, f"OCR did not find the File menu: {found['lines']}"
            point = menu_lines[0]
            report.append(f"OCR located 'File' at ({point['center_x']},{point['center_y']})")

            await call("desktop.click", {"x": point["center_x"], "y": point["center_y"]})
            items = (await call("desktop.find_element", {"control_type": "MenuItem", "max_results": 30}))["elements"]
            names = {item["name"].strip().lower() for item in items}
            assert names & MENU_ITEMS, f"File menu did not open; menu items seen: {sorted(names)}"
            report.append(f"click verified through UI Automation: menu items {sorted(names & MENU_ITEMS)}")
            await call("desktop.press_key", {"key": "esc"})

            status = await call("vision.status", {})
            if status["ready"]:
                located = await call("vision.locate", {"description": "the File menu in the top-left of Notepad",
                                                       "hwnd": window["hwnd"]})
                under = located.get("element_under_point") or {}
                report.append(f"vision.locate ({located['backend']}) -> ({located['x']},{located['y']}), "
                              f"UIA element under point: {under}")
            else:
                report.append(f"vision.locate BLOCKED: {status}")
            await call("desktop.hotkey", {"keys": ["ctrl", "w"]})
            return report
        finally:
            await services.close()

    report = asyncio.run(scenario())
    with capsys.disabled():
        print("\nTEST 3:\n  " + "\n  ".join(report))
