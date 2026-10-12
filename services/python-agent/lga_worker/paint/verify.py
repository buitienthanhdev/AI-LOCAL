"""Pixel verification of a canvas against its plan.

No vision model is involved. The expected canvas is rendered from the plan (`raster.render_plan`) and the
screenshot of the canvas is compared to it, object by object:

* stroke coverage: how much of the object's visible outline is present in the actual image, within a small
  positional tolerance (Paint anti-aliases and the mouse lands on whole pixels);
* fill colour: sampled well inside the object, compared with the colour the plan expects at that point;
* spill: non-white pixels in the actual image that the plan does not explain (a flood fill that escaped, a
  stroke drawn off target).

Failures name the object, so a repair redraws that object only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import raster
from lga_worker.paint.model import RGB, Plan, PlannedObject

COLOR_TOLERANCE = 48  # per channel; absorbs anti-aliasing and palette rounding, still separates a different colour
COVERAGE_OK = 0.90
FILL_SAMPLE_OK = 0.80
FILL_TRACED_OK = 0.50  # traced pictures: neighbouring polygons overlap a little, so a region is judged by its core
SPILL_OK = 0.01  # fraction of the drawn pixels that the plan does not explain
NEAR_WHITE = 245
FILL_SAMPLES = 9


@dataclass
class ObjectReport:
    id: str
    ok: bool
    stroke_coverage: float | None  # None: no visible outline to judge (stroke-less, or fully covered by later objects)
    fill_match: float | None  # None: no fill to judge
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "stroke_coverage": self.stroke_coverage,
                "fill_match": self.fill_match, "detail": self.detail}


@dataclass
class VerifyReport:
    ok: bool
    objects: list[ObjectReport]
    spill_ratio: float
    tolerance_px: int
    notes: list[str] = field(default_factory=list)

    @property
    def failed_ids(self) -> list[str]:
        return [o.id for o in self.objects if not o.ok]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "spill_ratio": round(self.spill_ratio, 4), "tolerance_px": self.tolerance_px,
                "failed": self.failed_ids, "objects": [o.to_dict() for o in self.objects], "notes": self.notes}

    def summary(self) -> str:
        if self.ok:
            return (f"Canvas matches the plan: {len(self.objects)} object(s) verified, "
                    f"{self.spill_ratio:.2%} unexplained pixels.")
        parts = [f"{o.id}: {o.detail}" for o in self.objects if not o.ok]
        if self.spill_ratio > SPILL_OK:
            parts.append(f"{self.spill_ratio:.1%} of the drawn pixels are not explained by the plan "
                         f"(colour spilled or strokes off target)")
        return "Canvas does NOT match the plan: " + "; ".join(parts)


def _dilate(mask: Image.Image, radius: int) -> Image.Image:
    return mask if radius <= 0 else mask.filter(ImageFilter.MaxFilter(2 * radius + 1))


def _erode(mask: Image.Image, radius: int) -> Image.Image:
    return mask if radius <= 0 else mask.filter(ImageFilter.MinFilter(2 * radius + 1))


def _count(mask: Image.Image) -> int:
    return mask.histogram()[255]


def tolerance_for_scale(scale: float) -> int:
    """Positional tolerance in canvas pixels: one screen pixel is 1/scale canvas pixels, plus one for rounding."""
    return max(1, math.ceil(1.0 / max(scale, 1e-6)) + 1)


def _non_white(image: Image.Image) -> Image.Image:
    """'L' mask: 255 where any channel is below NEAR_WHITE."""
    darkest = ImageChops.darker(ImageChops.darker(*image.split()[:2]), image.split()[2])
    return darkest.point(lambda v: 255 if v < NEAR_WHITE else 0)


def _later_footprint(plan: Plan, later: list[PlannedObject], box: tuple[int, int, int, int]) -> Image.Image:
    """'L' mask (cropped to `box`): where objects drawn after this one put any ink, fill or stroke."""
    mask = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
    for other in later:
        x0, y0, x1, y1 = other.bounds
        pad = other.stroke_px + 1
        if x1 + pad < box[0] or x0 - pad > box[2] or y1 + pad < box[1] or y0 - pad > box[3]:
            continue
        footprint = raster.stroke_mask(plan.canvas, other).crop(box)
        if other.fill is not None:
            footprint = ImageChops.lighter(footprint, raster.fill_mask(plan.canvas, other).crop(box))
        mask = ImageChops.lighter(mask, footprint)
    return mask


def _object_report(obj: PlannedObject, plan: Plan, expected: Image.Image, actual: Image.Image,
                   tol_px: int, tol_color: int, later: list[PlannedObject], fill_ok: float,
                   check_strokes: bool = True) -> ObjectReport:
    canvas = plan.canvas
    if obj.kind == "fill":
        x, y = round(obj.points[0][0]), round(obj.points[0][1])
        if not (0 <= x < canvas.width and 0 <= y < canvas.height) or obj.fill is None:
            return ObjectReport(id=obj.id, ok=False, stroke_coverage=None, fill_match=0.0, detail="fill seed is off the canvas")
        if tuple(expected.getpixel((x, y))) != tuple(obj.fill):  # painted over by something drawn later: nothing to see
            return ObjectReport(id=obj.id, ok=True, stroke_coverage=None, fill_match=None, detail="")
        got = actual.getpixel((x, y))
        ok = all(abs(a - b) <= tol_color for a, b in zip(got, obj.fill, strict=True))
        return ObjectReport(id=obj.id, ok=ok, stroke_coverage=None, fill_match=1.0 if ok else 0.0,
                            detail="" if ok else f"the region is {got}, not the fill {obj.fill}")
    pad = obj.stroke_px + 2 * tol_px + 3
    x0, y0, x1, y1 = obj.bounds
    box = (max(0, math.floor(x0) - pad), max(0, math.floor(y0) - pad),
           min(canvas.width, math.ceil(x1) + pad), min(canvas.height, math.ceil(y1) + pad))
    exp, act = expected.crop(box), actual.crop(box)
    coverage: float | None = None
    fill_match: float | None = None
    problems: list[str] = []

    if obj.stroke is not None and check_strokes:
        ideal = raster.stroke_mask(canvas, obj).crop(box)
        # only outline pixels that nothing drawn later covers or shares: another object's ink of the same colour
        # must not stand in for this one's
        uncovered = ImageChops.invert(_dilate(_later_footprint(plan, later, box), 1))
        visible = ImageChops.multiply(ImageChops.multiply(ideal, uncovered), raster.color_mask(exp, obj.stroke, 8))
        n_visible = _count(visible)
        if n_visible >= 4:
            near = _dilate(raster.color_mask(act, obj.stroke, tol_color), tol_px)
            coverage = _count(ImageChops.multiply(visible, near)) / n_visible
            if coverage < COVERAGE_OK:
                problems.append(f"outline only {coverage:.0%} present")

    if obj.fill is not None and obj.closed:
        # Pixels where the plan shows this object's fill, away from its edges and from anything drawn over it.
        shown = ImageChops.multiply(raster.fill_mask(canvas, obj).crop(box), raster.color_mask(exp, obj.fill, 8))
        inner = _erode(shown, tol_px + 1)
        bbox = inner.getbbox()
        if bbox is not None:
            samples = _grid_samples(inner, bbox)
            matches = sum(1 for xy in samples
                          if all(abs(a - b) <= tol_color for a, b in zip(exp.getpixel(xy), act.getpixel(xy), strict=True)))
            if samples:
                fill_match = matches / len(samples)
                if fill_match < fill_ok:
                    problems.append(f"fill is {fill_match:.0%} right (expected {exp.getpixel(samples[0])}, "
                                    f"found {act.getpixel(samples[0])})")
    return ObjectReport(id=obj.id, ok=not problems, stroke_coverage=_round(coverage), fill_match=_round(fill_match),
                        detail="; ".join(problems))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _grid_samples(mask: Image.Image, box: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    left, top, right, bottom = box
    out: list[tuple[int, int]] = []
    side = max(2, round(math.sqrt(FILL_SAMPLES * 3)))  # probe a denser grid, keep the points that fall inside
    for j in range(side):
        for i in range(side):
            x = left + round((right - left - 1) * (i + 0.5) / side)
            y = top + round((bottom - top - 1) * (j + 0.5) / side)
            if mask.getpixel((x, y)) == 255:
                out.append((x, y))
    step = max(1, len(out) // FILL_SAMPLES)
    return out[::step][:FILL_SAMPLES]


def compare(plan: Plan, actual: Image.Image, *, up_to_layer: int | None = None, scale: float = 1.0,
            only_ids: set[str] | None = None, fill_ok: float = FILL_SAMPLE_OK,
            check_strokes: bool = True) -> VerifyReport:
    """Compare `actual` (the canvas pixels, `plan.canvas` sized) with the plan drawn up to `up_to_layer`."""
    if actual.size != (plan.canvas.width, plan.canvas.height):
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        f"captured canvas is {actual.size[0]}x{actual.size[1]} but the plan is "
                        f"{plan.canvas.width}x{plan.canvas.height}; re-calibrate before verifying")
    actual = actual.convert("RGB")
    tol_px = tolerance_for_scale(scale)
    expected = raster.render_plan(plan, up_to_layer=up_to_layer)
    drawn = [o for o in plan.objects if up_to_layer is None or o.layer <= up_to_layer]
    reports = [_object_report(o, plan, expected, actual, tol_px, COLOR_TOLERANCE, drawn[i + 1:], fill_ok, check_strokes)
               for i, o in enumerate(drawn) if only_ids is None or o.id in only_ids]

    expected_zone = _dilate(_non_white(expected), tol_px + 1)
    actual_ink = _non_white(actual)
    unexplained = ImageChops.multiply(actual_ink, ImageChops.invert(expected_zone))
    total_ink = max(1, _count(actual_ink))
    spill = _count(unexplained) / total_ink
    ok = all(r.ok for r in reports) and spill <= SPILL_OK
    return VerifyReport(ok=ok, objects=reports, spill_ratio=spill, tolerance_px=tol_px)


def color_distance(a: RGB, b: RGB) -> int:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def ink_agreement(plan: Plan, actual: Image.Image, *, scale: float = 1.0) -> dict[str, float]:
    """For plans made only of thin strokes (sketches): how much of the planned ink is on the canvas and how
    much canvas ink the plan does not explain. Per-stroke checks would cost more than the drawing."""
    tol = tolerance_for_scale(scale) + 1
    expected = _non_white(raster.render_plan(plan))
    got = _non_white(actual.convert("RGB"))
    near_expected = _dilate(expected, tol)
    near_got = _dilate(got, tol)
    planned = max(1, _count(expected))
    drawn = max(1, _count(got))
    covered = _count(ImageChops.multiply(expected, near_got)) / planned
    stray = _count(ImageChops.multiply(got, ImageChops.invert(near_expected))) / drawn
    return {"coverage": round(covered, 3), "stray": round(stray, 3)}


def unexplained_ratio(expected: Image.Image, actual: Image.Image, scale: float = 1.0) -> float:
    """Share of the canvas ink that the expected picture does not explain (a fill that leaked shows up here)."""
    tol = tolerance_for_scale(scale)
    zone = _dilate(_non_white(expected.convert("RGB")), tol + 1)
    ink = _non_white(actual.convert("RGB"))
    return _count(ImageChops.multiply(ink, ImageChops.invert(zone))) / max(1, _count(ink))
