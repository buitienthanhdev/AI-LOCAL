"""Named scenes computed from a few numbers, so a small planner model never places strokes by hand.

Each template is designed in a 100 x 100 box (y down) and mapped into the canvas by `Frame`, centred and kept
inside the safe margin. They return `DrawingObject`s; `geometry.plan_scene` validates and resolves them.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint.model import Canvas, DrawingObject, Point

FILL_FRACTION = 0.86  # of the shorter canvas side
OUTLINE = "#000000"


@dataclass(frozen=True)
class Frame:
    """Maps the 100 x 100 design box to composition units."""

    left: float
    top: float
    size: float

    @classmethod
    def centred(cls, canvas: Canvas) -> Frame:
        """The largest square that fits the safe fraction of the canvas, centred."""
        size = min(canvas.unit_height, 1000.0) * FILL_FRACTION
        return cls((1000.0 - size) / 2, (canvas.unit_height - size) / 2, size)

    def pt(self, x: float, y: float) -> Point:
        k = self.size / 100.0
        return self.left + x * k, self.top + y * k

    def box(self, x0: float, y0: float, x1: float, y1: float) -> tuple[Point, Point]:
        return self.pt(x0, y0), self.pt(x1, y1)


def doraemon_v1(canvas: Canvas, *, body: str = "#0096E0", belly: str = "#FFFFFF", collar: str = "#E60012",
                bell: str = "#FFD400") -> list[DrawingObject]:
    """Version 1 of the template (free interpretation, 100 x 100 box). Kept as the way back; `doraemon` is version 2."""
    f = Frame.centred(canvas)
    objs: list[DrawingObject] = []

    def add(oid: str, kind: str, points, *, fill: str | None = None, stroke: str | None = OUTLINE,
            weight: str = "structure", layer: int = 0) -> None:
        objs.append(DrawingObject(oid, kind, tuple(points), fill=fill, stroke=stroke, weight=weight, layer=layer))

    # back to front; same-layer objects never overlap, so the order of the draw calls never matters
    add("foot_l", "ellipse", f.box(20, 90, 49, 100), fill=belly, layer=0)
    add("foot_r", "ellipse", f.box(51, 90, 80, 100), fill=belly, layer=0)
    add("torso", "ellipse", f.box(22, 58, 78, 96), fill=body, layer=1)
    add("arm_l", "ellipse", f.box(8, 62, 30, 80), fill=body, layer=2)
    add("arm_r", "ellipse", f.box(70, 62, 92, 80), fill=body, layer=2)
    add("hand_l", "ellipse", f.box(2, 72, 17, 87), fill=belly, layer=3)
    add("hand_r", "ellipse", f.box(83, 72, 98, 87), fill=belly, layer=3)
    add("belly", "ellipse", f.box(32, 64, 68, 92), fill=belly, weight="detail", layer=3)
    pocket = [f.pt(50 + 14 * math.cos(math.radians(a)), 74 + 14 * math.sin(math.radians(a)))
              for a in range(0, 181, 15)]
    add("pocket", "polygon", pocket, fill=belly, weight="detail", layer=4)
    add("collar", "rect", f.box(26, 58, 74, 67), fill=collar, layer=5)
    add("bell", "ellipse", f.box(43, 63, 57, 77), fill=bell, layer=6)
    add("bell_line", "line", [f.pt(43, 70), f.pt(57, 70)], weight="detail", layer=7)
    add("head", "ellipse", f.box(10, 2, 90, 64), fill=body, weight="accent", layer=8)
    add("face", "ellipse", f.box(18, 16, 82, 62), fill=belly, weight="detail", layer=9)
    add("eye_l", "ellipse", f.box(33, 8, 50, 30), fill="#FFFFFF", layer=10)
    add("eye_r", "ellipse", f.box(50, 8, 67, 30), fill="#FFFFFF", layer=10)
    add("pupil_l", "ellipse", f.box(42, 18, 48, 26), fill="#000000", weight="detail", layer=11)
    add("pupil_r", "ellipse", f.box(52, 18, 58, 26), fill="#000000", weight="detail", layer=11)
    add("nose", "ellipse", f.box(45, 29, 55, 39), fill="#E60012", layer=12)
    add("mouth_line", "line", [f.pt(50, 39), f.pt(50, 50)], weight="detail", layer=13)
    add("mouth", "curve", [f.pt(27, 42), f.pt(37, 54), f.pt(50, 58), f.pt(63, 54), f.pt(73, 42)],
        weight="structure", layer=13)
    for i, (y_in, y_out) in enumerate(((40, 34), (46, 46), (52, 58))):
        add(f"whisker_l{i}", "line", [f.pt(30, y_in), f.pt(11, y_out)], weight="detail", layer=13)
        add(f"whisker_r{i}", "line", [f.pt(70, y_in), f.pt(89, y_out)], weight="detail", layer=13)
    return objs


def house(canvas: Canvas, *, wall: str = "#FFC90E", roof: str = "#ED1C24", door: str = "#0096E0",
          window: str = "#99D9EA") -> list[DrawingObject]:
    f = Frame.centred(canvas)
    objs: list[DrawingObject] = []

    def add(oid: str, kind: str, points, *, fill: str | None = None, weight: str = "structure", layer: int = 0):
        objs.append(DrawingObject(oid, kind, tuple(points), fill=fill, stroke=OUTLINE, weight=weight, layer=layer))

    add("wall", "rect", f.box(12, 45, 88, 95), fill=wall, layer=0)
    add("roof", "polygon", [f.pt(4, 45), f.pt(50, 8), f.pt(96, 45)], fill=roof, weight="accent", layer=1)
    add("door", "rect", f.box(42, 65, 58, 95), fill=door, layer=2)
    add("knob", "ellipse", f.box(53, 79, 56, 82), fill="#000000", weight="detail", layer=3)
    add("window_l", "rect", f.box(20, 58, 34, 72), fill=window, weight="detail", layer=2)
    add("window_r", "rect", f.box(66, 58, 80, 72), fill=window, weight="detail", layer=2)
    for side, (x0, x1) in (("l", (20, 34)), ("r", (66, 80))):
        add(f"cross_v_{side}", "line", [f.pt((x0 + x1) / 2, 58), f.pt((x0 + x1) / 2, 72)], weight="detail", layer=3)
        add(f"cross_h_{side}", "line", [f.pt(x0, 65), f.pt(x1, 65)], weight="detail", layer=3)
    return objs


def doraemon(canvas: Canvas, **options: str) -> list[DrawingObject]:
    """Version 2 (proportion-checked, occlusion-ordered; see `lga_worker.paint.doraemon`). Colour options: body, belly,
    collar, bell. Use `doraemon.prepare` directly when the constraint report or a reference is needed."""
    from lga_worker.paint import doraemon as v2

    return v2.prepare(canvas, options=options).scene.objects


TEMPLATES: dict[str, Callable[..., list[DrawingObject]]] = {
    "doraemon": doraemon, "doraemon_v2": doraemon, "doraemon_v1": doraemon_v1, "house": house}


def build_template(name: str, canvas: Canvas, **options: str) -> list[DrawingObject]:
    builder = TEMPLATES.get(name.strip().lower())
    if builder is None:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        f"unknown template {name!r}; available: {', '.join(sorted(TEMPLATES))}")
    try:
        return builder(canvas, **options)
    except TypeError as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"template {name!r}: {exc}") from exc
