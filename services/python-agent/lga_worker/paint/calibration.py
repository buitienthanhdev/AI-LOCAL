"""Canvas <-> screen coordinate transform for Paint.

Inputs come from Paint's own UI Automation tree (verified on Paint 11.2605 with the Doraemon run's window):

* `CanvasSizeTextBlock` reads "1152 × 648px": the canvas size in pixels;
* the `image` element's rectangle is the canvas as displayed (so rectangle width / canvas width is the zoom);
* `ZoomValuesComboBox` reads "100%".

x_s = x_0 + s * x_c,  y_s = y_0 + s * y_c  (x_0, y_0: screen position of canvas pixel (0, 0); s: zoom).
The transform is rebuilt from the UI every time it is needed and cross-checked against a screenshot, so a
moved window, a changed zoom or a scrolled canvas is noticed before drawing, not after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageChops

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint.model import Canvas
from lga_worker.windows.provider import Rect

ASPECT_TOLERANCE = 0.01  # displayed rectangle vs canvas aspect ratio
WHITE_MIN = 253
ROW_FILL_MIN = 0.5  # a canvas row/column counts when this share of it is still white
SCAN_TOLERANCE_PX = 2

_SIZE = re.compile(r"(\d+)\s*[×xX]\s*(\d+)")
_ZOOM = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def parse_canvas_size(text: str) -> Canvas:
    match = _SIZE.search(text or "")
    if not match:
        raise ToolError(ErrorCode.EXECUTION_FAILED, f"cannot read the canvas size from {text!r}")
    return Canvas(int(match.group(1)), int(match.group(2)))


def parse_zoom(text: str) -> float:
    """'100%' -> 1.0."""
    match = _ZOOM.search(text or "")
    if not match:
        raise ToolError(ErrorCode.EXECUTION_FAILED, f"cannot read the zoom from {text!r}")
    return float(match.group(1).replace(",", ".")) / 100.0


@dataclass(frozen=True)
class CanvasTransform:
    canvas: Canvas
    origin_x: float  # screen x of canvas pixel 0
    origin_y: float
    scale: float  # screen pixels per canvas pixel

    @classmethod
    def from_rect(cls, canvas: Canvas, shown: Rect, *, zoom_hint: float | None = None) -> CanvasTransform:
        """The canvas as displayed in `shown` (the `image` element's rectangle, physical screen pixels)."""
        if shown.is_empty():
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, "the Paint canvas is not visible (window minimised?)")
        sx, sy = shown.width / canvas.width, shown.height / canvas.height
        if abs(sx - sy) / max(sx, sy) > ASPECT_TOLERANCE:
            raise ToolError(ErrorCode.EXECUTION_FAILED,
                            f"displayed canvas {shown.width}x{shown.height} does not match {canvas.width}x{canvas.height}"
                            f" (scale {sx:.3f} vs {sy:.3f}); the canvas rectangle is not the page",
                            details={"rect": shown.to_dict()})
        scale = (sx + sy) / 2
        if zoom_hint is not None and abs(scale - zoom_hint) > 0.02 * max(zoom_hint, 1e-6):
            raise ToolError(ErrorCode.EXECUTION_FAILED,
                            f"canvas rectangle implies zoom {scale:.2%} but Paint says {zoom_hint:.2%}",
                            details={"rect": shown.to_dict()})
        return cls(canvas, float(shown.left), float(shown.top), scale)

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return round(self.origin_x + self.scale * x), round(self.origin_y + self.scale * y)

    def to_canvas(self, sx: float, sy: float) -> tuple[float, float]:
        return (sx - self.origin_x) / self.scale, (sy - self.origin_y) / self.scale

    def contains_screen(self, sx: int, sy: int) -> bool:
        x, y = self.to_canvas(sx, sy)
        return 0 <= x < self.canvas.width and 0 <= y < self.canvas.height

    @property
    def screen_rect(self) -> Rect:
        left, top = round(self.origin_x), round(self.origin_y)
        return Rect(left, top, left + round(self.canvas.width * self.scale), top + round(self.canvas.height * self.scale))

    def same_place_as(self, other: CanvasTransform, tolerance_px: float = 1.0) -> bool:
        return (self.canvas == other.canvas and abs(self.scale - other.scale) < 1e-3
                and abs(self.origin_x - other.origin_x) <= tolerance_px
                and abs(self.origin_y - other.origin_y) <= tolerance_px)


def _runs(profile: list[int], threshold: float) -> tuple[int, int] | None:
    """First and last index (inclusive) of the longest run of values >= threshold."""
    best: tuple[int, int] | None = None
    start: int | None = None
    for i, value in enumerate([*profile, -1]):
        if value >= threshold and start is None:
            start = i
        elif value < threshold and start is not None:
            if best is None or (i - 1 - start) > (best[1] - best[0]):
                best = (start, i - 1)
            start = None
    return best


def find_blank_canvas(screenshot: Image.Image, canvas: Canvas, scale: float) -> tuple[int, int] | None:
    """Top-left pixel (in `screenshot` coordinates) of a mostly white canvas-sized area, or None.

    Cross-check for a freshly opened, still blank canvas: the UI rectangle must agree with where the white page
    really is on screen. Uses row/column profiles of pure-white pixels, so UI chrome and the window's pale
    background gradient do not matter.
    """
    width, height = round(canvas.width * scale), round(canvas.height * scale)
    rgb = screenshot.convert("RGB")
    white = _all_white(*rgb.split())
    col = list(white.resize((rgb.width, 1), Image.Resampling.BOX).tobytes())
    row = list(white.resize((1, rgb.height), Image.Resampling.BOX).tobytes())
    # the page occupies `height` rows of which each is `width`/screen-width white, and vice versa
    rows = _runs([1 if value / 255.0 * rgb.width >= width * ROW_FILL_MIN else 0 for value in row], 1)
    cols = _runs([1 if value / 255.0 * rgb.height >= height * ROW_FILL_MIN else 0 for value in col], 1)
    if rows is None or cols is None:
        return None
    found_h, found_w = rows[1] - rows[0] + 1, cols[1] - cols[0] + 1
    if abs(found_h - height) > SCAN_TOLERANCE_PX + 1 or abs(found_w - width) > SCAN_TOLERANCE_PX + 1:
        return None
    return cols[0], rows[0]


def _all_white(r: Image.Image, g: Image.Image, b: Image.Image) -> Image.Image:
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    return low.point(lambda v: 255 if v >= WHITE_MIN else 0)


def cross_check(transform: CanvasTransform, screenshot: Image.Image, screenshot_origin: tuple[int, int]) -> float | None:
    """Distance in screen pixels between the UI-derived canvas position and the white page found in a screenshot
    taken of a blank canvas. None when the page cannot be found (not blank, or off screen)."""
    found = find_blank_canvas(screenshot, transform.canvas, transform.scale)
    if found is None:
        return None
    fx, fy = found[0] + screenshot_origin[0], found[1] + screenshot_origin[1]
    return max(abs(fx - transform.origin_x), abs(fy - transform.origin_y))
