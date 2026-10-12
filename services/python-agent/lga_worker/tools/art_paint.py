"""art.brief and art.paint (contract: tools/art.json): the art-intelligence layer in front of the Paint engine.

art.brief  - classify the request, identify a named work, choose medium and rendering mode, fetch a reference picture
             (Wikimedia Commons) when a real work must be reproduced. Pure analysis + one optional download.
art.paint  - draw a picture in Paint in a chosen rendering mode (pencil, ink, oil, watercolour, lacquer...), from a
             reference picture or an SVG, with the state machine, brush types, verification and honest reporting.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image

from lga_worker.art import brief as brief_mod
from lga_worker.art import convert, reference, stylize
from lga_worker.art import svg as svg_art
from lga_worker.art.knowledge import ArtKnowledgeStore, ReferenceRecord
from lga_worker.art.modes import SPECS, Mode
from lga_worker.art.scene import Scene, Shape, luminance, render
from lga_worker.art.state import ArtRun, State
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import geometry, raster, vectorize, verify
from lga_worker.paint.driver import PaintDriver
from lga_worker.art.gate import run_gated, specification_summary
from lga_worker.paint.engine import run_plan  # noqa: F401
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.tools import paint as paint_tools
from lga_worker.tools.art import _load_image, _quality

BACKEND = "art-paint"
STROKE_MODES = {Mode.PENCIL_SKETCH, Mode.CHARCOAL_SKETCH, Mode.INK_LINE_ART, Mode.PAINTERLY, Mode.OIL_PAINT_SIMULATION}
PAINT_STROKE_BUDGET = 1100
ART_TOLERANCE = 0.10
COVERAGE_OK = 0.55
STRAY_MAX = 0.20
MAX_SECONDS = 1500.0
COLOUR_ERROR_MAX = 0.12


def _store(ctx: ToolContext) -> ArtKnowledgeStore:
    return ArtKnowledgeStore(root=ctx.config.path("paths.artifacts_dir").parent / "memory")


def _aspect(size: str) -> float | None:
    """width / height from '73.7 x 92.1 cm' (height x width as museums list it)."""
    import re

    m = re.match(r"\s*([\d.]+)\s*x\s*([\d.]+)", size or "")
    return float(m.group(2)) / float(m.group(1)) if m else None


def _greyscale_scene(scene: Scene, ink_only: bool) -> Scene:
    """Colours of a scene replaced by their tone (a no-colour mode must not contain colour)."""
    def conv(c):
        if c is None:
            return None
        g = luminance(c)
        if ink_only:
            g = 0.0 if g < 0.55 else 1.0
        v = round(255 * g)
        return (v, v, v)

    return Scene([Shape(s.points, s.closed, conv(s.fill), conv(s.stroke), s.width, s.hint) for s in scene.shapes],
                 scene.width, scene.height, conv(scene.background), list(scene.notes))


def register(registry: ToolRegistry) -> None:
    @registry.handler("art.brief")
    async def art_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        store = _store(ctx)
        has_ref = bool(args.get("image_path"))
        b = brief_mod.build_brief(
            str(args["request"]), store=store, subject=args.get("subject"), artwork_title=args.get("artwork_title"),
            artist=args.get("artist"), intent=args.get("intent"), medium=args.get("medium"), mode=args.get("mode"),
            style=args.get("style"), has_reference=has_ref, offline=bool(args.get("offline")))
        data = b.to_dict()
        work = store.by_key(b.artwork["key"]) if b.artwork else None
        fetched: dict[str, Any] | None = None
        if work is not None and b.resolution == "IDENTIFIED" and b.needs_reference and args.get("fetch", True):
            cached = store.reference_for(work.key)
            if cached is not None:
                fetched = {**cached.__dict__, "cached": True}
            else:
                folder = ctx.config.path("paths.artifacts_dir").parent / "memory" / "art" / "references"
                last: ToolError | None = None
                for q in b.reference_queries or [f"{work.titles[0]} {work.artist}"]:
                    try:
                        must = [w for w in work.titles[0].lower().split() if len(w) > 3][:1]
                        got = await asyncio.to_thread(reference.fetch, q, folder, must_include=must, aspect=_aspect(work.size))
                        store.remember_reference(ReferenceRecord(
                            artwork_key=work.key, path=got.path, source_url=got.source_url, page_url=got.page_url,
                            title=got.title, license=got.license, sha256=got.sha256, retrieved_at=got.retrieved_at,
                            width=got.width, height=got.height, note="title match only; identity not proven by pixels"))
                        fetched = {**got.to_dict(), "cached": False}
                        break
                    except ToolError as exc:
                        last = exc
                if fetched is None and last is not None:
                    data["notes"] = [*data["notes"], f"reference not fetched: {last.message}"]
                    data["verification"] = brief_mod.Verification.BUILTIN_KNOWLEDGE.value
        if fetched:
            data["reference"] = fetched
            data["verification"] = brief_mod.Verification.REFERENCE_FETCHED.value
            data["image_path"] = fetched["path"]
        run = ArtRun(task_id=ctx.task_id, request=str(args["request"]))
        run.go(State.CLASSIFIED, kinds=b.kinds)
        run.go(State.REFERENCE_RESOLVED, resolution=b.resolution, verification=data["verification"])
        run.go(State.ART_ANALYZED)
        run.go(State.MEDIUM_SELECTED, medium=b.medium)
        run.go(State.RENDERING_MODE_SELECTED, mode=b.mode)
        run.info["brief"] = data
        data["state"] = run.state.value
        run.write(ctx.artifact_dir("art"), f"brief-{int(time.time() * 1000)}")
        wait = "AMBIGUOUS" if b.resolution == "AMBIGUOUS" else None
        facts = ""
        if b.artwork:
            a = b.artwork
            facts = (f" {a['title']} — {a['artist']}, {a['year']}; {a['movement']}; {a['medium']}. Recognisable by: "
                     + "; ".join(a["signature"][:4]) + ".")
        ref_line = (f" Reference picture saved: {fetched['path']} (Wikimedia Commons, {fetched['license']}, {fetched['page_url']}); "
                    "title match only, identity not proven by pixels.") if fetched else ""
        return ToolOutcome(
            data=data,
            observation=(f"Classified {'+'.join(b.kinds)}; {b.resolution}"
                         f"{' (' + b.intent + ')' if b.intent else ''}; verification {data['verification']}; medium "
                         f"{b.medium or 'unspecified'}; rendering mode {b.mode} ({b.mode_label}).{facts}{ref_line} "
                         f"{'QUESTION FOR THE USER: ' + ' '.join(b.questions) if wait else ''}"
                         f"{' Notes: ' + '; '.join(b.notes) if b.notes else ''}").strip(),
            backend=BACKEND)

    @registry.handler("art.paint")
    async def art_paint(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        run = ArtRun(task_id=ctx.task_id, request=str(args.get("request") or ""))
        mode = Mode(str(args["mode"]).upper()) if args.get("mode") else None
        if mode is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "mode is required (take it from art.brief)")
        spec = SPECS[mode]
        sources = [k for k in ("image_path", "svg", "svg_path") if args.get(k)]
        if len(sources) != 1:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "give exactly one of image_path, svg, svg_path")
        notes: list[str] = []
        image: Image.Image | None = None
        name = sources[0]
        detail = str(args.get("detail") or "max")
        if detail == "ultra":
            detail = "max"
        max_strokes = int(args.get("max_strokes") or PAINT_STROKE_BUDGET)
        run.go(State.CLASSIFIED)
        run.go(State.REFERENCE_RESOLVED, source=name)
        run.go(State.ART_ANALYZED)
        run.go(State.MEDIUM_SELECTED)
        run.go(State.RENDERING_MODE_SELECTED, mode=mode.value)

        # ---- the picture as a Scene in the chosen mode (deterministic computation)
        if name == "image_path":
            path, image = await asyncio.to_thread(_load_image, str(args["image_path"]))
            name = path.name
            styled = await asyncio.to_thread(stylize.stylize, image, mode, detail=detail, max_strokes=max_strokes)
            scene, stats = styled.scene, styled.stats
            notes.extend(styled.notes)
        else:
            text = str(args["svg"]) if args.get("svg") else Path(str(args["svg_path"])).read_text(encoding="utf-8", errors="replace")
            scene = await asyncio.to_thread(svg_art.parse_svg, text)
            notes.extend(scene.notes)
            if spec.colour in ("none", "grey"):
                scene = _greyscale_scene(scene, ink_only=spec.colour == "none")
                notes.append(f"{mode.value}: the SVG's colours were converted to {'black ink' if spec.colour == 'none' else 'greys'}")
            stats = scene.stats()
        if not scene.shapes:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "the picture has nothing to draw")
        ctx.check_cancelled()

        provider = await ctx.services.desktop()
        session = await paint_tools._session(ctx)
        shots = ctx.artifact_dir("art")
        stamp = int(time.time() * 1000)
        reuse = bool(args.get("reuse_window")) and session.hwnd is not None
        resume = bool(args.get("resume")) and reuse
        brush_mode = mode in STROKE_MODES

        def work() -> dict[str, Any]:
            driver = PaintDriver(provider, should_stop=ctx.cancel_event.is_set)
            transform = driver.attach(session.hwnd, session.offset) if reuse else driver.open_new()
            session.hwnd, session.offset = driver.hwnd, driver.offset
            driver.initial_state()
            caps = driver.discover()
            run.info["capabilities"] = {k: caps.get(k) for k in ("version", "brushes", "tools")}
            canvas = transform.canvas
            run.go(State.CANVAS_CALIBRATED, canvas=[canvas.width, canvas.height], version=caps.get("version"))
            # the first brush of the mode that this Paint really has (never assumed)
            available = set(caps.get("brushes") or []) | {"Pencil"}
            brush = next((b for b in spec.brushes if b in available), None)
            if brush_mode and brush is None:
                brush = "Brush"
            if brush_mode and brush != spec.brushes[0]:
                notes.append(f"brush {spec.brushes[0]!r} is not offered by this Paint: {brush!r} used instead")
            objects, report = convert.to_paint_objects(scene, canvas, brush=brush if brush_mode else None, exact_width=brush_mode)
            plan = geometry.plan_scene(objects, canvas, check_overlaps=False)
            run.go(State.PLAN_CREATED, objects=len(plan.objects))
            session.plan, session.tolerance = plan, ART_TOLERANCE
            planned = vectorize.fidelity(image, raster.render_plan(plan), canvas) if image is not None else None
            run.go(State.PLAN_VALIDATED, objects=len(plan.objects), warnings=len(plan.warnings))
            driver.check_every = 4
            run.go(State.EXECUTING)
            result, gate = run_gated(
                plan, driver, request=run.request or f"{mode.value} drawing of {name}", directory=shots,
                brief=args.get("brief") if isinstance(args.get("brief"), dict) else None, mode=mode,
                source={"kind": "image" if image is not None else "svg", "name": name,
                        "reference_path": str(args.get("image_path") or "") or None},
                reference=image, should_stop=ctx.cancel_event.is_set, tolerance=ART_TOLERANCE, resume=resume,
                user_supplied_plan=image is None)
            run.info["specification"] = gate.spec_path
            run.counters["actions"] = len(plan.objects)
            run.counters["actions_ok"] = len(plan.objects) - len(result.final.failed_ids)
            run.counters["retries"] = sum(result.redraws.values())
            run.go(State.VERIFYING)
            captured = driver.capture()
            achieved = vectorize.fidelity(image, captured, canvas) if image is not None else None
            ink = None
            if brush_mode:
                ink = verify.ink_agreement(plan, captured, scale=transform.scale)
                result.ok = ink["coverage"] >= COVERAGE_OK and ink["stray"] <= STRAY_MAX and (
                    result.ok or not result.final.failed_ids)
            # colours, not just ink: what is on the canvas must look like what was planned (a translucent brush or a
            # wrong colour slot leaves ink in the right places in the wrong colours)
            import numpy as np

            want = np.asarray(raster.render_plan(plan).convert("RGB"), np.float32)
            got = np.asarray(captured.convert("RGB"), np.float32)
            colour_error = float(np.abs(want - got).mean() / 255.0)
            if colour_error > COLOUR_ERROR_MAX:
                result.ok = False
                notes.append(f"colours on the canvas differ from the plan (mean error {colour_error:.0%}): "
                             "a brush opacity or colour setting was not applied")
            out = shots / f"paint-{stamp}.png"
            captured.save(out, format="PNG")
            return {"result": result, "canvas": canvas, "plan": plan, "image": str(out), "ink": ink, "achieved": achieved,
                    "planned": planned, "report": report, "brush": brush, "hwnd": driver.hwnd,
                    "colour_error": round(colour_error, 3), "gate": gate}

        try:
            out = await provider.executor.run(max(5.0, min(ctx.remaining_s(), MAX_SECONDS + 300)), work)
        except ToolError as exc:
            run.fail(exc.message, code=str(exc.code))
            run.write(shots, f"run-{stamp}")
            raise
        result, canvas, plan = out["result"], out["canvas"], out["plan"]
        fills = result.skipped_fills
        problems = []
        if fills:
            problems.append(f"{len(fills)} fill(s) refused because the region was not closed: {', '.join(fills)}")
        if not result.ok:
            run.go(State.CORRECTING)
        report_path = run.write(shots, f"run-{stamp}")
        data = {
            "app": "paint", "mode": mode.value, "mode_label": spec.label, "source": name, "brush": out["brush"],
            "canvas": {"width": canvas.width, "height": canvas.height}, "objects": len(plan.objects), "drawn": result.drawn,
            "matches_plan": result.ok, "redrawn": result.redraws, "failed_objects": result.final.failed_ids,
            "events": result.events, "skipped_fills": fills, "ink": out["ink"], "fidelity": out["achieved"],
            "stats": stats, "capabilities": run.info.get("capabilities"), "notes": notes, "screenshot": out["image"],
            "colour_error": out["colour_error"], "run_log": str(report_path),
            "specification": specification_summary(out["gate"]), "counters": run.counters, "honesty": spec.honesty,
        }
        if not result.ok:
            run.state = State.CORRECTING
            raise ToolError(ErrorCode.VERIFICATION_FAILED,
                            "the canvas does not match the plan" + (f" (ink coverage {out['ink']['coverage']:.0%}, stray "
                            f"{out['ink']['stray']:.0%})" if out["ink"] else f": {result.final.summary()}"),
                            retryable=False, details={"data": data, "screenshot": out["image"]})
        run.go(State.COMPLETED)
        data["state"] = run.state.value
        run.write(shots, f"run-{stamp}")
        like = ""
        if out["achieved"] is not None:
            like = (f" Likeness to the reference: {_quality(out['achieved']['ssim'])} "
                    f"(structural similarity {out['achieved']['ssim']}).")
        ink = (f" Ink on canvas matches the plan {out['ink']['coverage']:.0%}, unplanned ink {out['ink']['stray']:.0%}."
               if out["ink"] else "")
        return ToolOutcome(
            data=data, checks=[Check("canvas_matches_plan", True, f"{len(plan.objects)} objects verified in Paint")],
            artifacts=[{"kind": "screenshot", "path": out["image"]}],
            observation=(f"Drew {name} in Paint as {spec.label} ({mode.value}): {len(plan.objects)} objects"
                         f"{', brush ' + out['brush'] if out['brush'] and brush_mode else ''}, canvas {canvas.width}x{canvas.height}; "
                         f"verified against the plan.{ink}{like} This is a simulation: {spec.honesty}."
                         f"{' ' + '; '.join(problems) + '.' if problems else ''}"
                         f"{' Notes: ' + '; '.join(notes) if notes else ''}"),
            backend=BACKEND)
