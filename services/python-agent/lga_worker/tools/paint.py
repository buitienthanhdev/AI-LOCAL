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
from lga_worker.paint import doraemon, geometry, quality, raster, saving, vectorize, verify
from lga_worker.paint.template_store import TemplateStore
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
    info: dict | None = None  # what a template drawing knows about itself (paint.quality.character_fidelity needs it)


async def _session(ctx: ToolContext) -> PaintSession:
    async def create() -> PaintSession:
        return PaintSession()

    return await ctx.services.get_or_create("paint", create)


def spec_observation(fn: Any) -> Any:
    """Append the artwork specification line to a drawing tool's observation (so the chat can show it)."""
    import functools

    @functools.wraps(fn)
    async def wrapper(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        out = await fn(ctx, args)
        spec = (out.data or {}).get("specification") if isinstance(out.data, dict) else None
        if spec:
            out.observation += (f" Specification {spec.get('status')} ({spec.get('state')}): {spec.get('objects')} objects, "
                                f"{spec.get('operations')} operations, about {spec.get('estimated_seconds')} s, "
                                f"{spec.get('assumptions')} assumption(s); file {spec.get('path')}")
        return out

    return wrapper


def open_driver(provider: Any, session: PaintSession, reuse: bool, should_stop: Any) -> tuple[PaintDriver, Any]:
    """A calibrated Paint driver: a new window, or the one this worker opened before (`reuse`). The one place that
    opens Paint for drawing, so the session (window, page offset) is kept the same way by every drawing tool."""
    driver = PaintDriver(provider, should_stop=should_stop)
    transform = driver.attach(session.hwnd, session.offset) if reuse else driver.open_new()
    session.hwnd, session.offset = driver.hwnd, driver.offset
    driver.initial_state()
    return driver, transform


def _scene_objects(args: dict[str, Any], canvas: Canvas) -> list:
    if args.get("template"):
        options = {k: str(v) for k, v in (args.get("options") or {}).items()}
        return build_template(str(args["template"]), canvas, **options)
    return [geometry.object_from_dict(o) for o in args["objects"]]


def _template_store(ctx: ToolContext) -> TemplateStore:
    return TemplateStore(ctx.config.path("paths.memory_dir") / "paint_templates")


def _reference_measurement(args: dict[str, Any]) -> dict[str, Any] | None:
    """Measure `reference_image` when one is given: AVAILABLE only when the picture was really read and analysed."""
    path = args.get("reference_image")
    if not path:
        return None
    source = Path(str(path))
    if not source.is_file():
        raise ToolError(ErrorCode.NOT_FOUND, f"reference image not found: {source}")
    try:
        with Image.open(source) as opened:
            image = opened.convert("RGB")
    except OSError as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{source.name} is not an image I can read: {exc}") from exc
    measured = doraemon.measure_reference(image, url=args.get("reference_url"), retrieved_at=args.get("reference_retrieved_at"),
                                          source=str(args.get("reference_url") or source))
    if measured.get("status") != "AVAILABLE":
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"the reference picture is unusable: {measured.get('note')}")
    return measured


def _is_doraemon(args: dict[str, Any]) -> bool:
    return str(args.get("template") or "").strip().lower() in ("doraemon", "doraemon_v2")


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
    @spec_observation
    async def draw(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        session = await _session(ctx)
        shots = ctx.artifact_dir("paint")
        reuse = bool(args.get("reuse_window")) and session.hwnd is not None

        def work() -> dict[str, Any]:
            driver, transform = open_driver(provider, session, reuse, ctx.cancel_event.is_set)
            canvas = transform.canvas
            info: dict[str, Any] | None = None
            source = {"kind": "template" if args.get("template") else "objects", "name": str(args.get("template") or "objects")}
            stamp = int(time.time() * 1000)
            candidate_id = str(args.get("candidate") or "") or None
            store = _template_store(ctx)
            if _is_doraemon(args):
                # preview and constraint check BEFORE the mouse moves: a scene that breaks its own proportions is
                # corrected here (or refused), never drawn
                base = store.params("doraemon")
                if candidate_id:
                    cand = store.candidate("doraemon", candidate_id)
                    if cand is None:
                        raise ToolError(ErrorCode.NOT_FOUND, f"no Doraemon template candidate {candidate_id!r}")
                    base = doraemon.Params.from_dict(cand["params"])
                prepared = doraemon.prepare(canvas, base, options={k: str(v) for k, v in (args.get("options") or {}).items()},
                                            reference=_reference_measurement(args))
                info = doraemon.scene_info(prepared, reviewed=bool((store.best("doraemon") or {}).get("reviewed")))
                objects = prepared.scene.objects
                source.update({"template_version": prepared.scene.version, "name": "doraemon",
                               "reference_status": prepared.reference.get("status", "OFFLINE_TEMPLATE"),
                               "reference_source": prepared.reference.get("source"),
                               "reference_retrieved_at": prepared.reference.get("retrieved_at"),
                               "measured": prepared.reference.get("measured", []), "object_confidence": 0.8})
            else:
                objects = _scene_objects(args, canvas)
            plan = geometry.plan_scene(objects, canvas)
            session.plan, session.tolerance, session.info = plan, 0.0, info
            if info is not None:
                raster.render_plan(plan).save(shots / f"preview-{stamp}.png", format="PNG")
                info["preview"] = str(shots / f"preview-{stamp}.png")

            def assess(p, img, res, shot, window_ok):  # noqa: ANN001
                return quality.assess(p, img, res, info=info, window_ok=window_ok, screenshot=shot)

            result, gate = run_gated(
                plan, driver, request=str(args.get("request") or f"paint.draw {args.get('template') or 'objects'}"),
                directory=shots, source=source, should_stop=ctx.cancel_event.is_set, user_supplied_plan=True, assess=assess)
            q = gate.quality
            learning = None
            if q is not None and info is not None and result.ok:
                try:
                    shot_path = (gate.spec or {}).get("execution", {}).get("actual_result", {}).get("screenshot")
                    row = store.record_run("doraemon", version=info["template_version"], params=info["params"], quality=q,
                                           screenshot=shot_path or "", reference_status=source["reference_status"])
                    learning = {"run": row["id"], "score": row["score"], "defects": row["defects"]}
                    if candidate_id:
                        promoted, why = store.promote("doraemon", candidate_id, quality=q,
                                                      constraints_ok=all(c["ok"] for c in info["constraints"]))
                        learning["candidate"] = {"id": candidate_id, "promoted": promoted, "refused_because": why}
                    else:
                        proposal = store.propose_candidate("doraemon")
                        if proposal:
                            learning["candidate_proposed"] = {"id": proposal["id"], "because": proposal["because"]}
                except ValueError as exc:
                    learning = {"not_recorded": str(exc)}
            if q is not None:
                q["report_path"] = quality.write_report(shots, f"{stamp}", {**q, "template": info and {
                    k: info[k] for k in ("template_version", "constraints", "corrections", "reference", "landmarks", "stroke_profile")}})
            stamp = int(time.time() * 1000)
            image_path = shots / f"canvas-{stamp}.png"
            driver.capture().save(image_path, format="PNG")
            (shots / f"plan-{stamp}.json").write_text(json.dumps({
                "canvas": [canvas.width, canvas.height], "args": args,
                "objects": [{"id": o.id, "kind": o.kind, "layer": o.layer, "stroke_px": o.stroke_px,
                             "fill": o.fill, "stroke": o.stroke, "points": o.points} for o in plan.objects],
                "result": result.to_dict(), "warnings": plan.warnings}, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"hwnd": driver.hwnd, "canvas": canvas, "scale": transform.scale, "plan": plan, "result": result,
                    "image": str(image_path), "gate": gate, "info": info, "learning": learning}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        result, canvas, plan = out["result"], out["canvas"], out["plan"]
        summary = result.final.summary()
        q = out["gate"].quality
        data = {
            "hwnd": out["hwnd"], "canvas": {"width": canvas.width, "height": canvas.height},
            "units": {"x": 1000, "y": round(canvas.unit_height)}, "scale": out["scale"],
            "objects": len(plan.objects), "drawn": result.drawn, "matches_plan": result.ok,
            "redrawn": result.redraws, "spill_ratio": round(result.final.spill_ratio, 4),
            "failed_objects": result.final.failed_ids, "warnings": plan.warnings, "screenshot": out["image"],
            "specification": specification_summary(out["gate"]),
        }
        if q is not None:
            data.update({"DRAW_EXECUTION_PASS": q["DRAW_EXECUTION_PASS"], "VISUAL_QUALITY_PASS": q["VISUAL_QUALITY_PASS"],
                         "quality": q})
        if out.get("info"):
            data["template"] = {k: out["info"][k] for k in ("template_version", "corrections", "preview", "reference")}
            data["template"]["constraints_ok"] = all(c["ok"] for c in out["info"]["constraints"])
        if out.get("learning"):
            data["learning"] = out["learning"]
        if not result.ok:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, summary, retryable=False,
                            details={"data": data, "failed": result.final.failed_ids,
                                     "report": result.final.to_dict(), "screenshot": out["image"]})
        verdict = ""
        if q is not None:
            verdict = " " + quality.summary_line(q) + "."
            problems = (q["visual_quality"]["problems"] + q["character_fidelity"].get("problems", [])
                        + q["canvas_safety"]["problems"])
            if problems:
                verdict += " Problems: " + "; ".join(problems[:4]) + "."
            verdict += (" Matching the plan is NOT proof of a good picture: look at the screenshot"
                        + (f" and the preview {out['info']['preview']}" if out.get("info") else "") + " before saying it looks right.")
            if q["character_fidelity"].get("limitations"):
                verdict += " Limits: " + "; ".join(q["character_fidelity"]["limitations"]) + "."
        return ToolOutcome(
            data=data, checks=[Check("canvas_matches_plan", True, summary)],
            artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=f"Drew {result.drawn} objects in Paint (window {out['hwnd']}, canvas {canvas.width}x"
                        f"{canvas.height}); {summary}" + (f" Warnings: {'; '.join(plan.warnings)}" if plan.warnings else "")
                        + verdict,
            backend=BACKEND)

    @registry.handler("paint.draw_image")
    @spec_observation
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
            driver, transform = open_driver(provider, session, reuse, ctx.cancel_event.is_set)
            canvas = transform.canvas
            if style == "sketch":
                traced = _sketch_objects(image, canvas, detail)
            else:
                traced = vectorize.vectorize(image, canvas, detail=detail, outline=bool(args.get("outline", True)))
            plan = geometry.plan_scene(traced.objects, canvas, check_overlaps=False)
            session.plan, session.tolerance, session.info = plan, TRACE_TOLERANCE, None
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

    @registry.handler("paint.save")
    async def save(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        session = await _session(ctx)
        if session.hwnd is None:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, "no Paint window has been opened by paint.draw yet")
        from lga_worker.paths import absolute_path

        path = absolute_path(str(args["path"]))
        saving.format_of(path)  # refuses an extension Paint does not write
        overwrite = bool(args.get("overwrite"))
        if path.exists() and not overwrite:
            raise ToolError(ErrorCode.CONFLICT, f"{path} already exists; pass overwrite=true to replace it (a backup copy is made first)")
        shots = ctx.artifact_dir("paint")

        def work() -> dict[str, Any]:
            driver = PaintDriver(provider, should_stop=ctx.cancel_event.is_set)
            transform = driver.attach(session.hwnd, session.offset)
            before = driver.capture()  # what the canvas shows right now: the saved file must show the same
            backup = saving.backup_existing(path) if overwrite else None
            started = time.time()
            saved = provider.save_as(session.hwnd, path, overwrite, max(5.0, min(30.0, ctx.remaining_s() - 2)))
            report = saving.verify_saved_file(path, (transform.canvas.width, transform.canvas.height), before, started_at=started)
            return {"saved": saved, "report": report, "backup": backup, "canvas": transform.canvas}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        report = out["report"]
        data = {"path": str(path), "size": path.stat().st_size if path.is_file() else 0, "format": saving.format_of(path),
                "canvas": {"width": out["canvas"].width, "height": out["canvas"].height},
                "verification": report.to_dict(), "backup": str(out["backup"]) if out["backup"] else None,
                "overwritten": out["backup"] is not None}
        if not report.ok:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, "the file was not saved correctly: " + "; ".join(report.failed()),
                            retryable=False, details={"data": data})
        return ToolOutcome(
            data=data, checks=[Check("saved_file_verified", True, f"{path} reopened: {data['format']} {data['canvas']['width']}x"
                                                                 f"{data['canvas']['height']}")],
            artifacts=[{"kind": "image", "path": str(path)}], backend=BACKEND,
            observation=f"Saved the canvas to {path} ({data['size']} bytes) and read it back: "
                        + ", ".join(c.name for c in report.checks) + " all passed."
                        + (f" The previous file was copied to {out['backup']}." if out["backup"] else ""))

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
            partial = None
            plan = session.plan
            if plan is not None and (plan.canvas.width, plan.canvas.height) == (image.width, image.height):
                partial = {"canvas_safety": quality.canvas_safety(plan, image, window_ok=None, screenshot=str(path)),
                           "character_fidelity": quality.character_fidelity(plan, image, session.info)}
                report = verify.compare(plan, image, scale=transform.scale,
                                        fill_ok=verify.FILL_TRACED_OK if session.tolerance > 0 else verify.FILL_SAMPLE_OK,
                                        check_strokes=session.tolerance <= 0)
                if not report.ok and session.tolerance > 0 and report.spill_ratio <= verify.SPILL_OK:
                    if len(report.failed_ids) <= max(1, int(session.tolerance * len(plan.objects))):
                        report.ok = True
                        report.notes.append(f"{len(report.failed_ids)} of {len(plan.objects)} regions differ (within tolerance)")
            return {"canvas": transform.canvas, "scale": transform.scale, "tool": driver._active_tool(),
                    "image": str(path), "report": report, "partial": partial}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        canvas, report = out["canvas"], out["report"]
        matches = None if report is None else report.ok
        data = {"hwnd": session.hwnd, "canvas": {"width": canvas.width, "height": canvas.height},
                "units": {"x": 1000, "y": round(canvas.unit_height)}, "scale": out["scale"], "tool": out["tool"],
                "screenshot": out["image"], "matches_plan": matches,
                "failed_objects": [] if report is None else report.failed_ids}
        if out["partial"] is not None:
            data["quality"] = out["partial"]  # safety and fidelity only: execution and visual quality need a drawing run
        verdict = ("no drawing has been made yet" if report is None else report.summary())
        return ToolOutcome(
            data=data, artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=f"Paint window {session.hwnd}: canvas {canvas.width}x{canvas.height}, tool {out['tool']}. {verdict}",
            backend=BACKEND)
