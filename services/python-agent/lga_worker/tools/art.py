"""Handler of art.draw (contract: tools/art.json): one drawing brain for every application that can draw.

The picture comes from one source (a photo, SVG, a template, or hand-placed objects) and is drawn by the backend that
fits the application: Paint (mouse, filled polygons), AutoCAD (coloured pencil strokes with hidden lines removed),
PowerPoint / Word / Excel (freeform shapes through COM), or SVG + PNG files for anything else. Whatever the backend
is, the result is verified against what should be there and reported; nothing is claimed that is not on the canvas.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image

from lga_worker.art import capabilities, convert, export
from lga_worker.art import office as office_art
from lga_worker.art import svg as svg_art
from lga_worker.art.scene import Scene, render
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import geometry, raster, vectorize, verify
from lga_worker.paint.driver import PaintDriver
from lga_worker.art.gate import run_gated, specification_summary
from lga_worker.paint.engine import run_plan  # noqa: F401
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.tools import paint as paint_tools
from lga_worker.tools.paint import spec_observation
from lga_worker.tools.paint import open_driver

BACKEND = "art-brain"
MAX_IMAGE_BYTES = 40_000_000
MAX_SVG_BYTES = 4_000_000
DETAILS = tuple(vectorize.DETAIL)
OBJECT_TOLERANCE = 0.0  # a hand-made picture has to match exactly
TRACED_TOLERANCE = paint_tools.TRACE_TOLERANCE
NEAR_MISS_WINDOW_S = 900.0  # a picture with floating parts is refused once per window; after that they are attached


async def _state() -> dict[str, float]:
    return {"near_miss_at": -1e9}


def _load_image(path_text: str) -> tuple[Path, Image.Image]:
    source = Path(path_text)
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
    return source, image


def _svg_text(args: dict[str, Any]) -> tuple[str, str]:
    if args.get("svg"):
        return str(args["svg"]), "svg"
    path = Path(str(args["svg_path"]))
    if not path.is_file():
        raise ToolError(ErrorCode.NOT_FOUND, f"svg file not found: {path}")
    if path.stat().st_size > MAX_SVG_BYTES:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path.name} is larger than {MAX_SVG_BYTES // 1_000_000} MB")
    return path.read_text(encoding="utf-8", errors="replace"), path.name


def _sources(args: dict[str, Any]) -> str:
    given = [k for k in ("image_path", "svg", "svg_path", "template", "objects") if args.get(k)]
    if len(given) != 1:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        "give exactly one picture source: image_path, svg, svg_path, template or objects"
                        + (f" (got {', '.join(given)})" if given else ""))
    return given[0]


def _quality(score: float) -> str:
    return "close" if score >= 0.75 else "recognisable" if score >= 0.55 else "rough"


def register(registry: ToolRegistry) -> None:
    @registry.handler("art.draw")
    @spec_observation
    async def draw(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        kind = _sources(args)
        profile = capabilities.resolve(args.get("app"))
        if profile is None:
            raise ToolError(ErrorCode.UNSUPPORTED,
                            f"I cannot drive {args.get('app')!r} for drawing yet. Supported: {capabilities.supported()}. "
                            "Use app 'file' to write the picture as SVG and PNG, then open or import it in that program.")
        style, style_note = capabilities.style_for(profile, args.get("style"))
        detail = str(args.get("detail") or ("max" if profile.backend == "paint" else "ultra"))
        if detail not in DETAILS:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"detail must be one of {', '.join(DETAILS)}")
        notes: list[str] = [style_note] if style_note else []
        if profile.backend == "paint" and detail == "ultra":
            detail = "max"
            notes.append("Paint is drawn by mouse: detail 'ultra' is limited to 'max'")
        colors = int(args.get("colors") or convert.FREE_COLORS)
        image: Image.Image | None = None
        source_name = kind
        scene: Scene | None = None

        # ---- the picture as pure computation (off the event loop)
        if kind == "image_path":
            path, image = await asyncio.to_thread(_load_image, str(args["image_path"]))
            source_name = path.name
        elif kind in ("svg", "svg_path"):
            text, source_name = _svg_text(args)
            scene = await asyncio.to_thread(svg_art.parse_svg, text)
            notes.extend(scene.notes)
            floor = convert.MIN_SHAPES.get(detail, 0)
            if kind == "svg" and len(scene.shapes) < floor:
                raise ToolError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"This SVG has only {len(scene.shapes)} shapes; a detailed picture needs at least {floor}. Add the parts that "
                    "are missing and call art.draw again: a darker outline on every part, inner ears / pupils / eye and "
                    "nose highlights, mouth, teeth, whiskers or fur strokes, cheeks, a lighter highlight and a darker shade "
                    "on every big part, a shadow under the subject, and background elements (ground, sky, plants, clouds, "
                    "furniture). If the user explicitly asked for a plain geometric figure (a circle, a square), call again "
                    "with detail 'low'.")
            if kind == "svg":
                from lga_worker.art import critique as critique_mod

                review = critique_mod.critique(scene)
                notes.extend(critique_mod.summary(review))
            groups = convert.detached_groups(scene) if kind == "svg" else []
            if groups:
                state = await ctx.services.get_or_create("art", _state)
                now = time.monotonic()
                if now - state["near_miss_at"] > NEAR_MISS_WINDOW_S:
                    state["near_miss_at"] = now
                    parts = "; ".join(f"the group in box {g['box']} (x0,y0,x1,y1) hangs {g['gap']:g} units off the rest "
                                      "of the picture" for g in groups[:4])
                    raise ToolError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"Parts of this picture float apart: {parts}. Parts of one subject must overlap (an ear over the head, "
                        "a head over the body with a neck between, a tail over the body, legs under the body). Move them or add "
                        "a connecting shape and call art.draw again.")
                moved = convert.attach_detached(scene)
                if moved:
                    notes.append(f"{moved} floating part(s) were moved until they touch the body")
        elif kind == "template" and profile.backend != "paint":
            scene = convert.scene_from_template(str(args["template"]), {k: str(v) for k, v in (args.get("options") or {}).items()})
            source_name = str(args["template"])
        elif kind == "objects" and profile.backend != "paint":
            scene = convert.scene_from_objects([geometry.object_from_dict(o) for o in args["objects"]])
        if kind == "image_path" and profile.backend != "paint":
            assert image is not None
            scene = await asyncio.to_thread(convert.scene_from_image, image, style=style, detail=detail, colors=colors,
                                            outline=bool(args.get("outline", False)))
            notes.extend(scene.notes)
        ctx.check_cancelled()

        shots = ctx.artifact_dir("art")
        stamp = int(time.time() * 1000)
        if profile.backend == "paint":
            return await _draw_paint(ctx, args, kind, style, detail, image, scene, source_name, notes, shots, stamp)
        assert scene is not None
        if not scene.shapes:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "the picture has nothing to draw")
        if len(scene.shapes) > profile.max_shapes:
            notes.append(f"{len(scene.shapes)} shapes exceed what {profile.title} handles well ({profile.max_shapes})")
        reference = render(scene, convert.SHEET.width)
        likeness = None
        if image is not None:
            likeness = await asyncio.to_thread(vectorize.fidelity, image, reference, convert.SHEET)
        elif kind in ("svg", "svg_path"):
            likeness = None  # an SVG is its own reference

        if profile.backend == "file":
            return await _draw_file(ctx, args, scene, source_name, notes, likeness, shots, stamp)
        if profile.backend == "autocad":
            return await _draw_autocad(ctx, args, scene, style, source_name, notes, likeness, image, shots, stamp)
        return await _draw_office(ctx, args, profile.key, scene, source_name, notes, likeness, shots, stamp)

    # ------------------------------------------------------------------------------------------------ backends

    async def _draw_paint(ctx: ToolContext, args: dict[str, Any], kind: str, style: str, detail: str,
                          image: Image.Image | None, scene: Scene | None, source_name: str, notes: list[str],
                          shots: Path, stamp: int) -> ToolOutcome:
        provider = await ctx.services.desktop()
        session = await paint_tools._session(ctx)
        reuse = bool(args.get("reuse_window")) and session.hwnd is not None
        outline = bool(args.get("outline", True))
        tolerance = OBJECT_TOLERANCE

        def work() -> dict[str, Any]:
            nonlocal tolerance
            driver, transform = open_driver(provider, session, reuse, ctx.cancel_event.is_set)
            canvas = transform.canvas
            report: dict[str, Any] = {}
            reference: Image.Image | None = image
            if kind == "image_path":
                assert image is not None
                if style == "sketch":
                    traced = paint_tools._sketch_objects(image, canvas, detail)
                else:
                    traced = vectorize.vectorize(image, canvas, detail=detail, outline=outline)
                objects = traced.objects
                report = dict(traced.stats)
                tolerance = TRACED_TOLERANCE
            elif kind in ("template", "objects"):
                objects = paint_tools._scene_objects(args, canvas)
            else:
                assert scene is not None
                objects, report = convert.to_paint_objects(scene, canvas)
                reference = render(scene, canvas.width, round(canvas.width * scene.height / scene.width))
                tolerance = 0.0 if len(objects) <= 60 else TRACED_TOLERANCE
            plan = geometry.plan_scene(objects, canvas, check_overlaps=False)
            session.plan, session.tolerance = plan, tolerance
            planned = vectorize.fidelity(reference, raster.render_plan(plan), canvas) if reference is not None else None
            result, gate = run_gated(
                plan, driver, request=str(args.get("request") or f"art.draw {source_name} in Paint ({style})"), directory=shots,
                source={"kind": "image" if kind == "image_path" else kind, "name": source_name,
                        "reference_path": str(args.get("image_path") or "") or None},
                reference=image, should_stop=ctx.cancel_event.is_set, tolerance=tolerance,
                user_supplied_plan=kind in ("template", "objects", "svg", "svg_path"))
            captured = driver.capture()
            achieved = vectorize.fidelity(reference, captured, canvas) if reference is not None else None
            if kind == "image_path" and style == "sketch":
                agreement = verify.ink_agreement(plan, captured, scale=transform.scale)
                result.ok = (agreement["coverage"] >= paint_tools.SKETCH_COVERAGE_OK
                             and agreement["stray"] <= paint_tools.SKETCH_STRAY_MAX)
                report["ink"] = agreement
            image_path = shots / f"canvas-{stamp}.png"
            captured.save(image_path, format="PNG")
            (shots / f"plan-{stamp}.json").write_text(json.dumps({
                "source": source_name, "kind": kind, "style": style, "detail": detail, "canvas": [canvas.width, canvas.height],
                "report": report, "fidelity_planned": planned, "fidelity_canvas": achieved, "result": result.to_dict(),
                "objects": [{"id": o.id, "kind": o.kind, "layer": o.layer, "fill": o.fill, "stroke": o.stroke,
                             "points": o.points} for o in plan.objects]}, ensure_ascii=False), encoding="utf-8")
            return {"hwnd": driver.hwnd, "canvas": canvas, "scale": transform.scale, "plan": plan, "result": result,
                    "image": str(image_path), "report": report, "achieved": achieved, "gate": gate}

        out = await provider.executor.run(max(5.0, ctx.remaining_s()), work)
        result, canvas, plan = out["result"], out["canvas"], out["plan"]
        summary = result.final.summary()
        if kind == "image_path" and style == "sketch":
            ink = out["report"].get("ink", {})
            summary = (f"{ink.get('coverage', 0):.0%} of the planned pencil lines are on the canvas, "
                       f"{ink.get('stray', 0):.0%} of the ink is unplanned.")
        elif result.ok and result.final.failed_ids:
            summary = (f"{len(plan.objects) - len(result.final.failed_ids)} of {len(plan.objects)} shapes are on the canvas "
                       f"as planned; {len(result.final.failed_ids)} thin ones are missing (within tolerance).")
        data: dict[str, Any] = {
            "app": "paint", "backend": "paint", "style": style, "source": source_name, "hwnd": out["hwnd"],
            "canvas": {"width": canvas.width, "height": canvas.height}, "units": {"x": 1000, "y": round(canvas.unit_height)},
            "objects": len(plan.objects), "drawn": result.drawn, "matches_plan": result.ok, "redrawn": result.redraws,
            "spill_ratio": round(result.final.spill_ratio, 4), "failed_objects": result.final.failed_ids,
            "screenshot": out["image"], "detail": detail, "notes": notes,
            "specification": specification_summary(out["gate"]),
            "report": {k: v for k, v in out["report"].items() if k in ("regions", "colors", "dropped_small", "custom_colors")},
        }
        if out["achieved"] is not None:
            data["fidelity"] = out["achieved"]
        if not result.ok:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, summary, retryable=False,
                            details={"data": data, "failed": result.final.failed_ids, "report": result.final.to_dict(),
                                     "screenshot": out["image"]})
        likeness = ""
        if out["achieved"] is not None:
            score = out["achieved"]["ssim"]
            likeness = f" It is a {_quality(score)} likeness of the picture (structural similarity {score})."
        what = "coloured pencil strokes" if (kind == "image_path" and style == "sketch") else "shapes"
        return ToolOutcome(
            data=data, checks=[Check("canvas_matches_plan", True, summary)],
            artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=(f"Drew {source_name} in Paint as {len(plan.objects)} {what} (window {out['hwnd']}, canvas "
                         f"{canvas.width}x{canvas.height}); it matches the plan.{likeness} {summary}"
                         + (f" Notes: {'; '.join(notes)}" if notes else "")),
            backend="art-brain/paint")

    async def _draw_autocad(ctx: ToolContext, args: dict[str, Any], scene: Scene, style: str, source_name: str,
                            notes: list[str], likeness: dict[str, float] | None, image: Image.Image | None,
                            shots: Path, stamp: int) -> ToolOutcome:
        from lga_worker.paint import sketch as sketching
        from lga_worker.tools import autocad as autocad_tools

        strokes = await asyncio.to_thread(convert.to_pencil_strokes, scene, shading=bool(args.get("shading", True)))
        if not strokes:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "the picture has no lines to draw")
        width = float(args.get("width") or 1000.0)
        scale = width / scene.width
        height = scene.height * scale
        cad = [(tuple((x * scale, height - y * scale) for x, y in pts), rgb, closed) for pts, rgb, closed in strokes]
        provider = await autocad_tools._provider(ctx)
        paper = str(args.get("paper") or "white")
        result = await autocad_tools._sta(ctx, provider, provider.draw_strokes, cad, bool(args.get("new_document", True)),
                                          paper, sketching.aci_nearest, budget_s=max(60.0, ctx.remaining_s() - 5.0))
        added = result.entities_after - result.entities_before
        complete = result.drawn == len(cad) and added == result.drawn
        pencil = Scene([convert.Shape(tuple(pts), closed, None, rgb, 0.5) for pts, rgb, closed in strokes])
        ssim = None
        if image is not None:
            ssim = (await asyncio.to_thread(vectorize.fidelity, image, render(pencil, convert.SHEET.width), convert.SHEET))["ssim"]
        preview = shots / f"art-{stamp}-preview.png"
        await asyncio.to_thread(lambda: render(pencil, 1280).save(preview, format="PNG"))
        data = {"app": "autocad", "backend": "autocad", "style": "sketch", "source": source_name, "preview": str(preview),
                "document": result.document, "strokes": result.drawn, "layers": result.layers,
                "true_color": result.true_color, "background": result.background, "sheet": [width, round(height, 1)],
                "notes": notes, "stats": scene.stats()}
        if ssim is not None:
            data["fidelity"] = {"ssim": ssim}
        extra = ""
        if result.background == "white":
            extra = (f" The model-space background was set to white (it was {result.previous_background:#08x}) so lines of any "
                     "colour show.")
        return ToolOutcome(
            data=data, checks=[Check("strokes_drawn", complete, f"{added} entities added for {len(cad)} strokes")],
            artifacts=[{"kind": "screenshot", "path": str(preview)}],
            observation=(f"Drew {source_name} in {result.document} as {result.drawn} coloured pencil strokes on "
                         f"{result.layers} colour layers ({'true colour' if result.true_color else 'nearest index colours'}), "
                         f"hidden lines removed, one undo step; sheet {width:g} x {height:.0f} units."
                         + (f" Likeness: structural similarity {ssim}." if ssim is not None else "") + extra
                         + (f" Notes: {'; '.join(notes)}" if notes else "")),
            backend="art-brain/autocad")

    async def _draw_office(ctx: ToolContext, args: dict[str, Any], app_key: str, scene: Scene, source_name: str,
                           notes: list[str], likeness: dict[str, float] | None, shots: Path, stamp: int) -> ToolOutcome:
        from lga_worker.tools import office as office_tools

        fn = functools.partial(office_art.draw_scene, app_key, scene, shots_dir=shots, stem=f"art-{stamp}",
                               save_path=None, should_stop=ctx.cancel_event.is_set)
        out = await office_tools._run(ctx, fn)
        if not out.get("screenshot"):  # Word and Excel cannot export a page picture: show the scene that was drawn
            preview = shots / f"art-{stamp}-preview.png"
            await asyncio.to_thread(lambda: render(scene, 1280).save(preview, format="PNG"))
            out["screenshot"] = str(preview)
            out["screenshot_is"] = "scene as planned (the document itself is open in the application)"
        data = {**out, "backend": "office-com", "style": "fill", "source": source_name, "notes": notes, "stats": scene.stats()}
        if likeness is not None:
            data["fidelity"] = likeness
        detail = (f"{out['shapes']} shapes in the document for {out['expected']} planned"
                  + (f"; slide matches the scene (similarity {out['ssim']})" if "ssim" in out else
                     f"; {out['readback']['checked']} shapes read back, {out['readback']['mismatched']} differ"))
        if not out["ok"]:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, f"{app_key}: {detail}", retryable=False, details={"data": data})
        title = capabilities.resolve(app_key).title  # type: ignore[union-attr]
        score = f" Likeness to the source: {_quality(likeness['ssim'])} (structural similarity {likeness['ssim']})." if likeness else ""
        return ToolOutcome(
            data=data, checks=[Check("shapes_match_scene", True, detail)],
            artifacts=[{"kind": "screenshot", "path": out["screenshot"]}] if out.get("screenshot") else [],
            observation=(f"Drew {source_name} in {title} ({out['document']}) as {out['drawn']} editable shapes in "
                         f"{len(scene.colors())} colours in {out['seconds']} s; {detail}.{score}"
                         + (f" Saved to {out['saved']}." if out.get("saved") else " The document is open and unsaved.")
                         + (f" Notes: {'; '.join(notes)}" if notes else "")),
            backend="art-brain/office")

    async def _draw_file(ctx: ToolContext, args: dict[str, Any], scene: Scene, source_name: str, notes: list[str],
                         likeness: dict[str, float] | None, shots: Path, stamp: int) -> ToolOutcome:
        if args.get("output_path"):
            target = Path(str(args["output_path"]))
            directory, stem = target.parent, target.stem
        else:
            directory, stem = shots, f"art-{stamp}"
        svg_path, png_path = await asyncio.to_thread(export.write_files, scene, directory, stem)
        ok = svg_path.stat().st_size > 0 and png_path.stat().st_size > 0
        data = {"app": "file", "backend": "file", "svg": str(svg_path), "png": str(png_path), "source": source_name,
                "notes": notes, "stats": scene.stats()}
        if likeness is not None:
            data["fidelity"] = likeness
        return ToolOutcome(
            data=data, checks=[Check("files_written", ok, f"{svg_path.name} {svg_path.stat().st_size} bytes, "
                                                          f"{png_path.name} {png_path.stat().st_size} bytes")],
            artifacts=[{"kind": "file", "path": str(svg_path)}, {"kind": "screenshot", "path": str(png_path)}],
            observation=(f"Wrote {source_name} as {len(scene.shapes)} vector shapes: SVG {svg_path} and PNG {png_path}. "
                         "Open or import either in the drawing program."
                         + (f" Notes: {'; '.join(notes)}" if notes else "")),
            backend="art-brain/file")
