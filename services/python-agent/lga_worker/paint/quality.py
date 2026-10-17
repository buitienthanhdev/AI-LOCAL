"""Four separate verdicts on a finished drawing (docs/specs/NANG_CAP_LOGIC_VE_DORAEMON.txt, section 11).

`execution_conformance`  did the tool draw the geometry that was planned? (the old "verified" status: it compares the
                         canvas with a plan made by the same system, so it says nothing about likeness)
`character_fidelity`     do proportions and landmarks match the template / the reference? (needs a template scene)
`visual_quality`         are lines continuous, fills clean, is there ink the plan does not explain, stray dots?
`canvas_safety`          nothing cut off or outside the margin, no blocking dialog, the screenshot opens.

`DRAW_EXECUTION_PASS` and `VISUAL_QUALITY_PASS` are kept apart on purpose: the first never implies the second.
Nothing here asks a model; everything is measured on the screenshot.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image

from lga_worker.paint import verify
from lga_worker.paint.model import Plan

NON_WHITE = 235
STRAY_MAX_AREA = 80  # px: a dark speck smaller than this that the plan does not explain is a stray dot
STRAY_ALLOWED = 0
LANDMARK_ERROR_MAX = 0.02  # of the figure height
SILHOUETTE_SLACK = 0.03
MIN_STROKE_COVERAGE = verify.COVERAGE_OK
REVIEWED_NOTE = ("the local template has not been reviewed against the official artwork by the owner; "
                 "this is a comparison with the template, not with a picture of the character")


def _ink_mask(image: Image.Image):  # noqa: ANN202
    import numpy as np

    arr = np.asarray(image.convert("RGB")).astype(int)
    return arr.min(axis=2) < NON_WHITE, arr


def _bbox(mask) -> tuple[int, int, int, int] | None:  # noqa: ANN001
    import numpy as np

    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _colour_centroid(arr, window: tuple[int, int, int, int], target: tuple[int, int, int], tol: int):  # noqa: ANN001
    import numpy as np

    x0, y0, x1, y1 = (max(0, w) for w in window)
    sub = arr[y0:y1 + 1, x0:x1 + 1]
    if sub.size == 0:
        return None
    near = (np.abs(sub - np.array(target)) <= tol).all(axis=2)
    ys, xs = np.where(near)
    if len(xs) < 6:
        return None
    return float(xs.mean() + x0), float(ys.mean() + y0), int(len(xs))


def execution_conformance(result: Any) -> dict[str, Any]:
    final = result.final
    return {"ok": bool(result.ok), "matches_plan": bool(final.ok), "objects_drawn": result.drawn,
            "failed_objects": list(final.failed_ids), "spill_ratio": round(final.spill_ratio, 4),
            "redrawn": dict(result.redraws), "stages": [{"up_to_layer": s.up_to_layer, "ok": s.ok} for s in result.stages],
            "note": "executed what was planned; says nothing about likeness"}


def canvas_safety(plan: Plan, image: Image.Image, *, window_ok: bool | None, screenshot: str | None) -> dict[str, Any]:
    mask, _ = _ink_mask(image)
    box = _bbox(mask)
    m = plan.margin_px
    w, h = image.size
    problems: list[str] = []
    if box is None:
        problems.append("the canvas is blank")
    else:
        x0, y0, x1, y1 = box
        if x0 < 1 or y0 < 1 or x1 > w - 2 or y1 > h - 2:
            problems.append("ink touches the edge of the canvas (something is cut off)")
        elif x0 < m - 2 or y0 < m - 2 or x1 > w - m + 1 or y1 > h - m + 1:
            problems.append(f"ink is inside the {m}px safe margin")
    opens = False
    if screenshot:
        try:
            with Image.open(screenshot) as probe:
                probe.verify()
            opens = True
        except (OSError, ValueError):
            problems.append("the screenshot file does not open")
    else:
        problems.append("no screenshot was saved")
    if window_ok is False:
        problems.append("Paint reported a dialog or lost focus")
    return {"ok": not problems, "ink_bbox": box, "margin_px": m, "no_blocking_dialog": window_ok,
            "screenshot_opens": opens, "problems": problems}


def _stray_specks(plan: Plan, image: Image.Image) -> dict[str, Any]:
    """Small dark components the plan does not explain (a stray dot from a mouse that stopped)."""
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover - the worker always has them
        return {"status": "NOT_RUN", "count": None}
    from lga_worker.paint import raster

    expected = raster.render_plan(plan)
    exp_ink, _ = _ink_mask(expected)
    act_ink, arr = _ink_mask(image)
    dark = arr.max(axis=2) < 110
    near_expected = cv2.dilate(exp_ink.astype("uint8"), np.ones((9, 9), np.uint8)) > 0
    stray = (dark & act_ink & ~near_expected).astype("uint8")
    count, _, stats, _ = cv2.connectedComponentsWithStats(stray, connectivity=8)
    specks = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, count) if 0 < stats[i, cv2.CC_STAT_AREA] <= STRAY_MAX_AREA]
    return {"status": "RUN", "count": len(specks), "areas": specks[:10]}


def visual_quality(plan: Plan, image: Image.Image, result: Any) -> dict[str, Any]:
    final = result.final
    coverages = [o.stroke_coverage for o in final.objects if o.stroke_coverage is not None]
    fills = [o.fill_match for o in final.objects if o.fill_match is not None]
    worst_cov = min(coverages) if coverages else None
    worst_fill = min(fills) if fills else None
    specks = _stray_specks(plan, image)
    problems: list[str] = []
    if final.spill_ratio > verify.SPILL_OK:
        problems.append(f"{final.spill_ratio:.1%} of the ink is not explained by the plan (spill or overdraw)")
    if worst_cov is not None and worst_cov < MIN_STROKE_COVERAGE:
        gaps = [o.id for o in final.objects if o.stroke_coverage is not None and o.stroke_coverage < MIN_STROKE_COVERAGE]
        problems.append(f"broken or missing outline on: {', '.join(gaps[:6])}")
    if worst_fill is not None and worst_fill < verify.FILL_SAMPLE_OK:
        bad = [o.id for o in final.objects if o.fill_match is not None and o.fill_match < verify.FILL_SAMPLE_OK]
        problems.append(f"wrong or missing fill colour on: {', '.join(bad[:6])}")
    if specks.get("count"):
        problems.append(f"{specks['count']} stray dot(s) the plan does not explain")
    _, arr = _ink_mask(image)
    palette = {tuple(c) for c in _plan_colours(plan)}
    edge = _intermediate_share(arr, palette)
    return {"ok": not problems, "worst_stroke_coverage": None if worst_cov is None else round(worst_cov, 3),
            "worst_fill_match": None if worst_fill is None else round(worst_fill, 3),
            "spill_ratio": round(final.spill_ratio, 4), "stray_specks": specks,
            "anti_aliasing_observed": edge > 0.002, "intermediate_colour_share": round(edge, 4), "problems": problems}


def _plan_colours(plan: Plan) -> set[tuple[int, int, int]]:
    out: set[tuple[int, int, int]] = {(255, 255, 255)}
    for o in plan.objects:
        for c in (o.fill, o.stroke):
            if c is not None:
                out.add(tuple(c))
    return out


def _intermediate_share(arr, palette: set[tuple[int, int, int]]) -> float:  # noqa: ANN001
    """Share of pixels whose colour is none of the planned colours (anti-aliased edge pixels are such pixels)."""
    import numpy as np

    flat = arr.reshape(-1, 3)
    exact = np.zeros(len(flat), dtype=bool)
    for c in palette:
        exact |= (np.abs(flat - np.array(c)) <= 6).all(axis=1)
    return float((~exact).mean())


def character_fidelity(plan: Plan, image: Image.Image, info: dict[str, Any] | None) -> dict[str, Any]:
    """Proportions and landmarks of the DRAWN canvas against the template (and the reference when one was measured)."""
    if not info:
        return {"status": "NOT_APPLICABLE", "ok": None, "reason": "not a named character: nothing to compare with",
                "limitations": ["no reference picture and no approved template: fidelity is not scored"]}
    reference = info.get("reference") or {}
    status = reference.get("status", "OFFLINE_TEMPLATE")
    mask, arr = _ink_mask(image)
    box = _bbox(mask)
    constraints = info.get("constraints") or []
    failed_geometry = [c["name"] for c in constraints if not c.get("ok")]
    checks: list[dict[str, Any]] = []
    if box is None:
        return {"status": "FAIL", "ok": False, "reference_status": status, "problems": ["the canvas is blank"],
                "geometry_constraints_failed": failed_geometry}
    x0, y0, x1, y1 = box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    aspect = w / h
    lo, hi = info.get("silhouette_aspect", (0.72, 0.86))
    checks.append({"name": "silhouette_aspect", "value": round(aspect, 3), "expected": [lo, hi],
                   "ok": lo - SILHOUETTE_SLACK <= aspect <= hi + SILHOUETTE_SLACK})
    flo, fhi = info.get("height_fraction", (0.0, 1.0))
    frac = h / plan.canvas.height
    if info.get("limited_by", "height") == "height":
        checks.append({"name": "figure_height_fraction", "value": round(frac, 3), "expected": [flo, fhi],
                       "ok": flo - 0.02 <= frac <= fhi + 0.02})
    centre = ((x0 + x1) / 2 / plan.canvas.width - 0.5, (y0 + y1) / 2 / plan.canvas.height - 0.5)
    checks.append({"name": "centred", "value": [round(centre[0], 3), round(centre[1], 3)], "expected": "|offset| <= 0.03",
                   "ok": abs(centre[0]) <= 0.03 and abs(centre[1]) <= 0.03})
    errors: dict[str, float | None] = {}
    for name, spec in (info.get("landmark_colours") or {}).items():
        expected = info["landmarks_px"].get(name)
        if expected is None:
            continue
        r = max(8, int(spec.get("window", 14)))
        found = _colour_centroid(arr, (int(expected[0] - r), int(expected[1] - r), int(expected[0] + r), int(expected[1] + r)),
                                 tuple(spec["rgb"]), int(spec.get("tolerance", 40)))
        errors[name] = None if found is None else round(math.dist(found[:2], expected) / max(h, 1), 4)
    worst = [k for k, v in errors.items() if v is None or v > LANDMARK_ERROR_MAX]
    checks.append({"name": "landmark_error", "value": errors, "expected": f"<= {LANDMARK_ERROR_MAX} of the figure height",
                   "ok": not worst, "note": f"off or not found: {worst}" if worst else ""})
    failed = [c["name"] for c in checks if not c["ok"]]
    problems = [f"{c['name']}: {c['value']} (expected {c['expected']})" for c in checks if not c["ok"]]
    problems += [f"template constraint failed before drawing: {n}" for n in failed_geometry]
    limitations = []
    if status != "AVAILABLE":
        limitations.append("reference_status OFFLINE_TEMPLATE: no reference picture was analysed")
    if not info.get("reviewed"):
        limitations.append(REVIEWED_NOTE)
    ok = not failed and not failed_geometry
    return {"status": "PASS" if ok else "FAIL", "ok": ok, "reference_status": status,
            "template_version": info.get("template_version"), "checks": checks, "landmark_errors": errors,
            "geometry_constraints_failed": failed_geometry, "problems": problems, "limitations": limitations,
            "measured_from": "screenshot"}


def assess(plan: Plan, image: Image.Image, result: Any, *, info: dict[str, Any] | None = None,
           window_ok: bool | None = None, screenshot: str | None = None) -> dict[str, Any]:
    """The four verdicts and the two flags. `info` is `scene_info(...)` of a template drawing (None for other pictures)."""
    execution = execution_conformance(result)
    safety = canvas_safety(plan, image, window_ok=window_ok, screenshot=screenshot)
    visual = visual_quality(plan, image, result)
    fidelity = character_fidelity(plan, image, info)
    draw_pass = bool(execution["ok"] and safety["ok"])
    visual_pass = bool(visual["ok"] and fidelity.get("ok") is not False and draw_pass)
    notes = []
    if fidelity["status"] == "NOT_APPLICABLE":
        notes.append("no character template: visual quality is measured, likeness is not scored")
    if fidelity.get("limitations"):
        notes.extend(fidelity["limitations"])
    return {"DRAW_EXECUTION_PASS": draw_pass, "VISUAL_QUALITY_PASS": visual_pass,
            "execution_conformance": execution, "character_fidelity": fidelity, "visual_quality": visual,
            "canvas_safety": safety, "notes": notes}


def summary_line(q: dict[str, Any]) -> str:
    f = q["character_fidelity"]["status"]
    return (f"DRAW_EXECUTION_PASS={q['DRAW_EXECUTION_PASS']}, VISUAL_QUALITY_PASS={q['VISUAL_QUALITY_PASS']} "
            f"(visual {'ok' if q['visual_quality']['ok'] else 'FAIL'}, fidelity {f}, safety {'ok' if q['canvas_safety']['ok'] else 'FAIL'})")


def write_report(directory: Path, stem: str, report: dict[str, Any]) -> str:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"quality-{stem}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return str(path)
