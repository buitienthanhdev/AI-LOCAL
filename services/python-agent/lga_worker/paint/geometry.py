"""Deterministic geometry for the Paint engine: composition units -> validated canvas-pixel plan.

Everything here is pure arithmetic, so a wrong request fails with a message the planner can fix (which object,
which rule, the allowed range) before a single mouse event is sent.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint.model import (
    CLOSED_KINDS, DEFAULT_WEIGHT, KINDS, POINT_COUNTS, RGB, WEIGHT_FACTORS, Canvas, DrawingObject, Plan,
    PlannedObject, Point,
)

MARGIN_FRACTION = 0.04  # of the shorter canvas side: empty border kept around the picture
MIN_OBJECT_PX = 3.0  # an object thinner than this is a mistake, not a detail
BASE_STROKE_DIVISOR = 324  # base stroke = round(height / 324): 2 px on the default 648 px canvas
CURVE_STEP_PX = 4.0
ELLIPSE_MIN_SAMPLES = 48
SHAPE_WIDTHS = (1, 3, 5, 8)  # Paint 11's Size menu for shapes (Size1/Size3/Size5/Size8), measured
EPS = 1e-6

NAMED_COLORS: dict[str, RGB] = {
    "black": (0, 0, 0), "white": (255, 255, 255), "grey": (127, 127, 127), "gray": (127, 127, 127),
    "red": (237, 28, 36), "orange": (255, 127, 39), "yellow": (255, 242, 0), "green": (34, 177, 76),
    "blue": (0, 150, 224), "dark_blue": (0, 70, 160), "sky": (153, 217, 234), "purple": (163, 73, 164),
    "pink": (255, 174, 201), "brown": (136, 72, 20), "gold": (255, 201, 14), "skin": (255, 214, 170),
    "den": (0, 0, 0), "trang": (255, 255, 255), "do": (237, 28, 36), "vang": (255, 242, 0),
    "xanh_la": (34, 177, 76), "xanh_duong": (0, 150, 224), "cam": (255, 127, 39), "hong": (255, 174, 201),
    "nau": (136, 72, 20), "tim": (163, 73, 164), "xam": (127, 127, 127),
}
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value: str, *, what: str = "color") -> RGB:
    text = value.strip()
    match = _HEX.match(text)
    if match:
        raw = match.group(1)
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    key = text.lower().replace(" ", "_").replace("-", "_")
    if key in NAMED_COLORS:
        return NAMED_COLORS[key]
    raise ToolError(ErrorCode.INVALID_ARGUMENT,
                    f"{what} {value!r} is not '#RRGGBB' or one of: {', '.join(sorted(NAMED_COLORS))}")


def object_from_dict(data: Mapping[str, Any]) -> DrawingObject:
    """A tool-call object ({'id', 'kind', 'points': [[x, y], ...], 'fill', 'stroke', 'weight', 'layer'})."""
    try:
        points = tuple((float(p[0]), float(p[1])) for p in data["points"])
        return DrawingObject(
            id=str(data["id"]), kind=str(data["kind"]), points=points, fill=data.get("fill"),
            stroke=data.get("stroke", "#000000"), weight=str(data.get("weight") or DEFAULT_WEIGHT),
            layer=int(data.get("layer", 0)),
            width_px=int(data["width_px"]) if data.get("width_px") else None,
            brush=str(data["brush"]) if data.get("brush") else None,
            region=str(data["region"]) if data.get("region") else None,
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"malformed object {dict(data)!r}: {exc!r}") from exc


def base_stroke_px(canvas: Canvas) -> int:
    return max(1, round(canvas.height / BASE_STROKE_DIVISOR))


def snap_width(width: float) -> int:
    """The nearest outline width Paint offers for shapes (ties go to the thinner one)."""
    return min(SHAPE_WIDTHS, key=lambda w: (abs(w - width), w))


def stroke_px(canvas: Canvas, weight: str) -> int:
    factor = WEIGHT_FACTORS.get(weight)
    if factor is None:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        f"unknown weight {weight!r}; use one of {', '.join(WEIGHT_FACTORS)}")
    return snap_width(base_stroke_px(canvas) * factor)


# --- outlines ----------------------------------------------------------------------------------------------


def catmull_rom(points: Sequence[Point], step_px: float = CURVE_STEP_PX) -> list[Point]:
    """A smooth open curve through every point (centripetal-free uniform Catmull-Rom), sampled about every `step_px`."""
    if len(points) < 3:
        return list(points)
    padded = [points[0], *points, points[-1]]
    out: list[Point] = [points[0]]
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i - 1], padded[i], padded[i + 1], padded[i + 2]
        length = math.dist(p1, p2)
        n = max(2, math.ceil(length / step_px))
        for k in range(1, n + 1):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * ((2 * p1[a]) + (-p0[a] + p2[a]) * t + (2 * p0[a] - 5 * p1[a] + 4 * p2[a] - p3[a]) * t2
                       + (-p0[a] + 3 * p1[a] - 3 * p2[a] + p3[a]) * t3)
                for a in (0, 1)))  # type: ignore[arg-type]
    return out


def ellipse_points(box: tuple[float, float, float, float]) -> list[Point]:
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
    perimeter = math.pi * (3 * (rx + ry) - math.sqrt((3 * rx + ry) * (rx + 3 * ry)))
    n = max(ELLIPSE_MIN_SAMPLES, math.ceil(perimeter / CURVE_STEP_PX))
    return [(cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n)) for k in range(n)]


def outline_of(kind: str, points: Sequence[Point]) -> list[Point]:
    """The stroked path of an object given its (pixel) points. Closed kinds do not repeat the first point."""
    if kind in ("rect", "ellipse", "triangle"):
        (xa, ya), (xb, yb) = points[0], points[1]
        x0, x1, y0, y1 = min(xa, xb), max(xa, xb), min(ya, yb), max(ya, yb)
        if kind == "rect":
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if kind == "triangle":
            return [((x0 + x1) / 2, y0), (x1, y1), (x0, y1)]
        return ellipse_points((x0, y0, x1, y1))
    if kind == "curve":
        return catmull_rom(points)
    return list(points)


def bounds_of(points: Iterable[Point]) -> tuple[float, float, float, float]:
    pts = list(points)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# --- planning ----------------------------------------------------------------------------------------------


def plan_scene(objects: Sequence[DrawingObject], canvas: Canvas, *, margin_fraction: float = MARGIN_FRACTION,
               check_overlaps: bool = True) -> Plan:
    """Validate `objects` and resolve them to canvas pixels in draw order (layer, then request order)."""
    if not objects:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, "the scene has no objects")
    scale = canvas.px_per_unit
    margin_px = max(1, round(margin_fraction * min(canvas.width, canvas.height)))
    lo_x, hi_x = margin_px / scale, 1000.0 - margin_px / scale
    lo_y, hi_y = margin_px / scale, canvas.unit_height - margin_px / scale
    allowed = f"x in [{lo_x:.0f}, {hi_x:.0f}], y in [{lo_y:.0f}, {hi_y:.0f}]"

    seen: set[str] = set()
    planned: list[PlannedObject] = []
    for index, obj in enumerate(objects):
        if obj.id in seen:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"duplicate object id {obj.id!r}")
        seen.add(obj.id)
        if obj.kind not in KINDS:
            raise ToolError(ErrorCode.INVALID_ARGUMENT,
                            f"object {obj.id!r}: unknown kind {obj.kind!r}; use one of {', '.join(sorted(KINDS))}")
        low, high = POINT_COUNTS[obj.kind]
        if len(obj.points) < low or (high is not None and len(obj.points) > high):
            need = f"{low}" if high == low else f"{low}+" if high is None else f"{low}-{high}"
            raise ToolError(ErrorCode.INVALID_ARGUMENT,
                            f"object {obj.id!r}: {obj.kind} needs {need} points, got {len(obj.points)}")
        for x, y in obj.points:
            if not (lo_x - EPS <= x <= hi_x + EPS and lo_y - EPS <= y <= hi_y + EPS):
                raise ToolError(ErrorCode.INVALID_ARGUMENT,
                                f"object {obj.id!r}: point ({x:g}, {y:g}) is outside the drawable area "
                                f"({allowed}); the canvas is 1000 x {canvas.unit_height:.0f} units")
        if obj.kind == "fill" and obj.fill is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"object {obj.id!r}: a fill needs a fill colour")
        if obj.fill is not None and obj.kind not in CLOSED_KINDS and obj.kind != "fill":
            raise ToolError(ErrorCode.INVALID_ARGUMENT,
                            f"object {obj.id!r}: a {obj.kind} is not closed and cannot have a fill")
        if obj.fill is None and obj.stroke is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"object {obj.id!r} has neither fill nor stroke")
        px_points = tuple((x * scale, y * scale) for x, y in obj.points)
        outline = tuple(outline_of(obj.kind, px_points))
        x0, y0, x1, y1 = bounds_of(outline)
        if obj.kind != "fill" and max(x1 - x0, y1 - y0) < MIN_OBJECT_PX:
            raise ToolError(ErrorCode.INVALID_ARGUMENT,
                            f"object {obj.id!r} is only {x1 - x0:.1f} x {y1 - y0:.1f} px; make it larger")
        if obj.kind in ("rect", "ellipse", "triangle") and min(x1 - x0, y1 - y0) < MIN_OBJECT_PX:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"object {obj.id!r} is flat; use a line instead")
        planned.append(PlannedObject(
            id=obj.id, kind=obj.kind, points=px_points, outline=outline,
            fill=parse_color(obj.fill, what=f"object {obj.id!r} fill") if obj.fill is not None else None,
            stroke=parse_color(obj.stroke, what=f"object {obj.id!r} stroke") if obj.stroke is not None else None,
            stroke_px=(max(1, min(90, int(obj.width_px))) if obj.width_px else stroke_px(canvas, obj.weight)),
            layer=obj.layer, order=index, brush=obj.brush, region=obj.region))
    planned.sort(key=lambda o: (o.layer, o.order))
    plan = Plan(canvas=canvas, objects=planned, base_stroke_px=base_stroke_px(canvas), margin_px=margin_px)
    if check_overlaps:
        plan.warnings.extend(overlap_warnings(planned))
    return plan


def _box_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float]:
    """Overlap area and the fraction of the smaller box it covers."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= EPS or h <= EPS:
        return 0.0, 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h, (w * h) / smaller if smaller > EPS else 0.0


def overlap_warnings(objects: Sequence[PlannedObject]) -> list[str]:
    """Filled objects of the same layer whose boxes partly overlap (neither contains the other): the later one
    would cut into the earlier by accident. Different layers overlap on purpose (face over head)."""
    warnings: list[str] = []
    filled = [o for o in objects if o.fill is not None and o.kind != "fill"]
    for i, a in enumerate(filled):
        for b in filled[i + 1:]:
            if a.layer != b.layer:
                continue
            _, fraction = _box_overlap(a.bounds, b.bounds)
            if 0.02 < fraction < 0.98:
                warnings.append(f"{a.id!r} and {b.id!r} are on the same layer and overlap partially; "
                                f"give one a higher layer or move them apart")
    return warnings


def interior_point(obj: PlannedObject) -> tuple[int, int] | None:
    """A pixel well inside a closed object (for a bucket-fill seed or a colour sample), or None for open ones."""
    if not obj.closed:
        return None
    x0, y0, x1, y1 = obj.bounds
    if obj.kind in ("rect", "ellipse"):
        return round((x0 + x1) / 2), round((y0 + y1) / 2)
    if obj.kind == "triangle":
        return round((x0 + x1) / 2), round(y0 + (y1 - y0) * 0.66)
    # polygon: centroid when inside, otherwise the middle of the widest horizontal chord at mid height
    cx = sum(p[0] for p in obj.outline) / len(obj.outline)
    cy = sum(p[1] for p in obj.outline) / len(obj.outline)
    if point_in_polygon((cx, cy), obj.outline):
        return round(cx), round(cy)
    best: tuple[float, float] | None = None
    best_y = y0
    for k in range(1, 10):
        y = y0 + (y1 - y0) * k / 10
        xs = sorted(_crossings(obj.outline, y))
        for a, b in zip(xs[0::2], xs[1::2], strict=False):
            if best is None or b - a > best[1] - best[0]:
                best = (a, b)
                best_y = y
    if best is None:
        return None
    return round((best[0] + best[1]) / 2), round(best_y)


def _crossings(poly: Sequence[Point], y: float) -> list[float]:
    xs: list[float] = []
    for (xa, ya), (xb, yb) in zip(poly, [*poly[1:], poly[0]], strict=True):
        if (ya <= y < yb) or (yb <= y < ya):
            xs.append(xa + (y - ya) * (xb - xa) / (yb - ya))
    return xs


def point_in_polygon(point: Point, poly: Sequence[Point]) -> bool:
    return sum(1 for x in _crossings(poly, point[1]) if x > point[0]) % 2 == 1
