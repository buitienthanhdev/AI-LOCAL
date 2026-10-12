"""Application-neutral picture description: what to draw, not how.

A `Scene` is an ordered list of filled and/or stroked polygons in "sheet units": x runs 0..1000, y runs
0..`height` (562.5 for the 16:9 sheet every backend maps onto its own canvas, slide, page or drawing). Later
shapes are painted over earlier ones. Everything that can be drawn (a traced photograph, an SVG written by the
planner, a template, hand-placed objects) is reduced to this form, and every drawing application is driven from it
by one backend that knows its own strengths (filled regions, lines only, true colour or a small palette).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

RGB = tuple[int, int, int]
Point = tuple[float, float]

SHEET_WIDTH = 1000.0
SHEET_HEIGHT = 562.5  # 16:9
MAX_SHAPES = 3000
MAX_VERTICES = 150_000


@dataclass(frozen=True)
class Shape:
    points: tuple[Point, ...]
    closed: bool = True
    fill: RGB | None = None
    stroke: RGB | None = None
    width: float = 1.0  # stroke width in sheet units
    hint: str | None = None  # "rect" | "ellipse" | "line": an exact primitive a backend may draw natively

    def bounds(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def visible(self) -> bool:
        return self.fill is not None or self.stroke is not None


@dataclass
class Scene:
    shapes: list[Shape]
    width: float = SHEET_WIDTH
    height: float = SHEET_HEIGHT
    background: RGB | None = None
    notes: list[str] = field(default_factory=list)  # things that were approximated or skipped, for the report

    @property
    def vertex_count(self) -> int:
        return sum(len(s.points) for s in self.shapes)

    def colors(self) -> list[RGB]:
        seen: dict[RGB, None] = {}
        for s in self.shapes:
            for c in (s.fill, s.stroke):
                if c is not None:
                    seen.setdefault(c, None)
        return list(seen)

    def stats(self) -> dict[str, int]:
        return {"shapes": len(self.shapes), "vertices": self.vertex_count, "colors": len(self.colors()),
                "filled": sum(1 for s in self.shapes if s.fill is not None),
                "stroked": sum(1 for s in self.shapes if s.stroke is not None)}


def hex_of(rgb: RGB) -> str:
    return "#%02X%02X%02X" % rgb


def luminance(rgb: RGB) -> float:
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255.0


def render(scene: Scene, width_px: int, height_px: int | None = None, *, supersample: int = 2) -> Image.Image:
    """The scene as pixels (white where nothing is painted): the reference image backends are verified against."""
    height_px = height_px or round(width_px * scene.height / scene.width)
    k = width_px / scene.width * supersample
    size = (round(width_px * supersample), round(height_px * supersample))
    image = Image.new("RGB", size, scene.background or (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for shape in scene.shapes:
        pts = [(x * k, y * k) for x, y in shape.points]
        if len(pts) < 2:
            continue
        if shape.fill is not None and len(pts) >= 3:
            draw.polygon(pts, fill=shape.fill)
        if shape.stroke is not None:
            path = pts + [pts[0]] if shape.closed and len(pts) > 2 else pts
            w = max(1, round(shape.width * k))
            draw.line(path, fill=shape.stroke, width=w, joint="curve")
    if supersample > 1:
        image = image.resize((width_px, height_px), Image.Resampling.LANCZOS)
    return image


def simplify(points: list[Point], tolerance: float, closed: bool) -> list[Point]:
    """Douglas-Peucker; for a closed ring the two farthest-apart anchors split it first."""
    if len(points) <= 3 or tolerance <= 0:
        return points

    def dp(seq: list[Point]) -> list[Point]:
        if len(seq) <= 2:
            return seq
        (x0, y0), (x1, y1) = seq[0], seq[-1]
        dx, dy = x1 - x0, y1 - y0
        norm = math.hypot(dx, dy)
        best, idx = -1.0, 0
        for i in range(1, len(seq) - 1):
            x, y = seq[i]
            d = (abs(dy * (x - x0) - dx * (y - y0)) / norm) if norm > 1e-12 else math.hypot(x - x0, y - y0)
            if d > best:
                best, idx = d, i
        if best <= tolerance:
            return [seq[0], seq[-1]]
        return dp(seq[: idx + 1])[:-1] + dp(seq[idx:])

    if not closed:
        return dp(points)
    a = 0
    b = max(range(len(points)), key=lambda i: (points[i][0] - points[a][0]) ** 2 + (points[i][1] - points[a][1]) ** 2)
    first = points[a: b + 1] if a <= b else points[a:] + points[: b + 1]
    second = (points[b:] + points[: a + 1]) if b >= a else points[b: a + 1]
    return dp(first)[:-1] + dp(second)[:-1]
