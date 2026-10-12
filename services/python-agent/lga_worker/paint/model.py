"""Data model of the Paint drawing engine.

The planner never sees pixels. It describes a picture in *composition units*: x runs 0..1000 across the canvas
width and y runs 0..`Canvas.unit_height` (1000 * height / width), so one unit is the same length horizontally
and vertically. The geometry engine (`geometry.plan_scene`) turns units into canvas pixels, validates them and
orders the objects; nothing in this package touches the screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RGB = tuple[int, int, int]
Point = tuple[float, float]

CLOSED_KINDS = frozenset({"rect", "ellipse", "triangle", "polygon"})
OPEN_KINDS = frozenset({"line", "polyline", "curve", "path"})
# A bucket fill: one point (the seed) and a fill colour. It only makes sense after the outline that closes the region.
FILL_KIND = "fill"
KINDS = CLOSED_KINDS | OPEN_KINDS | {FILL_KIND}
# Number of points each kind takes: (minimum, maximum or None). rect/ellipse/triangle are given by two opposite
# corners of their bounding box; a triangle is isosceles with its apex at the top centre.
POINT_COUNTS: dict[str, tuple[int, int | None]] = {
    "rect": (2, 2), "ellipse": (2, 2), "triangle": (2, 2), "line": (2, 2),
    "polygon": (3, None), "polyline": (2, None), "curve": (3, None), "path": (2, None), "fill": (1, 1),
}

# Stroke weight classes, as a multiple of the base stroke (docs: sketch/detail 1x, structure 1.5-2x, accent 2-3x).
WEIGHT_FACTORS: dict[str, float] = {"detail": 1.0, "structure": 1.75, "accent": 2.5}
DEFAULT_WEIGHT = "structure"


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int

    @property
    def unit_height(self) -> float:
        return 1000.0 * self.height / self.width

    @property
    def px_per_unit(self) -> float:
        return self.width / 1000.0


@dataclass(frozen=True)
class DrawingObject:
    """One thing to draw, in composition units. Colours are '#RRGGBB' or a name from `geometry.NAMED_COLORS`."""

    id: str
    kind: str
    points: tuple[Point, ...]
    fill: str | None = None
    stroke: str | None = "#000000"
    weight: str = DEFAULT_WEIGHT
    layer: int = 0
    width_px: int | None = None  # an exact brush/outline width in canvas px (1..90); overrides `weight`
    brush: str | None = None  # a Paint brush type ("Oil brush"...) for path/curve objects
    region: str | None = None  # fill only: the closed object to shape-fill instead when the bucket would leak


@dataclass(frozen=True)
class PlannedObject:
    """A `DrawingObject` resolved to canvas pixels, ready to execute and to verify."""

    id: str
    kind: str
    points: tuple[Point, ...]  # canvas pixels (float); see `outline` for the drawn path
    outline: tuple[Point, ...]  # the path that is stroked (closed kinds repeat nothing; the closing edge is implied)
    fill: RGB | None
    stroke: RGB | None
    stroke_px: int
    layer: int
    order: int  # draw order: by layer, then by position in the request
    brush: str | None = None
    region: str | None = None

    @property
    def closed(self) -> bool:
        return self.kind in CLOSED_KINDS

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Plan:
    canvas: Canvas
    objects: list[PlannedObject]  # already in draw order
    base_stroke_px: int
    margin_px: int
    warnings: list[str] = field(default_factory=list)

    def by_id(self, object_id: str) -> PlannedObject:
        for obj in self.objects:
            if obj.id == object_id:
                return obj
        raise KeyError(object_id)

    def layers(self) -> list[int]:
        return sorted({o.layer for o in self.objects})
