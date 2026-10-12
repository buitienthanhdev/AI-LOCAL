"""Artwork Specification: the structured description of a picture that must exist before Paint is touched.

Groups A-H (metadata, canvas, medium, composition, objects, colour and lighting, strokes and tools, execution and
validation) plus an `admin` block (schema version, status, evidence, validation errors, unresolved fields, assumption
log, provenance). Every value that matters carries a provenance: KNOWN (given or measured), INFERRED (derived, with a
source and a confidence) or UNKNOWN. Nothing here is filled with invented data: what cannot be determined is listed
in `admin.unresolved_fields`, and what does not apply to a picture is null with a reason in `admin.not_applicable`.

`build_spec` is deterministic: it derives the specification from the planned objects (canvas pixels), the art brief,
the capabilities of the running Paint and, when there is one, the reference picture.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from lga_worker.art.modes import SPECS, Mode
from lga_worker.paint.model import FILL_KIND, Plan, PlannedObject

SCHEMA_VERSION = "1.0"
NAMESPACE = uuid.UUID("5a1f0b0e-2f27-4c47-9f0c-6a1d1c0a77b1")
MAX_CONTOUR_POINTS = 400
SECONDS_PER = {"shape": 3.6, "line": 3.2, "curve": 0.5, "brush": 0.35, "fill": 1.6, "other": 1.0}

KNOWN, INFERRED, UNKNOWN, NOT_APPLICABLE = "KNOWN", "INFERRED", "UNKNOWN", "NOT_APPLICABLE"

MEDIUM_OF_MODE = {
    Mode.PENCIL_SKETCH: ("pencil", "paper", "translucent", ["hatching", "contour line", "tonal build-up"]),
    Mode.CHARCOAL_SKETCH: ("other", "paper", "translucent", ["broad strokes", "tonal masses"]),
    Mode.INK_LINE_ART: ("ink", "paper", "opaque", ["contour", "solid blacks", "hatching"]),
    Mode.BLACK_WHITE: ("ink", "paper", "opaque", ["solid shapes"]),
    Mode.GRAYSCALE: ("other", "paper", "opaque", ["flat tonal regions"]),
    Mode.FLAT_COLOR: ("other", "canvas", "opaque", ["flat colour regions", "outline"]),
    Mode.PAINTERLY: ("other", "canvas", "opaque", ["directional strokes", "coarse to fine layers"]),
    Mode.WATERCOLOR_SIMULATION: ("watercolor", "paper", "transparent", ["pale washes", "paper white", "wet edges"]),
    Mode.OIL_PAINT_SIMULATION: ("oil", "canvas", "opaque", ["overlapping directional strokes", "coarse to fine layers"]),
    Mode.ACRYLIC_SIMULATION: ("acrylic", "canvas", "opaque", ["colour blocks", "broad strokes"]),
    Mode.LACQUER_ART_SIMULATION: ("lacquer", "lacquer_panel", "opaque", ["dark ground", "gold and eggshell inlay shapes"]),
    Mode.TECHNICAL_ILLUSTRATION: ("ink", "paper", "opaque", ["uniform outline", "flat tint"]),
    Mode.PIXEL_ART: ("other", "canvas", "opaque", ["grid cells", "limited palette"]),
    Mode.HYBRID_RENDERING: ("other", "canvas", "opaque", ["flat regions", "ink outline"]),
}


def hex_of(rgb: tuple[int, int, int] | None) -> str | None:
    return None if rgb is None else "#%02X%02X%02X" % tuple(int(c) for c in rgb)


def _lum(rgb: tuple[int, int, int]) -> float:
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255.0


def _area(obj: PlannedObject) -> float:
    x0, y0, x1, y1 = obj.bounds
    return max(0.0, (x1 - x0) * (y1 - y0))


def _bbox(obj: PlannedObject) -> dict[str, float]:
    x0, y0, x1, y1 = obj.bounds
    return {"x": round(x0, 2), "y": round(y0, 2), "width": round(x1 - x0, 2), "height": round(y1 - y0, 2)}


def _stride(points: list[Any], limit: int) -> list[Any]:
    if len(points) <= limit:
        return points
    step = math.ceil(len(points) / limit)
    return points[::step]


def _tool_type(obj: PlannedObject) -> tuple[str, str]:
    """(tool_type, brush_id) as the schema names them."""
    if obj.kind == FILL_KIND:
        return "fill", "Fill with colour"
    if obj.kind in ("rect", "ellipse", "triangle", "polygon"):
        return "shape", {"rect": "Rectangle", "ellipse": "Oval", "triangle": "Triangle", "polygon": "Polygon"}[obj.kind]
    if obj.kind == "line" or obj.kind == "polyline":
        return "line", "Line"
    if obj.kind == "curve":
        return "brush", obj.brush or "Brush"
    if obj.kind == "path":
        brush = obj.brush or "Brush"
        return ("pencil" if brush == "Pencil" else "brush"), brush
    return "other", obj.kind


def _geometry_type(obj: PlannedObject) -> str:
    if obj.kind == "ellipse":
        return "ellipse"
    if obj.kind in ("rect", "triangle", "polygon"):
        return "polygon"
    if obj.kind in ("line", "polyline"):
        return "line"
    return "curve"


def object_id(plan_object_id: str, artwork_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(artwork_id), plan_object_id))


def build_spec(*, request: str, plan: Plan, draw_order: list[PlannedObject], stages: list[list[PlannedObject]],
               brief: dict[str, Any] | None = None, mode: Mode | None = None, source: dict[str, Any] | None = None,
               caps: dict[str, Any] | None = None, canvas_state: dict[str, Any] | None = None,
               reference: Image.Image | None = None, user_supplied_plan: bool = True,
               background: tuple[int, int, int] = (255, 255, 255)) -> dict[str, Any]:
    source = source or {}
    brief = brief or {}
    canvas = plan.canvas
    artwork_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prov: dict[str, dict[str, Any]] = {}
    assumptions: list[dict[str, Any]] = []
    not_applicable: dict[str, str] = {}
    unresolved: list[str] = []

    def P(path: str, status: str, src: str, conf: float = 1.0) -> None:
        prov[path] = {"status": status, "source": src, "confidence": round(conf, 2)}

    def assume(path: str, value: Any, why: str) -> None:
        assumptions.append({"field": path, "value": value, "reason": why})

    drawn = [o for o in draw_order if o.kind != FILL_KIND]
    fills = [o for o in draw_order if o.kind == FILL_KIND]
    mode = mode or (Mode(brief["mode"]) if brief.get("mode") else Mode.FLAT_COLOR)
    spec_mode = SPECS[mode]

    # ---- A: metadata ------------------------------------------------------------------------------------------
    work = brief.get("artwork") or None
    resolution = brief.get("resolution", "NOT_ARTWORK")
    ref_given = bool(source.get("reference_path"))
    if work and resolution == "IDENTIFIED":
        ref_type = "existing_artwork"
    elif ref_given or source.get("kind") == "image":
        ref_type = "photo_reference"
    else:
        ref_type = "original"
    intent = brief.get("intent")
    fidelity = {"REPRODUCTION": "faithful_reproduction", "STUDY": "faithful_reproduction", "ADAPTATION": "inspired",
                "INTERPRETATION": "inspired", "INSPIRED_BY": "inspired"}.get(intent or "", None)
    if fidelity is None:
        fidelity = "faithful_reproduction" if source.get("kind") == "image" else "free_interpretation"
        assume("metadata.fidelity_level", fidelity, "no intent stated: derived from the kind of source")
        P("metadata.fidelity_level", INFERRED, "source kind", 0.7)
    else:
        P("metadata.fidelity_level", KNOWN if brief.get("intent") else INFERRED, "art brief intent", 0.9)
    title = (work or {}).get("title") or source.get("name") or request[:80] or "Untitled"
    metadata = {
        "artwork_id": artwork_id, "title": title,
        "description": (request or "").strip()[:600] or f"{source.get('kind', 'drawing')} drawn in Paint",
        "artwork_type": (work or {}).get("genre") or ("copy of a picture" if ref_given else "illustration"),
        "art_style": (work or {}).get("movement") or brief.get("movement") or spec_mode.label,
        "reference_type": ref_type, "reference_source": source.get("reference_source") or source.get("reference_path")
        or source.get("name"), "fidelity_level": fidelity,
    }
    P("metadata.title", KNOWN if work or source.get("name") else INFERRED, "brief / source", 0.9)
    P("metadata.reference_type", KNOWN if work or ref_given else INFERRED, "brief / source", 0.9)
    if brief.get("verification"):
        prov["metadata.reference_source"] = {"status": INFERRED if brief["verification"] in ("BUILTIN_KNOWLEDGE", "UNVERIFIED")
                                             else KNOWN, "source": f"verification {brief['verification']}", "confidence": 0.6
                                             if brief["verification"] in ("BUILTIN_KNOWLEDGE", "UNVERIFIED") else 0.9}
    if ref_type == "original" and not metadata["reference_source"]:
        not_applicable["metadata.reference_source"] = "an original picture has no reference"

    # ---- B: canvas --------------------------------------------------------------------------------------------
    margin = plan.margin_px
    cs = canvas_state or {}
    canvas_group = {
        "canvas_width": canvas.width, "canvas_height": canvas.height, "aspect_ratio": round(canvas.width / canvas.height, 4),
        "background_color": hex_of(background), "background_type": "solid", "resolution": None,
        "coordinate_system": "origin_top_left_pixel_coordinates",
        "safe_margin": {"left": margin, "top": margin, "right": margin, "bottom": margin, "unit": "px"},
        "layer_plan": [{"layer": layer, "objects": [o.id for o in drawn if o.layer == layer]} for layer in plan.layers()],
        "screen_rect": cs.get("screen_rect"), "scale": cs.get("scale"), "dpi_scaling": cs.get("dpi_scaling"),
        "zoom_percent": round((cs.get("scale") or 1.0) * 100, 1),
    }
    P("canvas.canvas_width", KNOWN if cs else INFERRED, "Paint status bar" if cs else "plan canvas", 1.0 if cs else 0.7)
    not_applicable["canvas.resolution"] = "Paint canvases have pixel size only, no DPI"

    # ---- C: medium and technique ------------------------------------------------------------------------------
    medium_name, surface, opacity, technique = MEDIUM_OF_MODE[mode]
    original_medium = brief.get("medium")
    medium = {
        "medium": medium_name, "surface_type": surface, "technique": list(technique),
        "brushwork_style": ("directional overlapping strokes" if mode in (Mode.OIL_PAINT_SIMULATION, Mode.PAINTERLY) else
                            "hatched strokes" if mode in (Mode.PENCIL_SKETCH, Mode.CHARCOAL_SKETCH) else
                            "flat regions with outlines"),
        "texture_profile": {"kind": "simulated", "source": "stroke overlap and spacing",
                            "grain": "none" if not spec_mode.strokes else "stroke direction"},
        "opacity_behavior": opacity,
        "drying_behavior": {"applicable": False, "reason": "raster simulation: no drying"},
        "material_constraints": {"simulated": True, "original_medium": original_medium, "rendering_mode": mode.value,
                                 "honesty": spec_mode.honesty, "physical_simulation": False},
    }
    P("medium.medium", KNOWN if brief.get("mode") else INFERRED, "rendering mode " + mode.value, 0.9)

    # ---- E: objects (before D / F: they derive from them) ---------------------------------------------------
    total = float(canvas.width * canvas.height)
    objects: list[dict[str, Any]] = []
    ids: dict[str, str] = {}
    for rank, o in enumerate(drawn):
        ids[o.id] = object_id(o.id, artwork_id)
    for rank, o in enumerate(drawn):
        pts = [(round(x, 2), round(y, 2)) for x, y in _stride(list(o.outline), MAX_CONTOUR_POINTS)]
        cover = []
        x0, y0, x1, y1 = o.bounds
        for later in drawn[rank + 1:]:
            if later.fill is None or not later.closed:
                continue
            a0, b0, a1, b1 = later.bounds
            if min(x1, a1) - max(x0, a0) > 0 and min(y1, b1) - max(y0, b0) > 0:
                cover.append(ids[later.id])
        regions = []
        if o.fill is not None:
            regions.append({"role": "fill", "color": hex_of(o.fill), "geometry": "contour"})
        if o.stroke is not None:
            regions.append({"role": "outline" if o.closed else "stroke", "color": hex_of(o.stroke), "geometry": "contour"})
        objects.append({
            "object_id": ids[o.id], "plan_id": o.id, "object_type": o.kind, "semantic_label": o.id,
            "bounding_box": _bbox(o), "contour_points": [list(p) for p in pts], "geometry_type": _geometry_type(o),
            "transform": {"translate": [0, 0], "rotate_deg": 0, "scale": 1.0}, "depth_order": rank, "layer": o.layer,
            "color_regions": regions, "detail_level": round(min(1.0, len(o.outline) / 40.0), 2), "occlusion": cover,
            "confidence": 1.0 if user_supplied_plan else 0.75, "children": [],
        })
    P("objects", KNOWN if user_supplied_plan else INFERRED, "planned objects" if user_supplied_plan else "traced from a picture",
      1.0 if user_supplied_plan else 0.75)

    # ---- F: colour and lighting -------------------------------------------------------------------------------
    weights: dict[tuple[int, int, int], float] = {}
    for o in drawn:
        for c, w in ((o.fill, _area(o)), (o.stroke, 1.0 + o.stroke_px * (sum(math.dist(a, b) for a, b in zip(o.outline, o.outline[1:])) or 1))):
            if c is not None:
                weights[c] = weights.get(c, 0.0) + w
    ranked = sorted(weights, key=lambda c: -weights[c])
    primary = ranked[0] if ranked else background
    lums = [_lum(c) for c in ranked] or [_lum(background)]
    sats = [(max(c) - min(c)) / 255.0 for c in ranked] or [0.0]
    warmth = sum((c[0] - c[2]) * weights[c] for c in ranked) / (sum(weights.values()) or 1.0)
    light = {"status": UNKNOWN}
    if reference is not None:
        g = reference.convert("L").resize((64, 48))
        px = list(g.tobytes())
        w_, h_ = g.size
        tot = sum(px) or 1
        cx = sum((i % w_) * v for i, v in enumerate(px)) / tot / w_
        cy = sum((i // w_) * v for i, v in enumerate(px)) / tot / h_
        light = {"status": INFERRED, "source": "luminance centroid of the reference", "confidence": 0.5,
                 "position_uv": [round(cx, 3), round(cy, 3)]}
        direction = {"x": round(cx - 0.5, 3), "y": round(cy - 0.5, 3)}
    else:
        direction = None
        if spec_mode.colour in ("none", "grey") or mode in (Mode.FLAT_COLOR, Mode.PIXEL_ART, Mode.HYBRID_RENDERING):
            not_applicable["color_lighting.light_source"] = "flat / graphic rendering without modelled light"
            not_applicable["color_lighting.light_direction"] = "flat / graphic rendering without modelled light"
        else:
            unresolved.append("color_lighting.light_source")
            unresolved.append("color_lighting.light_direction")
    dark = [o for o in drawn if o.fill is not None and _lum(o.fill) < 0.25 and _area(o) < 0.5 * total]
    bright = [o for o in drawn if o.fill is not None and _lum(o.fill) > 0.8 and _area(o) < 0.5 * total]
    color_lighting = {
        "color_palette": [{"hex": hex_of(c), "role": "primary" if c == primary else "secondary", "share": round(weights[c] / sum(weights.values()), 3)}
                          for c in ranked[:32]],
        "primary_color": hex_of(primary), "secondary_colors": [hex_of(c) for c in ranked[1:6]],
        "color_temperature": "warm" if warmth > 25 else "cool" if warmth < -25 else "neutral",
        "value_range": {"min": round(min(lums), 3), "max": round(max(lums), 3)},
        "saturation_profile": {"mean": round(sum(sats) / len(sats), 3), "max": round(max(sats), 3)},
        "light_source": light if light["status"] != UNKNOWN or "color_lighting.light_source" in unresolved else None,
        "light_direction": direction,
        "shadow_regions": [{"object": ids[o.id], "color": hex_of(o.fill)} for o in dark[:12]],
        "highlight_regions": [{"object": ids[o.id], "color": hex_of(o.fill)} for o in bright[:12]],
        "ambient_occlusion": {"applicable": False, "reason": "not modelled in Paint"},
    }
    P("color_lighting.color_palette", KNOWN, "planned colours", 1.0)

    # ---- D: composition ---------------------------------------------------------------------------------------
    bg_like = [o for o in drawn if _area(o) > 0.6 * total]
    subjects = [o for o in drawn if o not in bg_like and _area(o) > 0]
    top = sorted(subjects, key=lambda o: -_area(o))[:6] or drawn[:1]
    wsum = sum(_area(o) for o in top) or 1.0
    fx = sum((o.bounds[0] + o.bounds[2]) / 2 * _area(o) for o in top) / wsum
    fy = sum((o.bounds[1] + o.bounds[3]) / 2 * _area(o) for o in top) / wsum
    fu, fv = fx / canvas.width, fy / canvas.height
    thirds = min(math.dist((fu, fv), (a, b)) for a in (1 / 3, 2 / 3) for b in (1 / 3, 2 / 3))
    ctype = "centered" if math.dist((fu, fv), (0.5, 0.5)) < 0.1 else "rule_of_thirds" if thirds < 0.12 else "asymmetric"
    cells = 6
    empty: list[dict[str, float]] = []
    for gy in range(4):
        for gx in range(cells):
            cx0, cy0 = gx * canvas.width / cells, gy * canvas.height / 4
            cx1, cy1 = cx0 + canvas.width / cells, cy0 + canvas.height / 4
            if not any(min(cx1, o.bounds[2]) - max(cx0, o.bounds[0]) > 2 and min(cy1, o.bounds[3]) - max(cy0, o.bounds[1]) > 2
                       for o in subjects):
                empty.append({"x": round(cx0), "y": round(cy0), "width": round(cx1 - cx0), "height": round(cy1 - cy0)})
    composition = {
        "composition_type": ctype,
        "focal_point": {"u": round(fu, 3), "v": round(fv, 3), "x": round(fx, 1), "y": round(fy, 1)},
        "horizon_line": None, "vanishing_points": [],
        "subject_placement": [{"object": ids[o.id], "u": round((o.bounds[0] + o.bounds[2]) / 2 / canvas.width, 3),
                               "v": round((o.bounds[1] + o.bounds[3]) / 2 / canvas.height, 3),
                               "relative_area": round(_area(o) / total, 4)} for o in top],
        "negative_space": empty[:10],
        "depth_layers": [{"layer": layer, "objects": [ids[o.id] for o in drawn if o.layer == layer]} for layer in plan.layers()],
        "visual_hierarchy": [ids[o.id] for o in sorted(subjects, key=lambda o: -_area(o) * (1 + abs(_lum(o.fill or (128, 128, 128)) - 0.5)))[:10]],
        "balance": {"centroid_u": round(fu, 3), "centroid_v": round(fv, 3),
                    "offset_from_center": round(math.dist((fu, fv), (0.5, 0.5)), 3)},
    }
    not_applicable["composition.horizon_line"] = "no perspective construction was specified or detected"
    not_applicable["composition.vanishing_points"] = "no perspective construction was specified or detected"
    P("composition.focal_point", INFERRED, "area-weighted centroid of the main objects", 0.6)

    # ---- G: strokes and tools ----------------------------------------------------------------------------------
    stage_of: dict[str, int] = {}
    for n, batch in enumerate(stages):
        for o in batch:
            stage_of[o.id] = n
    plan_ids = {o.id: o for o in plan.objects}
    strokes: list[dict[str, Any]] = []
    for order, o in enumerate(draw_order):
        tool, brush = _tool_type(o)
        if o.kind == FILL_KIND:
            region = o.region
            strokes.append({
                "stroke_id": str(uuid.uuid5(uuid.UUID(artwork_id), "stroke:" + o.id)), "plan_id": o.id, "tool_type": "fill",
                "brush_id": brush, "brush_size": None, "stroke_color": None, "stroke_opacity": 1.0, "stroke_points": [],
                "stroke_pressure": None, "stroke_spacing": None, "fill_color": hex_of(o.fill), "fill_mode": "flood_fill",
                "anti_aliasing": False, "blend_mode": "normal", "execution_order": order,
                "undo_checkpoint": f"stage-{stage_of.get(o.id, len(stages))}",
                "seed": [round(o.points[0][0], 1), round(o.points[0][1], 1)], "region_object": ids.get(region or "") if region else None,
                "preconditions": ["region outline drawn", "region closed"], "expected": {"region_color": hex_of(o.fill)},
            })
            continue
        pts = [[round(x, 1), round(y, 1)] for x, y in _stride(list(o.outline), MAX_CONTOUR_POINTS)]
        strokes.append({
            "stroke_id": str(uuid.uuid5(uuid.UUID(artwork_id), "stroke:" + o.id)), "plan_id": o.id, "tool_type": tool,
            "brush_id": brush, "brush_size": float(o.stroke_px), "stroke_color": hex_of(o.stroke), "stroke_opacity": 1.0,
            "stroke_points": pts, "stroke_pressure": None, "stroke_spacing": None, "fill_color": hex_of(o.fill),
            "fill_mode": "solid_fill",
            "anti_aliasing": True, "blend_mode": "normal", "execution_order": order,
            "undo_checkpoint": f"stage-{stage_of.get(o.id, 0)}", "object_id": ids[o.id],
            "preconditions": ["canvas_calibrated", "paint_window_focused", f"tool {brush} selected"],
            "expected": {"region": _bbox(o), "color": hex_of(o.fill or o.stroke)},
        })
    not_applicable["strokes.stroke_pressure"] = "a mouse has no pressure"
    not_applicable["strokes.stroke_spacing"] = "Paint brushes have no spacing control"

    # ---- H: execution and validation ----------------------------------------------------------------------------
    graph: dict[str, list[str]] = {}
    for rank, o in enumerate(draw_order):
        deps: list[str] = []
        x0, y0, x1, y1 = o.bounds
        if o.kind == FILL_KIND:
            sx, sy = o.points[0]
            for earlier in draw_order[:rank]:
                if earlier.kind != FILL_KIND and earlier.closed:
                    a0, b0, a1, b1 = earlier.bounds
                    if a0 <= sx <= a1 and b0 <= sy <= b1:
                        deps.append(earlier.id)
        else:
            for earlier in draw_order[:rank]:
                if earlier.kind == FILL_KIND:
                    continue
                a0, b0, a1, b1 = earlier.bounds
                if min(x1, a1) - max(x0, a0) > 0 and min(y1, b1) - max(y0, b0) > 0:
                    deps.append(earlier.id)
        graph[o.id] = deps
    seconds = sum(SECONDS_PER[_tool_type(o)[0] if _tool_type(o)[0] in SECONDS_PER else "other"] for o in draw_order)
    drawing_plan = []
    for n, batch in enumerate(stages):
        bb = [o.bounds for o in batch if o.kind != FILL_KIND] or [(0, 0, 0, 0)]
        drawing_plan.append({
            "stage": n, "stage_id": f"stage-{n}", "objects": [o.id for o in batch],
            "up_to_layer": max(o.layer for o in batch),
            "prerequisites": [f"stage-{n - 1}"] if n else ["canvas_calibrated"],
            "expected_region": {"x": round(min(b[0] for b in bb), 1), "y": round(min(b[1] for b in bb), 1),
                                "x2": round(max(b[2] for b in bb), 1), "y2": round(max(b[3] for b in bb), 1)},
            "validation_rules": ["canvas matches the plan for the objects drawn so far", "no unexplained ink"],
        })
    not_applicable["execution.checkpoint_image"] = "checkpoints are taken per stage at runtime (result-<id>.png); none exists before drawing"
    execution = {
        "drawing_plan": drawing_plan,
        "prerequisites": ["paint_open", "canvas_calibrated", "tools_available", "undo_available", "no_blocking_validation_errors"],
        "estimated_cost": {"actions": len(draw_order), "estimated_seconds": round(seconds, 1), "stages": len(stages)},
        "dependency_graph": graph,
        "checkpoint_image": None,
        "expected_region": {"x": 0, "y": 0, "width": canvas.width, "height": canvas.height},
        "validation_rules": [
            {"id": "plan_match", "kind": "pixels", "threshold": 0.9, "required": True},
            {"id": "no_spill", "kind": "unexplained_ink", "threshold": 0.01, "required": True},
            {"id": "palette", "kind": "mean_colour_error", "threshold": 0.12, "required": True},
        ],
        "actual_result": {},
        "error_tolerance": {"coordinate_px": 2, "color_per_channel": 48, "missing_objects_share": 0.0 if user_supplied_plan else 0.10},
        "retry_policy": {"max_repair_rounds": 2, "max_recoveries": 4, "redraw_only_failed": True, "never_clear_canvas": True},
        "completion_status": "pending",
    }

    return {
        "metadata": metadata, "canvas": canvas_group, "medium": medium, "composition": composition, "objects": objects,
        "color_lighting": color_lighting, "strokes": strokes, "execution": execution,
        "admin": {
            "schema_version": SCHEMA_VERSION, "specification_status": "DRAFT", "created_at": now, "updated_at": now,
            "evidence": [], "validation_errors": [], "unresolved_fields": unresolved, "assumption_log": assumptions,
            "provenance": prov, "not_applicable": not_applicable, "request": request, "mode": mode.value,
            "brief_verification": brief.get("verification"), "capabilities": caps or {},
        },
    }
