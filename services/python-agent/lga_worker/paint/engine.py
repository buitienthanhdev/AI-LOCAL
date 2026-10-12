"""Orchestration: execute a plan through a `CanvasDriver`, verify after each stage, repair only what is wrong.

The driver is the only part that touches Paint (`driver.PaintDriver`); a fake driver that draws on a PIL image
lets this logic be tested without a desktop.

Rules (docs: acceptance criteria):
* objects go in plan order (layer, then request order), grouped by style inside a layer so tool, colour and
  size changes are kept to a minimum;
* the canvas is captured and compared with the plan at stage boundaries (about every `BATCH_OBJECTS` objects, at a
  layer boundary), never blind;
* a failed object is drawn again (drawing the same colours over itself is idempotent); anything that is not
  explained by a single object (colour spill, a stroke far off target) is not "repaired" by drawing more;
* the run stops when the driver reports that Paint lost focus or moved.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from PIL import Image, ImageChops, ImageDraw

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import raster, verify
from lga_worker.paint.model import Plan, PlannedObject

BATCH_OBJECTS = 6
MAX_REPAIR_ROUNDS = 2
REPAIR_SPILL_MAX = 0.05  # more unexplained ink than this is not one bad object: do not draw on top of it
MAX_FILL_FRACTION = 0.45  # a bucket fill that would cover more of the page than this is a leak, not a region
FILL_LEAK_SPILL = 0.02
MAX_RECOVERIES = 4


class CanvasDriver(Protocol):
    scale: float

    def check_ready(self) -> None:
        """Raise `ToolError` when Paint lost focus, a dialog opened, or the canvas moved/zoomed."""

    def draw(self, obj: PlannedObject) -> None:
        """Draw one object and leave it committed."""

    def capture(self) -> Image.Image:
        """The canvas pixels at canvas resolution (`plan.canvas` size)."""

    # Optional (looked up with getattr): recover(error) -> bool puts the window back after a recoverable error and says
    # whether drawing may go on; undo() takes back the last operation (a bucket fill that leaked).


@dataclass
class Stage:
    up_to_layer: int
    objects: list[str]
    ok: bool
    failed: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    spill_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"up_to_layer": self.up_to_layer, "objects": len(self.objects), "ok": self.ok, "failed": self.failed,
                "repaired": self.repaired, "spill_ratio": round(self.spill_ratio, 4)}


@dataclass
class DrawResult:
    ok: bool
    drawn: int
    stages: list[Stage]
    final: verify.VerifyReport
    redraws: dict[str, int]
    events: list[dict[str, Any]] = field(default_factory=list)  # recoveries, fill decisions, resume: what was decided and why
    skipped_fills: list[str] = field(default_factory=list)  # fills refused because the region was not closed / would leak
    resumed: list[str] = field(default_factory=list)  # ids found already on the canvas and not drawn again

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "drawn": self.drawn, "stages": [s.to_dict() for s in self.stages],
                "redraws": self.redraws, "verification": self.final.to_dict(), "events": self.events,
                "resumed": len(self.resumed), "skipped_fills": self.skipped_fills}


def draw_order(objects: list[PlannedObject]) -> list[PlannedObject]:
    """Plan order, with objects of one layer grouped by (tool, colours, width) to avoid needless UI changes."""
    def style_key(o: PlannedObject) -> tuple:
        return (o.kind in ("curve",), o.kind, o.stroke or (-1, -1, -1), o.fill or (-1, -1, -1), o.stroke_px)

    return sorted(objects, key=lambda o: (o.layer, style_key(o), o.order))


def _stages(ordered: list[PlannedObject]) -> list[list[PlannedObject]]:
    """Batches that end at layer boundaries and hold at least BATCH_OBJECTS objects (the last may be smaller)."""
    batches: list[list[PlannedObject]] = []
    current: list[PlannedObject] = []
    for i, obj in enumerate(ordered):
        current.append(obj)
        last_of_layer = i + 1 == len(ordered) or ordered[i + 1].layer != obj.layer
        if last_of_layer and len(current) >= BATCH_OBJECTS:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


def _overlap(plan: Plan, a: PlannedObject, b: PlannedObject) -> bool:
    """True when the drawn footprints (fill and stroke) of two objects share a pixel. Bounding boxes decide
    first; only boxes that meet are rasterised, in a window the size of their intersection."""
    if a.kind == "fill" or b.kind == "fill":
        return True  # where a bucket fill ends depends on everything around it: redo it with whatever is redone
    ax0, ay0, ax1, ay1 = a.bounds
    bx0, by0, bx1, by1 = b.bounds
    pad = max(a.stroke_px, b.stroke_px) + 1
    left, top = max(ax0, bx0) - pad, max(ay0, by0) - pad
    right, bottom = min(ax1, bx1) + pad, min(ay1, by1) + pad
    if right <= left or bottom <= top:
        return False
    ox, oy = int(left), int(top)
    size = (max(1, int(right) - ox + 1), max(1, int(bottom) - oy + 1))

    def footprint(obj: PlannedObject) -> Image.Image:
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        pts = [(x - ox, y - oy) for x, y in obj.outline]
        if obj.fill is not None and obj.closed:
            draw.polygon(pts, fill=255)
        if obj.stroke is not None:
            path = [*pts, pts[0]] if obj.closed else pts
            draw.line(path, fill=255, width=obj.stroke_px, joint="curve")
        return mask

    return ImageChops.multiply(footprint(a), footprint(b)).getbbox() is not None


def repair_set(plan: Plan, failed: list[str], drawn: set[str] | None = None) -> list[PlannedObject]:
    """Objects to draw again, in draw order: the failed ones and everything drawn after them whose pixels
    overlap theirs (an object drawn late would otherwise end up on top of the objects that belong above it).
    Only objects that are already on the canvas (`drawn`) are candidates: later stages are not drawn early."""
    ordered = [o for o in draw_order(plan.objects) if drawn is None or o.id in drawn]
    position = {o.id: i for i, o in enumerate(ordered)}
    todo = {i for i in failed}
    queue = list(todo)
    while queue:
        current = plan.by_id(queue.pop())
        for o in ordered:
            if o.id in todo or position[o.id] < position[current.id]:
                continue
            if _overlap(plan, current, o):
                todo.add(o.id)
                queue.append(o.id)
    return [o for o in ordered if o.id in todo]


def fill_decision(plan: Plan, on_canvas: list[PlannedObject], obj: PlannedObject) -> tuple[str, str]:
    """('fill' | 'skip' | 'unsafe', why) for a bucket fill, decided on the picture the canvas must show right now."""
    seed = (round(obj.points[0][0]), round(obj.points[0][1]))
    base = raster.render(plan.canvas, on_canvas)
    if not (0 <= seed[0] < plan.canvas.width and 0 <= seed[1] < plan.canvas.height):
        return "unsafe", "the seed is off the canvas"
    if tuple(base.getpixel(seed)) == tuple(obj.fill or ()):
        return "skip", "the region already has that colour"
    enclosed, area = raster.bucket_fill_region(base, seed)
    if not enclosed:
        return "unsafe", "the region is not closed (it reaches the edge of the page): the colour would flood it"
    fraction = area / (plan.canvas.width * plan.canvas.height)
    if fraction > MAX_FILL_FRACTION:
        return "unsafe", f"the region covers {fraction:.0%} of the page: more than a region, a leak"
    return "fill", f"closed region of {area} px ({fraction:.1%} of the page)"


class _Runner:
    """One execution of a plan: draws objects, recovers from recoverable errors, decides bucket fills."""

    def __init__(self, plan: Plan, driver: CanvasDriver, should_stop: Callable[[], bool]) -> None:
        self.plan, self.driver, self.should_stop = plan, driver, should_stop
        self.on_canvas: list[PlannedObject] = []  # in the order they were drawn
        self.events: list[dict[str, Any]] = []
        self.recoveries = 0
        self.skipped: list[str] = []  # fills that were refused (unsafe) and not drawn: not part of the plan any more

    def _ready(self) -> None:
        while True:
            if self.should_stop():
                raise ToolError(ErrorCode.CANCELLED, "drawing cancelled")
            try:
                self.driver.check_ready()
                return
            except ToolError as exc:
                kind = (exc.details or {}).get("recoverable")
                recover = getattr(self.driver, "recover", None)
                if kind is None or recover is None or self.recoveries >= MAX_RECOVERIES:
                    raise
                self.recoveries += 1
                ok = bool(recover(exc))
                self.events.append({"event": "recovery", "kind": kind, "ok": ok, "message": exc.message})
                if not ok:
                    raise

    def draw(self, obj: PlannedObject) -> None:
        self._ready()
        if obj.kind != "fill":
            self.driver.draw(obj)
            self._record(obj)
            return
        decision, why = fill_decision(self.plan, self.on_canvas, obj)
        self.events.append({"event": "fill", "id": obj.id, "decision": decision, "why": why})
        if decision == "skip":
            self._record(obj)
            return
        if decision == "unsafe":
            self._shape_fill(obj, f"bucket refused: {why}")
            return
        self.driver.draw(obj)
        expected = raster.render(self.plan.canvas, [*self.on_canvas, obj])
        leak = verify.unexplained_ratio(expected, self.driver.capture(), getattr(self.driver, "scale", 1.0))
        if leak > FILL_LEAK_SPILL:
            undo = getattr(self.driver, "undo", None)
            undone = bool(undo()) if undo else False
            self.events.append({"event": "fill_leak", "id": obj.id, "unexplained": round(leak, 3), "undone": undone})
            if not undone:
                raise ToolError(ErrorCode.VERIFICATION_FAILED, f"the fill {obj.id!r} leaked and could not be undone",
                                details={"unexplained": leak})
            self._shape_fill(obj, "the bucket leaked and was undone")
            return
        self._record(obj)

    def _shape_fill(self, obj: PlannedObject, why: str) -> None:
        target = None
        if obj.region:
            try:
                target = self.plan.by_id(obj.region)
            except KeyError:
                target = None
        if target is not None and target.closed:
            self.driver.draw(dataclasses.replace(target, fill=obj.fill))
            self.events.append({"event": "fill_fallback", "id": obj.id, "region": target.id, "via": "shape fill", "why": why})
            self._record(dataclasses.replace(target, fill=obj.fill))
        else:
            self.events.append({"event": "fill_skipped", "id": obj.id, "why": why})
            self.skipped.append(obj.id)

    def _record(self, obj: PlannedObject) -> None:
        self.on_canvas = [o for o in self.on_canvas if o.id != obj.id] + [obj]


def _already_drawn(plan: Plan, driver: CanvasDriver, fill_ok: float, check_strokes: bool) -> set[str]:
    """Objects whose own pixels are measurably on the canvas now (a resumed run does not draw them again). An object
    nothing of which is visible (covered, or too thin to measure) counts as not drawn."""
    report = verify.compare(plan, driver.capture(), scale=getattr(driver, "scale", 1.0), fill_ok=fill_ok,
                            check_strokes=check_strokes)
    return {r.id for r in report.objects if r.ok and (r.stroke_coverage is not None or r.fill_match is not None)}


def run_plan(plan: Plan, driver: CanvasDriver, *, should_stop: Callable[[], bool] = lambda: False,
             max_repair_rounds: int = MAX_REPAIR_ROUNDS, tolerance: float = 0.0, resume: bool = False) -> DrawResult:
    """Draw `plan` through `driver`. `tolerance` is the share of objects that may still be wrong at the end
    (0 for exact drawings; a traced photo of 100 regions accepts a few thin ones that Paint did not take).
    `resume` first looks at the canvas and draws only what is not already there."""
    ordered = draw_order(plan.objects)
    fill_ok = verify.FILL_TRACED_OK if tolerance > 0 else verify.FILL_SAMPLE_OK
    stages: list[Stage] = []
    redraws: dict[str, int] = {}
    drawn = 0
    report = verify.VerifyReport(ok=True, objects=[], spill_ratio=0.0, tolerance_px=0)
    runner = _Runner(plan, driver, should_stop)
    present: set[str] = set()
    if resume:
        present = _already_drawn(plan, driver, fill_ok, tolerance <= 0)
        runner.on_canvas = [o for o in ordered if o.id in present]
        runner.events.append({"event": "resume", "found_on_canvas": len(present), "of": len(ordered)})
    on_canvas = {o.id for o in runner.on_canvas}

    for batch in _stages(ordered):
        for obj in batch:
            if obj.id in present:
                continue
            runner.draw(obj)
            drawn += 1
            if obj.id in runner.skipped:  # an unsafe fill was refused: verify the rest, report this one
                plan.objects = [o for o in plan.objects if o.id != obj.id]
            else:
                on_canvas.add(obj.id)
        up_to = max(o.layer for o in batch)
        report = verify.compare(plan, driver.capture(), up_to_layer=up_to, scale=driver.scale, fill_ok=fill_ok,
                                check_strokes=tolerance <= 0)
        stage = Stage(up_to_layer=up_to, objects=[o.id for o in batch], ok=report.ok, failed=list(report.failed_ids),
                      spill_ratio=report.spill_ratio)
        rounds = 0
        rounds_allowed = 1 if tolerance > 0 else max_repair_rounds
        while report.failed_ids and rounds < rounds_allowed and report.spill_ratio <= REPAIR_SPILL_MAX:
            rounds += 1
            for obj in repair_set(plan, list(report.failed_ids), on_canvas):
                runner.draw(obj)
                redraws[obj.id] = redraws.get(obj.id, 0) + 1
                stage.repaired.append(obj.id)
            report = verify.compare(plan, driver.capture(), up_to_layer=up_to, scale=driver.scale, fill_ok=fill_ok,
                                    check_strokes=tolerance <= 0)
        stage.ok = report.ok
        stage.failed = list(report.failed_ids)
        stage.spill_ratio = report.spill_ratio
        stages.append(stage)
        if not _acceptable(report, len(drawn_ids(stages, plan)), tolerance):
            break  # what is on the canvas is not what the plan says; more drawing would pile on top of a mistake

    final = verify.compare(plan, driver.capture(), scale=driver.scale, fill_ok=fill_ok,
                           check_strokes=tolerance <= 0) if drawn_ids(stages, plan) else report
    ok = _acceptable(final, len(plan.objects), tolerance) and len(stages) == len(_stages(ordered))
    return DrawResult(ok=ok, drawn=drawn, stages=stages, final=final, redraws=redraws, events=runner.events,
                      resumed=sorted(present), skipped_fills=list(runner.skipped))


def drawn_ids(stages: list[Stage], plan: Plan) -> list[str]:
    seen: list[str] = []
    for stage in stages:
        seen.extend(stage.objects)
    return seen


def _acceptable(report: verify.VerifyReport, total: int, tolerance: float) -> bool:
    if report.spill_ratio > verify.SPILL_OK:
        return False
    if not report.failed_ids:
        return True
    return tolerance > 0 and len(report.failed_ids) <= max(1, int(tolerance * max(total, 1)))
