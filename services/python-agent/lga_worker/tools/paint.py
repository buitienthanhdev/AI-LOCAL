"""Handlers for the paint.* tools (contract: tools/paint.json).

One call draws a whole picture: the scene is planned and validated in canvas pixels, drawn in a fresh Paint
window, compared with the plan pixel by pixel after each stage, and the call fails (VERIFICATION_FAILED) when the
canvas does not match, so the agent can never report a drawing that is not there.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import geometry, raster, vectorize, verify
from lga_worker.paint.driver import PaintDriver
from lga_worker.art.gate import run_gated, specification_summary
from lga_worker.paint.engine import run_plan  # noqa: F401
from lga_worker.paint.model import Canvas
from lga_worker.paint.templates import build_template
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "paint-engine"
MAX_IMAGE_BYTES = 40_000_000
PAINT_SKETCH_MAX = 1100  # strokes: each is a separate brush stroke, about 0.25 s
SKETCH_COVERAGE_OK = 0.60
SKETCH_STRAY_MAX = 0.15
TRACE_TOLERANCE = 0.10  # a traced picture may keep this share of its polygons unmatched (thin ones Paint refused)


@dataclass
class PaintSession:
    """The Paint window this worker opened last (never a window it did not open)."""

    hwnd: int | None = None
    offset: tuple[int, int] = (0, 0)
    plan: Any = None  # the plan of the last paint.draw, so paint.inspect can check the canvas against it again
    tolerance: float = 0.0  # share of objects allowed to differ (traced pictures)


async def _session(ctx: ToolContext) -> PaintSession:
    async def create() -> PaintSession:
        return PaintSession()

    return await ctx.services.get_or_create("paint", create)


def _scene_objects(args: dict[str, Any], canvas: Canvas) -> list:
    if args.get("template"):
        options = {k: str(v) for k, v in (args.get("options") or {}).items()}
        return build_template(str(args["template"]), canvas, **options)
    return [geometry.object_from_dict(o) for o in args["objects"]]


def _sketch_objects(image: Image.Image, canvas: Canvas, detail: str) -> vectorize.Vectorized:
    """The picture as coloured pencil strokes for Paint's brush: stroke colours are snapped to the colours Paint can
    have at hand (its 20 swatches plus up to 10 fitted custom colours), the stroke count is capped by time."""
    import cv2
    import numpy as np

    from lga_worker.paint import sketch as sketching
    from lga_worker.paint.model import DrawingObject

    traced = sketching.sketch(image, canvas, detail=detail, colors=sketching.DEFAULT_COLORS, max_strokes=PAINT_SKETCH_MAX)
    colors = sorted({s.color for s in traced.strokes})
    weights = np.array([sum(1 for s in traced.strokes if s.color == c) for c in colors], np.float32)
    points = vectorize._lab(cv2, np, [sketching.hex_to_rgb(c) for c in colors])
    palette_lab, palette_rgb = vectorize.build_palette(cv2, np, points, weights, vectorize.CUSTOM_SLOTS)
    nearest = ((points[:, None, :] - palette_lab[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
    mapped = {c: "#%02X%02X%02X" % palette_rgb[int(i)] for c, i in zip(colors, nearest, strict=True)}
    objects = []
    for s in traced.strokes:
        xs = [x for x, _ in s.points]
        ys = [y for _, y in s.points]
        if max(max(xs) - min(xs), max(ys) - min(ys)) * canvas.px_per_unit < 4.0:
            continue  # smaller than the plan's minimum object size: a speck, not a stroke
        n = len(objects)
        pts = tuple(s.points)
        objects.append(DrawingObject(id=f"s{n:04d}", kind="path", points=pts + ((pts[0],) if s.closed else ()),
                                     fill=None, stroke=mapped[s.color], weight="detail", layer=0))
    used = sorted(set(mapped.values()))
    return vectorize.Vectorized(objects=objects, palette=used,
                                stats={**traced.stats, "regions": len(objects), "colors": len(used), "max_depth": 0})


def register(registry: ToolRegistry) -> None:
    @registry.handler("paint.draw")
    async def draw(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        session = await _session(ctx)
        shots = ctx.artifact_dir("paint")
        reuse = bool(args.get("reuse_window")) and session.hwnd is not None

        def work() -> dict[str, Any]:
            driver = PaintDriver(provider, should_stop=ctx.cancel_event.is_set)
            transform = driver.attach(session.hwnd, session.offset) if reuse else driver.open_new()
            session.hwnd, session.offset = driver.hwnd, driver.offset
            driver.initial_state()
            canvas = transform.canvas
            plan = geometry.plan_scene(_scene_objects(args, canvas), canvas)
            session.plan, session.tolerance = plan, 0.0
            result, gate = run_gated(
                plan, driver, request=str(args.get("request") or f"paint.draw {args.get('template') or 'objects'}"),
                directory=shots, source={"kind": "template" if args.get("template") else "objects",
                                         "name": str(args.get("template") or "objects")},
                should_stop=ctx.cancel_event.is_set, user_supplied_plan=True)
            stamp = int(time.time() * 1000)
            image_path = shots / f"canvas-{stamp}.png"
            driver.capture().save(image_path, format="PNG")
            (shots / f"plan-{stamp}.json").write_text(json.dumps({
                "canvas": [canvas.width, canvas.height], "args": args,
                "objects": [{"id": o.id, "kind": o.kind, "layer": o.layer, "stroke_px": o.stroke_px,
                             "fill": o.fill, "stroke": o.stroke, "points": o.points} for o in plan.objects],
                "result": result.to_dict(), "warnings": plan.warnings}, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"hwnd": driver.hwnd, "canvas": canvas, "scale": transform.scale, "plan": plan, "result": result,
                    "image": str(image_path), "gate": gate}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        result, canvas, plan = out["result"], out["canvas"], out["plan"]
        summary = result.final.summary()
        data = {
            "hwnd": out["hwnd"], "canvas": {"width": canvas.width, "height": canvas.height},
            "units": {"x": 1000, "y": round(canvas.unit_height)}, "scale": out["scale"],
            "objects": len(plan.objects), "drawn": result.drawn, "matches_plan": result.ok,
            "redrawn": result.redraws, "spill_ratio": round(result.final.spill_ratio, 4),
            "failed_objects": result.final.failed_ids, "warnings": plan.warnings, "screenshot": out["image"],
            "specification": specification_summary(out["gate"]),
        }
        if not result.ok:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, summary, retryable=False,
                            details={"data": data, "failed": result.final.failed_ids,
                                     "report": result.final.to_dict(), "screenshot": out["image"]})
        return ToolOutcome(
            data=data, checks=[Check("canvas_matches_plan", True, summary)],
            artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=f"Drew {result.drawn} objects in Paint (window {out['hwnd']}, canvas {canvas.width}x"
                        f"{canvas.height}); {summary}" + (f" Warnings: {'; '.join(plan.warnings)}" if plan.warnings else ""),
            backend=BACKEND)

    @registry.handler("paint.draw_image")
    async def draw_image(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        source = Path(args["image_path"])
        if not source.is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"image not found: {source}")
        if source.stat().st_size > MAX_IMAGE_BYTES:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{source.name} is larger than {MAX_IMAGE_BYTES // 1_000_000} MB")
        try:
            with Image.open(source) as opened:
                image = opened.convert("RGB")
        except OSError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{source.name} is not an image I can read: {exc}") from exc
        if min(image.size) < 16:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{source.name} is too small ({image.width}x{image.height})")
        detail = str(args.get("detail") or vectorize.DEFAULT_DETAIL)
        style = str(args.get("style") or "fill")
        provider = await ctx.services.desktop()
        session = await _session(ctx)
        shots = ctx.artifact_dir("paint")
        reuse = bool(args.get("reuse_window")) and session.hwnd is not None

        def work() -> dict[str, Any]:
            driver = PaintDriver(provider, should_stop=ctx.cancel_event.is_set)
            transform = driver.attach(session.hwnd, session.offset) if reuse else driver.open_new()
            session.hwnd, session.offset = driver.hwnd, driver.offset
            driver.initial_state()
            canvas = transform.canvas
            if style == "sketch":
                traced = _sketch_objects(image, canvas, detail)
            else:
                traced = vectorize.vectorize(image, canvas, detail=detail, outline=bool(args.get("outline", True)))
            plan = geometry.plan_scene(traced.objects, canvas, check_overlaps=False)
            session.plan, session.tolerance = plan, TRACE_TOLERANCE
            planned = vectorize.fidelity(image, raster.render_plan(plan), canvas)
            result, gate = run_gated(
                plan, driver, request=f"redraw {source.name} in Paint ({style}, detail {detail})", directory=shots,
                source={"kind": "image", "name": source.name, "reference_path": str(source)}, reference=image,
                should_stop=ctx.cancel_event.is_set, tolerance=TRACE_TOLERANCE, user_supplied_plan=False)
            captured = driver.capture()
            achieved = vectorize.fidelity(image, captured, canvas)
            if style == "sketch":
                agreement = verify.ink_agreement(plan, captured, scale=transform.scale)
                result.ok = agreement["coverage"] >= SKETCH_COVERAGE_OK and agreement["stray"] <= SKETCH_STRAY_MAX
                traced.stats["ink"] = agreement
            stamp = int(time.time() * 1000)
            image_path = shots / f"canvas-{stamp}.png"
            captured.save(image_path, format="PNG")
            (shots / f"image-plan-{stamp}.json").write_text(json.dumps({
                "source": str(source), "detail": detail, "canvas": [canvas.width, canvas.height],
                "stats": traced.stats, "palette": traced.palette, "fidelity_planned": planned,
                "fidelity_canvas": achieved, "result": result.to_dict(),
                "polygons": [{"id": o.id, "layer": o.layer, "fill": o.fill, "points": o.points} for o in traced.objects],
            }, ensure_ascii=False), encoding="utf-8")
            return {"hwnd": driver.hwnd, "canvas": canvas, "scale": transform.scale, "plan": plan, "result": result,
                    "image": str(image_path), "stats": traced.stats, "planned": planned, "achieved": achieved, "gate": gate}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        result, canvas, plan = out["result"], out["canvas"], out["plan"]
        summary = result.final.summary()
        if style == "sketch":
            ink = out["stats"].get("ink", {})
            summary = (f"{ink.get('coverage', 0):.0%} of the planned pencil lines are on the canvas, "
                       f"{ink.get('stray', 0):.0%} of the ink is unplanned.")
        elif result.ok and result.final.failed_ids:
            summary = (f"{len(plan.objects) - len(result.final.failed_ids)} of {len(plan.objects)} regions are on the canvas "
                       f"as planned; {len(result.final.failed_ids)} thin ones are missing (within tolerance).")
        data = {
            "hwnd": out["hwnd"], "canvas": {"width": canvas.width, "height": canvas.height},
            "units": {"x": 1000, "y": round(canvas.unit_height)}, "scale": out["scale"],
            "objects": len(plan.objects), "drawn": result.drawn, "matches_plan": result.ok,
            "redrawn": result.redraws, "spill_ratio": round(result.final.spill_ratio, 4),
            "failed_objects": result.final.failed_ids, "warnings": [], "screenshot": out["image"],
            "source": str(source), "detail": detail, "colors": out["stats"]["colors"],
            "fidelity": out["achieved"], "fidelity_planned": out["planned"], "style": style,
            "specification": specification_summary(out["gate"]),
        }
        if style == "sketch":
            data["ink"] = out["stats"].get("ink", {})
        if not result.ok:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, summary, retryable=False,
                            details={"data": data, "failed": result.final.failed_ids,
                                     "report": result.final.to_dict(), "screenshot": out["image"]})
        score = out["achieved"]["ssim"]
        quality = "close" if score >= 0.75 else "recognisable" if score >= 0.55 else "rough"
        return ToolOutcome(
            data=data, checks=[Check("canvas_matches_plan", True, summary)],
            artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=f"Redrew {source.name} in Paint as {len(plan.objects)} "
                        f"{'coloured pencil strokes' if style == 'sketch' else 'filled regions'} in {out['stats']['colors']} colours "
                        f"(window {out['hwnd']}, canvas {canvas.width}x{canvas.height}); it matches the plan and is a {quality} "
                        f"likeness of the picture (structural similarity {score}). {summary}",
            backend=BACKEND)

    @registry.handler("paint.inspect")
    async def inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        session = await _session(ctx)
        if session.hwnd is None:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, "no Paint window has been opened by paint.draw yet")
        shots = ctx.artifact_dir("paint")

        def work() -> dict[str, Any]:
            driver = PaintDriver(provider, should_stop=ctx.cancel_event.is_set)
            transform = driver.attach(session.hwnd, session.offset)
            path = shots / f"inspect-{int(time.time() * 1000)}.png"
            image = driver.capture()
            image.save(path, format="PNG")
            report = None
            plan = session.plan
            if plan is not None and (plan.canvas.width, plan.canvas.height) == (image.width, image.height):
                report = verify.compare(plan, image, scale=transform.scale,
                                        fill_ok=verify.FILL_TRACED_OK if session.tolerance > 0 else verify.FILL_SAMPLE_OK,
                                        check_strokes=session.tolerance <= 0)
                if not report.ok and session.tolerance > 0 and report.spill_ratio <= verify.SPILL_OK:
                    if len(report.failed_ids) <= max(1, int(session.tolerance * len(plan.objects))):
                        report.ok = True
                        report.notes.append(f"{len(report.failed_ids)} of {len(plan.objects)} regions differ (within tolerance)")
            return {"canvas": transform.canvas, "scale": transform.scale, "tool": driver._active_tool(),
                    "image": str(path), "report": report}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        canvas, report = out["canvas"], out["report"]
        matches = None if report is None else report.ok
        data = {"hwnd": session.hwnd, "canvas": {"width": canvas.width, "height": canvas.height},
                "units": {"x": 1000, "y": round(canvas.unit_height)}, "scale": out["scale"], "tool": out["tool"],
                "screenshot": out["image"], "matches_plan": matches,
                "failed_objects": [] if report is None else report.failed_ids}
        verdict = ("no drawing has been made yet" if report is None else report.summary())
        return ToolOutcome(
            data=data, artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=f"Paint window {session.hwnd}: canvas {canvas.width}x{canvas.height}, tool {out['tool']}. {verdict}",
            backend=BACKEND)
