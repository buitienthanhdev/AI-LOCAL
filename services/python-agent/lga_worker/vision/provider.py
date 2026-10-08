"""VisionProvider: the perception fallback when UI Automation is not enough.

Perception order used by the agent (docs/architecture.md):
  1. accessibility tree (desktop.inspect_ui)   2. semantic lookup (desktop.find_element)
  3. OCR (vision.ocr, Windows built-in)          4. screenshot + vision model (vision.locate / vision.describe)
  5. coordinate action (desktop.click x,y)
This provider only perceives; it never clicks. Every located point is cross-checked against UI
Automation (`ControlFromPoint`) so the planner sees which real element lies under it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from lga_common.config import Config

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.vision import ocr
from lga_worker.vision.backends import GeminiGrounding, Grounding, GroundingBackend, UiTarsGrounding, free_vram_mb
from lga_worker.vision.gateway_client import GatewayClient
from lga_worker.vision.ui_tars_parser import IMAGE_FACTOR, MIN_PIXELS, smart_resize
from lga_worker.windows.provider import Rect, ScreenshotResult
from lga_worker.windows.windows_use_provider import WindowsUseProvider


@dataclass(frozen=True)
class PreparedImage:
    path: Path
    width: int
    height: int
    # Screen rectangle the image shows.
    screen: Rect


@dataclass(frozen=True)
class Located:
    x: int
    y: int
    grounding: Grounding
    image: PreparedImage
    element_under_point: dict[str, Any] | None


class VisionProvider:
    def __init__(self, config: Config, desktop: WindowsUseProvider) -> None:
        self._config = config
        self._desktop = desktop
        self._backend_name = str(config.get("vision.backend"))
        self._max_pixels = int(config.get("vision.max_pixels"))
        self._timeout_s = config.seconds("vision.grounding_timeout_ms")
        self._gateway: GatewayClient | None = None

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def gateway(self) -> GatewayClient:
        if self._gateway is None:
            self._gateway = GatewayClient(self._config)
        return self._gateway

    def grounding_backend(self) -> GroundingBackend:
        if self._backend_name == "gemini":
            return GeminiGrounding(self.gateway())
        if self._backend_name == "ui_tars":
            return UiTarsGrounding(str(self._config.get("vision.ui_tars_base_url")),
                                   str(self._config.get("vision.ui_tars_model")),
                                   int(self._config.get("vision.ui_tars_min_free_vram_mb")))
        raise ToolError(ErrorCode.UNSUPPORTED, f"vision backend {self._backend_name!r} is disabled or unknown")

    async def status(self) -> tuple[bool, dict[str, Any], str]:
        info: dict[str, Any] = {"backend": self._backend_name}
        try:
            info["ocr_languages"] = await asyncio.to_thread(ocr.available_languages)
        except ToolError as exc:
            info["ocr_languages"] = []
            info["ocr_error"] = exc.message
        if self._backend_name == "gemini":
            try:
                health = await asyncio.to_thread(self.gateway().health)
            except ToolError as exc:
                return False, {**info, "error": exc.message}, f"gemini vision unavailable: {exc.message}"
            capable = [b["name"] for b in health.get("backends", []) if b.get("capabilities", {}).get("images")]
            info["image_capable_backends"] = capable
            if capable:
                return True, info, f"gemini vision via {', '.join(capable)}; OCR languages {info['ocr_languages']}"
            return False, info, ("no Gemini backend accepts images (sign in a Google account for the gateway; "
                                 f"see docs/operations.md); OCR languages {info['ocr_languages']}")
        if self._backend_name == "ui_tars":
            free = await asyncio.to_thread(free_vram_mb)
            info["free_vram_mb"] = free
            needed = int(self._config.get("vision.ui_tars_min_free_vram_mb"))
            ready = free is None or free >= needed
            return ready, info, f"ui_tars backend; free VRAM {free} MB (needs {needed})"
        return False, info, f"vision backend {self._backend_name!r} disabled; OCR languages {info['ocr_languages']}"

    async def capture(self, directory: Path, stem: str, hwnd: int | None, region: Rect | None) -> ScreenshotResult:
        path = directory / f"{stem}-{int(time.time() * 1000)}.png"
        return await self._desktop.executor.run(self._timeout_s, self._desktop.take_screenshot, path, region, hwnd)

    def prepare(self, shot: ScreenshotResult, for_ui_tars: bool) -> PreparedImage:
        """Downscale to the pixel budget. UI-TARS needs exactly its smart_resize dimensions."""
        screen = Rect(shot.origin_x, shot.origin_y, shot.origin_x + shot.width, shot.origin_y + shot.height)
        if for_ui_tars:
            height, width = smart_resize(shot.height, shot.width, IMAGE_FACTOR, MIN_PIXELS, self._max_pixels)
        elif shot.width * shot.height > self._max_pixels:
            scale = (self._max_pixels / (shot.width * shot.height)) ** 0.5
            width, height = max(1, int(shot.width * scale)), max(1, int(shot.height * scale))
        else:
            return PreparedImage(shot.path, shot.width, shot.height, screen)
        resized_path = shot.path.with_name(shot.path.stem + f"-{width}x{height}.png")
        with Image.open(shot.path) as image:
            image.resize((width, height), Image.Resampling.LANCZOS).save(resized_path, format="PNG")
        return PreparedImage(resized_path, width, height, screen)

    def _element_at(self, x: int, y: int) -> dict[str, Any] | None:
        control = self._desktop.uia.ControlFromPoint(x, y)
        if control is None:
            return None
        return {"name": control.Name or "", "control_type": control.ControlTypeName or "",
                "automation_id": control.AutomationId or ""}

    async def locate(self, directory: Path, stem: str, description: str, hwnd: int | None) -> Located:
        backend = self.grounding_backend()
        shot = await self.capture(directory, stem, hwnd, None)
        image = await asyncio.to_thread(self.prepare, shot, isinstance(backend, UiTarsGrounding))
        grounding = await asyncio.to_thread(backend.locate, image.path, image.width, image.height, description,
                                            self._timeout_s)
        if not grounding.found:
            raise ToolError(ErrorCode.ELEMENT_NOT_FOUND,
                            f"vision could not find {description!r} on screen: {grounding.label or 'not visible'}",
                            details={"backend": grounding.backend, "screenshot": str(shot.path)})
        x = image.screen.left + round(grounding.x * image.screen.width)
        y = image.screen.top + round(grounding.y * image.screen.height)
        element = await self._desktop.executor.run(self._timeout_s, self._element_at, x, y)
        return Located(x, y, grounding, image, element)

    async def describe(self, directory: Path, stem: str, question: str, hwnd: int | None) -> tuple[str, str, Path]:
        if self._backend_name != "gemini":
            raise ToolError(ErrorCode.UNSUPPORTED, "vision.describe needs the gemini vision backend")
        shot = await self.capture(directory, stem, hwnd, None)
        image = await asyncio.to_thread(self.prepare, shot, False)
        backend = GeminiGrounding(self.gateway())
        answer, used = await asyncio.to_thread(backend.describe, image.path, question, self._timeout_s)
        return answer, used, shot.path

    async def read_text(self, directory: Path, stem: str, hwnd: int | None,
                        region: Rect | None) -> tuple[list[ocr.OcrLine], str, ScreenshotResult]:
        shot = await self.capture(directory, stem, hwnd, region)
        lines, language = await ocr.recognize(shot.path)
        return lines, language, shot
