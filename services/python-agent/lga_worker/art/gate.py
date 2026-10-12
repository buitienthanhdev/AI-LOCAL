"""Validation, state machine and the execution gate of an artwork.

    IDLE -> ANALYZING_REQUEST -> ANALYZING_REFERENCE -> BUILDING_SPECIFICATION -> VALIDATING_SPECIFICATION
         -> BUILDING_DRAWING_PLAN -> CHECKING_READINESS -> READY_TO_DRAW -> DRAWING -> VALIDATING_RESULT -> COMPLETED
    side states: AWAITING_USER_INPUT, RECOVERING, FAILED

`run_gated` is the only way the Paint tools execute a plan. The gate it creates is handed to the driver, and
`PaintDriver.draw` refuses to run unless that gate is in DRAWING: a model (or another code path) that calls a drawing
tool cannot skip the specification, because the check lives in the thing that moves the mouse, not in a prompt.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from lga_worker.art import spec as spec_mod
from lga_worker.art.modes import SPECS, Mode
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import engine, raster
from lga_worker.paint.model import FILL_KIND, Plan

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_ACTIONS = 3000
MAX_SECONDS = 3600.0
COORD_SLACK = 1.0
SCALE_SLACK = 0.005
MAX_FILL_FRACTION = engine.MAX_FILL_FRACTION


class Level(str, Enum):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


@dataclass
class Issue:
    level: Level
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level.value, "code": self.code, "path": self.path, "message": self.message}


# --------------------------------------------------------------------------------------------- validation

ENUMS = {
    "metadata.reference_type": {"original", "existing_artwork", "photo_reference"},
    "metadata.fidelity_level": {"free_interpretation", "inspired", "faithful_reproduction"},
    "canvas.background_type": {"solid", "gradient", "textured"},
    "canvas.coordinate_system": {"origin_top_left_pixel_coordinates"},
    "medium.medium": {"oil", "acrylic", "watercolor", "lacquer", "pencil", "ink", "other"},
    "medium.surface_type": {"canvas", "paper", "wood", "lacquer_panel", "other"},
    "medium.opacity_behavior": {"transparent", "translucent", "opaque"},
    "color_lighting.color_temperature": {"warm", "neutral", "cool"},
    "execution.completion_status": {"pending", "running", "verified", "failed"},
}
GROUP_FIELDS: dict[str, dict[str, tuple[type | tuple[type, ...], bool]]] = {
    # field -> (type, may be null when listed in admin.not_applicable)
    "metadata": {"artwork_id": (str, False), "title": (str, False), "description": (str, False), "artwork_type": (str, False),
                 "art_style": (str, False), "reference_type": (str, False), "reference_source": (str, True),
                 "fidelity_level": (str, False)},
    "canvas": {"canvas_width": (int, False), "canvas_height": (int, False), "aspect_ratio": ((int, float), False),
               "background_color": (str, False), "background_type": (str, False), "resolution": ((int, float), True),
               "coordinate_system": (str, False), "safe_margin": (dict, False), "layer_plan": (list, False)},
    "medium": {"medium": (str, False), "surface_type": (str, False), "technique": (list, False), "brushwork_style": (str, False),
               "texture_profile": (dict, False), "opacity_behavior": (str, False), "drying_behavior": (dict, False),
               "material_constraints": (dict, False)},
    "composition": {"composition_type": (str, False), "focal_point": (dict, False), "horizon_line": ((int, float), True),
                    "vanishing_points": (list, False), "subject_placement": (list, False), "negative_space": (list, False),
                    "depth_layers": (list, False), "visual_hierarchy": (list, False), "balance": (dict, False)},
    "color_lighting": {"color_palette": (list, False), "primary_color": (str, False), "secondary_colors": (list, False),
                       "color_temperature": (str, False), "value_range": (dict, False), "saturation_profile": (dict, False),
                       "light_source": (dict, True), "light_direction": (dict, True), "shadow_regions": (list, False),
                       "highlight_regions": (list, False), "ambient_occlusion": (dict, False)},
    "execution": {"drawing_plan": (list, False), "prerequisites": (list, False), "estimated_cost": (dict, False),
                  "dependency_graph": (dict, False), "checkpoint_image": (str, True), "expected_region": (dict, False),
                  "validation_rules": (list, False), "actual_result": (dict, False), "error_tolerance": (dict, False),
                  "retry_policy": (dict, False), "completion_status": (str, False)},
}
OBJECT_FIELDS = {"object_id": str, "object_type": str, "semantic_label": str, "bounding_box": dict, "contour_points": list,
                 "geometry_type": str, "transform": dict, "depth_order": int, "color_regions": list, "detail_level": (int, float),
                 "occlusion": list, "confidence": (int, float)}
STROKE_FIELDS = {"tool_type": str, "brush_id": str, "stroke_color": (str, type(None)), "stroke_opacity": (int, float),
                 "stroke_points": list, "fill_color": (str, type(None)), "fill_mode": str, "anti_aliasing": bool,
                 "blend_mode": str, "execution_order": int, "undo_checkpoint": str}
GEOMETRY_TYPES = {"line", "polygon", "curve", "ellipse", "composite"}
TOOL_TYPES = {"pencil", "brush", "line", "curve", "shape", "fill", "eraser", "other"}
FILL_MODES = {"flood_fill", "solid_fill", "pattern"}
SHAPE_TOOL_NAMES = {"Rectangle", "Oval", "Triangle", "Polygon", "Line"}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def _walk_hex(node: Any, path: str, out: list[tuple[str, Any]]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("hex", "background_color", "primary_color", "stroke_color", "fill_color", "color") and isinstance(v, str):
                out.append((f"{path}.{k}", v))
            _walk_hex(v, f"{path}.{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node[:2000]):
            if isinstance(v, str) and path.endswith(("secondary_colors",)):
                out.append((f"{path}[{i}]", v))
            _walk_hex(v, f"{path}[{i}]", out)


def _in_canvas(x: float, y: float, w: int, h: int) -> bool:
    return -COORD_SLACK <= x <= w + COORD_SLACK and -COORD_SLACK <= y <= h + COORD_SLACK


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            if state.get(dep, 0) == 1:
                return path[path.index(dep):] + [dep]
            if state.get(dep, 0) == 0 and dep in graph:
                found = visit(dep)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return None

    for n in list(graph):
        if state.get(n, 0) == 0:
            found = visit(n)
            if found:
                return found
    return None


def check_fills(plan: Plan, draw_order: list[Any]) -> list[Issue]:
    """A bucket fill must land in a closed region on the picture as it will be when the fill runs."""
    issues: list[Issue] = []
    done: list[Any] = []
    for o in draw_order:
        if o.kind != FILL_KIND:
            done.append(o)
            continue
        seed = (round(o.points[0][0]), round(o.points[0][1]))
        base = raster.render(plan.canvas, done)
        enclosed, area = raster.bucket_fill_region(base, seed)
        fraction = area / float(plan.canvas.width * plan.canvas.height)
        fallback = None
        if o.region:
            fallback = next((p for p in plan.objects if p.id == o.region and p.closed), None)
        if not enclosed or fraction > MAX_FILL_FRACTION:
            why = "the region reaches the edge of the page" if not enclosed else f"the region would cover {fraction:.0%} of the page"
            level = Level.WARNING if fallback is not None else Level.BLOCKING
            issues.append(Issue(level, "FILL_NOT_CLOSED", f"strokes[{o.id}]",
                                f"{why}: a bucket fill would leak"
                                + (f"; the closed object {fallback.id!r} will be shape-filled instead" if fallback else
                                   "; close the outline first or name the closed object in `region`")))
        else:
            done.append(o)
    return issues


@dataclass
class Validation:
    issues: list[Issue] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.level == Level.BLOCKING]

    @property
    def ok(self) -> bool:
        return not self.blocking and not self.questions


def validate_spec(spec: dict[str, Any], *, plan: Plan | None = None, draw_order: list[Any] | None = None,
                  caps: dict[str, Any] | None = None, max_actions: int = MAX_ACTIONS,
                  max_seconds: float = MAX_SECONDS) -> Validation:
    v = Validation()

    def add(level: Level, code: str, path: str, message: str) -> None:
        v.issues.append(Issue(level, code, path, message))

    admin = spec.get("admin") if isinstance(spec.get("admin"), dict) else None
    if admin is None:
        add(Level.BLOCKING, "SCHEMA", "admin", "the admin block is missing")
        admin = {}
    if admin.get("schema_version") != spec_mod.SCHEMA_VERSION:
        add(Level.BLOCKING, "SCHEMA_VERSION", "admin.schema_version", f"expected {spec_mod.SCHEMA_VERSION}")
    na = admin.get("not_applicable") or {}

    # structure: groups, fields, types, nulls only where declared not applicable
    for group, fields in GROUP_FIELDS.items():
        node = spec.get(group)
        if not isinstance(node, dict):
            add(Level.BLOCKING, "SCHEMA", group, f"group {group!r} is missing")
            continue
        for name, (typ, nullable) in fields.items():
            path = f"{group}.{name}"
            if name not in node:
                add(Level.BLOCKING, "MISSING_FIELD", path, "required field is missing")
                continue
            val = node[name]
            if val is None:
                if nullable and (path in na or path in (admin.get("unresolved_fields") or [])):
                    continue
                if not nullable or path not in na:
                    add(Level.BLOCKING, "NULL_FIELD", path, "null without a NOT_APPLICABLE reason in admin.not_applicable"
                        if nullable else "required field is null")
                continue
            if isinstance(val, bool) and typ in (int, (int, float)):
                add(Level.BLOCKING, "TYPE", path, "expected a number")
            elif not isinstance(val, typ):
                add(Level.BLOCKING, "TYPE", path, f"expected {typ}, got {type(val).__name__}")
            if path in ENUMS and val not in ENUMS[path]:
                add(Level.BLOCKING, "ENUM", path, f"{val!r} is not one of {sorted(ENUMS[path])}")
    if not isinstance(spec.get("objects"), list) or not spec["objects"]:
        add(Level.BLOCKING, "NO_OBJECTS", "objects", "the specification has no objects to draw")
    if not isinstance(spec.get("strokes"), list) or not spec["strokes"]:
        add(Level.BLOCKING, "NO_STROKES", "strokes", "the specification has no drawing operations")
    if any(i.code in ("SCHEMA", "MISSING_FIELD") for i in v.issues):
        return v  # nothing below can be trusted on a broken structure

    cv = spec["canvas"]
    w, h = cv["canvas_width"], cv["canvas_height"]
    if w <= 0 or h <= 0:
        add(Level.BLOCKING, "CANVAS_SIZE", "canvas", f"canvas size {w}x{h} is not positive")
        return v
    if abs(cv["aspect_ratio"] - w / h) > 0.01:
        add(Level.BLOCKING, "ASPECT", "canvas.aspect_ratio", "aspect ratio disagrees with the canvas size")

    # colours
    found: list[tuple[str, Any]] = []
    for group in ("canvas", "color_lighting", "objects", "strokes"):
        _walk_hex(spec[group], group, found)
    for path, val in found:
        if not isinstance(val, str) or not HEX.match(val):
            add(Level.BLOCKING, "HEX", path, f"{val!r} is not a #RRGGBB colour")

    # objects: geometry inside the canvas, boxes consistent with contours, occlusion consistent with depth
    by_id: dict[str, dict[str, Any]] = {}
    for i, o in enumerate(spec["objects"]):
        base = f"objects[{i}]"
        if not isinstance(o, dict):
            add(Level.BLOCKING, "SCHEMA", base, "object is not a mapping")
            continue
        for name, typ in OBJECT_FIELDS.items():
            if name not in o or not isinstance(o[name], typ) or (typ in (int, (int, float)) and isinstance(o[name], bool)):
                add(Level.BLOCKING, "OBJECT_FIELD", f"{base}.{name}", "missing or of the wrong type")
        if any(i.path.startswith(base) for i in v.issues):
            continue
        by_id[o["object_id"]] = o
        if o["geometry_type"] not in GEOMETRY_TYPES:
            add(Level.BLOCKING, "ENUM", f"{base}.geometry_type", f"{o['geometry_type']!r} is not one of {sorted(GEOMETRY_TYPES)}")
        b = o["bounding_box"]
        if not all(isinstance(b.get(k), (int, float)) for k in ("x", "y", "width", "height")):
            add(Level.BLOCKING, "BBOX", f"{base}.bounding_box", "x, y, width and height must be numbers")
            continue
        if b["width"] < 0 or b["height"] < 0:
            add(Level.BLOCKING, "BBOX_NEGATIVE", f"{base}.bounding_box", "negative width or height")
        elif b["x"] < -COORD_SLACK or b["y"] < -COORD_SLACK or b["x"] + b["width"] > w + COORD_SLACK or b["y"] + b["height"] > h + COORD_SLACK:
            add(Level.BLOCKING, "BBOX_OUTSIDE", f"{base}.bounding_box", f"box {b} is outside the {w}x{h} canvas")
        pts = o["contour_points"]
        if len(pts) < 1:
            add(Level.BLOCKING, "CONTOUR", f"{base}.contour_points", "no contour")
        bad = [p for p in pts if not (isinstance(p, (list, tuple)) and len(p) == 2 and _in_canvas(p[0], p[1], w, h))]
        if bad:
            add(Level.BLOCKING, "POINT_OUTSIDE", f"{base}.contour_points", f"{len(bad)} point(s) outside the canvas, e.g. {bad[0]}")
        elif pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            if (min(xs) < b["x"] - 2 or min(ys) < b["y"] - 2 or max(xs) > b["x"] + b["width"] + 2 or max(ys) > b["y"] + b["height"] + 2):
                add(Level.BLOCKING, "BBOX_CONTOUR", base, "the contour leaves its bounding box")
        for r in o["color_regions"]:
            if not isinstance(r, dict) or "color" not in r:
                add(Level.BLOCKING, "COLOR_REGION", f"{base}.color_regions", "a colour region has no colour")
    for i, o in enumerate(spec["objects"]):
        if not isinstance(o, dict) or o.get("object_id") not in by_id:
            continue
        for occ in o["occlusion"]:
            other = by_id.get(occ)
            if other is None:
                add(Level.BLOCKING, "OCCLUSION_UNKNOWN", f"objects[{i}].occlusion", f"unknown occluder {occ}")
            elif other["depth_order"] <= o["depth_order"]:
                add(Level.BLOCKING, "OCCLUSION_ORDER", f"objects[{i}].occlusion",
                    f"{other['semantic_label']!r} hides {o['semantic_label']!r} but is drawn before it")

    # palette against the rendering mode
    mode = admin.get("mode")
    spec_mode = SPECS.get(Mode(mode)) if mode in {m.value for m in Mode} else None
    colours = [_hex_to_rgb(c["hex"]) for c in spec["color_lighting"]["color_palette"] if isinstance(c, dict) and HEX.match(str(c.get("hex", "")))]
    if spec_mode is not None and colours:
        spread = max(max(c) - min(c) for c in colours)
        if spec_mode.colour in ("none", "grey") and spread > 12:
            add(Level.BLOCKING, "PALETTE_MODE", "color_lighting.color_palette",
                f"{mode} allows no colour, the palette has hue (channel spread {spread})")
        if spec_mode.colour == "none" and any(0.3 < (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255 < 0.7 for c in colours):
            add(Level.WARNING, "PALETTE_TONE", "color_lighting.color_palette", f"{mode} is black on white; mid tones will look grey")
    hl, sh = spec["color_lighting"]["highlight_regions"], spec["color_lighting"]["shadow_regions"]
    if hl and sh:
        lum = lambda r: (lambda c: (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]))(_hex_to_rgb(r["color"]))  # noqa: E731
        try:
            if min(lum(r) for r in hl) < max(lum(r) for r in sh) - 60:
                add(Level.WARNING, "LIGHT_CONSISTENCY", "color_lighting", "a highlight region is darker than a shadow region")
        except (KeyError, ValueError, TypeError):
            pass

    # strokes: tools exist in this Paint, operations are real, order and dependencies are sound
    tools = set((caps or {}).get("tools") or [])
    brushes = set((caps or {}).get("brushes") or [])
    seen_orders: set[int] = set()
    plan_to_order: dict[str, int] = {}
    for i, s in enumerate(spec["strokes"]):
        base = f"strokes[{i}]"
        if not isinstance(s, dict):
            add(Level.BLOCKING, "SCHEMA", base, "stroke is not a mapping")
            continue
        for name, typ in STROKE_FIELDS.items():
            if name not in s or not isinstance(s[name], typ):
                add(Level.BLOCKING, "STROKE_FIELD", f"{base}.{name}", "missing or of the wrong type")
        if any(x.path.startswith(base + ".") for x in v.issues):
            continue
        if s["tool_type"] not in TOOL_TYPES:
            add(Level.BLOCKING, "ENUM", f"{base}.tool_type", f"{s['tool_type']!r} is not one of {sorted(TOOL_TYPES)}")
        if s["fill_mode"] not in FILL_MODES:
            add(Level.BLOCKING, "ENUM", f"{base}.fill_mode", f"{s['fill_mode']!r} is not one of {sorted(FILL_MODES)}")
        if not 0.0 <= s["stroke_opacity"] <= 1.0:
            add(Level.BLOCKING, "OPACITY", f"{base}.stroke_opacity", "opacity must be between 0 and 1")
        if s["blend_mode"] != "normal":
            add(Level.WARNING, "BLEND_UNSUPPORTED", f"{base}.blend_mode", f"Paint has no {s['blend_mode']!r} blend: normal is used")
        if s.get("stroke_pressure"):
            add(Level.BLOCKING, "PRESSURE", f"{base}.stroke_pressure", "a mouse has no pressure: it must be null")
        if s["tool_type"] in ("brush", "pencil") and brushes and s["brush_id"] not in brushes | {"Pencil"}:
            add(Level.BLOCKING, "TOOL_UNAVAILABLE", f"{base}.brush_id", f"this Paint does not offer the brush {s['brush_id']!r}")
        if s["tool_type"] in ("shape", "line") and tools and s["brush_id"] not in tools:
            add(Level.BLOCKING, "TOOL_UNAVAILABLE", f"{base}.brush_id", f"this Paint has no {s['brush_id']!r} tool")
        if s["tool_type"] == "fill" and tools and "Fill" not in tools:
            add(Level.BLOCKING, "TOOL_UNAVAILABLE", f"{base}.tool_type", "this Paint has no Fill tool")
        if s["tool_type"] != "fill":
            bad = [p for p in s["stroke_points"] if not (isinstance(p, (list, tuple)) and len(p) == 2 and _in_canvas(p[0], p[1], w, h))]
            if bad:
                add(Level.BLOCKING, "POINT_OUTSIDE", f"{base}.stroke_points", f"{len(bad)} point(s) outside the canvas, e.g. {bad[0]}")
            if not s["stroke_points"]:
                add(Level.BLOCKING, "STROKE_EMPTY", f"{base}.stroke_points", "no points")
        elif "seed" in s and not _in_canvas(s["seed"][0], s["seed"][1], w, h):
            add(Level.BLOCKING, "POINT_OUTSIDE", f"{base}.seed", "the fill seed is outside the canvas")
        if s["execution_order"] in seen_orders:
            add(Level.BLOCKING, "ORDER_DUPLICATE", f"{base}.execution_order", "two operations share an execution order")
        seen_orders.add(s["execution_order"])
        plan_to_order[s.get("plan_id", f"s{i}")] = s["execution_order"]

    graph = spec["execution"]["dependency_graph"]
    for node, deps in graph.items():
        if not isinstance(deps, list):
            add(Level.BLOCKING, "GRAPH", f"execution.dependency_graph.{node}", "dependencies must be a list")
            continue
        for d in deps:
            if d not in graph:
                add(Level.BLOCKING, "GRAPH_MISSING", f"execution.dependency_graph.{node}", f"depends on {d!r}, which is not in the plan")
            elif node in plan_to_order and d in plan_to_order and plan_to_order[d] >= plan_to_order[node]:
                add(Level.BLOCKING, "GRAPH_ORDER", f"execution.dependency_graph.{node}", f"{d!r} must be drawn before {node!r}")
    cycle = find_cycle({k: [d for d in v_ if d in graph] for k, v_ in graph.items() if isinstance(v_, list)})
    if cycle:
        add(Level.BLOCKING, "GRAPH_CYCLE", "execution.dependency_graph", "cycle: " + " -> ".join(cycle))

    # budgets, claims, honesty
    cost = spec["execution"]["estimated_cost"]
    if cost.get("actions", 0) > max_actions:
        add(Level.BLOCKING, "BUDGET_ACTIONS", "execution.estimated_cost", f"{cost['actions']} operations exceed the limit of {max_actions}")
    if cost.get("estimated_seconds", 0) > max_seconds:
        add(Level.BLOCKING, "BUDGET_TIME", "execution.estimated_cost",
            f"about {cost['estimated_seconds']:.0f} s exceeds the limit of {max_seconds:.0f} s: use a lower detail")
    if spec["medium"]["material_constraints"].get("physical_simulation"):
        add(Level.BLOCKING, "PHYSICS_CLAIM", "medium.material_constraints", "Paint cannot simulate the physics of a medium")
    if spec["execution"]["completion_status"] == "verified":
        ev = admin.get("evidence") or []
        actual = spec["execution"]["actual_result"]
        if not ev or not actual.get("screenshot") or actual.get("ok") is not True:
            add(Level.BLOCKING, "VERIFIED_WITHOUT_EVIDENCE", "execution.completion_status",
                "marked verified without runtime evidence (screenshot and a passing measured result)")
    for path, p in (admin.get("provenance") or {}).items():
        if p.get("status") == "INFERRED" and not p.get("source"):
            add(Level.WARNING, "NO_SOURCE", f"admin.provenance.{path}", "an inferred value without a source")
    if plan is not None and draw_order is not None:
        v.issues.extend(check_fills(plan, draw_order))
    for f in admin.get("unresolved_fields") or []:
        if f not in na:
            add(Level.WARNING, "UNRESOLVED", f, "could not be determined; the specification continues without it")
    return v


# ------------------------------------------------------------------------------------------ state machine


class State(str, Enum):
    IDLE = "IDLE"
    ANALYZING_REQUEST = "ANALYZING_REQUEST"
    ANALYZING_REFERENCE = "ANALYZING_REFERENCE"
    BUILDING_SPECIFICATION = "BUILDING_SPECIFICATION"
    VALIDATING_SPECIFICATION = "VALIDATING_SPECIFICATION"
    AWAITING_USER_INPUT = "AWAITING_USER_INPUT"
    BUILDING_DRAWING_PLAN = "BUILDING_DRAWING_PLAN"
    CHECKING_READINESS = "CHECKING_READINESS"
    READY_TO_DRAW = "READY_TO_DRAW"
    DRAWING = "DRAWING"
    VALIDATING_RESULT = "VALIDATING_RESULT"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


S = State
TRANSITIONS: dict[State, set[State]] = {
    S.IDLE: {S.ANALYZING_REQUEST},
    S.ANALYZING_REQUEST: {S.ANALYZING_REFERENCE, S.AWAITING_USER_INPUT, S.FAILED},
    S.ANALYZING_REFERENCE: {S.BUILDING_SPECIFICATION, S.AWAITING_USER_INPUT, S.FAILED},
    S.BUILDING_SPECIFICATION: {S.VALIDATING_SPECIFICATION, S.FAILED},
    S.VALIDATING_SPECIFICATION: {S.BUILDING_DRAWING_PLAN, S.AWAITING_USER_INPUT, S.BUILDING_SPECIFICATION, S.FAILED},
    S.AWAITING_USER_INPUT: {S.ANALYZING_REQUEST, S.BUILDING_SPECIFICATION, S.FAILED},
    S.BUILDING_DRAWING_PLAN: {S.CHECKING_READINESS, S.FAILED},
    S.CHECKING_READINESS: {S.READY_TO_DRAW, S.BUILDING_SPECIFICATION, S.AWAITING_USER_INPUT, S.FAILED},
    S.READY_TO_DRAW: {S.DRAWING, S.CHECKING_READINESS, S.FAILED},
    S.DRAWING: {S.VALIDATING_RESULT, S.RECOVERING, S.FAILED},
    S.VALIDATING_RESULT: {S.COMPLETED, S.RECOVERING, S.FAILED},
    S.RECOVERING: {S.DRAWING, S.CHECKING_READINESS, S.FAILED},
    S.COMPLETED: set(),
    S.FAILED: set(),
}


class GateClosed(ToolError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(ErrorCode.POLICY_DENIED, message, retryable=False, details=details)


@dataclass
class SpecRun:
    state: State = State.IDLE
    history: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def go(self, new: State, **detail: Any) -> None:
        if new not in TRANSITIONS[self.state]:
            raise GateClosed(f"illegal transition {self.state.value} -> {new.value}", state=self.state.value, to=new.value)
        self.history.append({"from": self.state.value, "to": new.value, "t": round(time.monotonic() - self.started, 2), **detail})
        self.state = new


@dataclass
class ExecutionGate:
    """Opened (state DRAWING) only after a validated specification and a passed readiness check."""

    run: SpecRun = field(default_factory=SpecRun)
    spec: dict[str, Any] | None = None
    spec_path: str | None = None
    validated: bool = False
    ready: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def is_open(self) -> bool:
        return self.validated and self.ready and self.run.state == State.DRAWING

    def assert_open(self) -> None:
        if not self.is_open():
            raise GateClosed("drawing refused: no validated artwork specification is in force "
                             f"(state {self.run.state.value}); the specification gate must be passed first",
                             state=self.run.state.value)

    def begin(self, probe: dict[str, Any]) -> None:
        """READY_TO_DRAW -> DRAWING, after re-reading the canvas: it must still be the one that was planned."""
        if self.run.state != State.READY_TO_DRAW or not self.validated or not self.ready:
            raise GateClosed(f"cannot start drawing from state {self.run.state.value}", state=self.run.state.value)
        problems = [i for i in check_readiness(self.spec or {}, probe) if i.level == Level.BLOCKING]
        if problems:
            self.run.go(State.CHECKING_READINESS, reason="canvas changed since planning")
            self.ready = False
            self.run.go(State.FAILED, reason=problems[0].message)
            raise ToolError(ErrorCode.CONFLICT, "the canvas changed between planning and drawing: " + problems[0].message,
                            retryable=True, details={"state": "FAILED", "issues": [i.to_dict() for i in problems]})
        self.run.go(State.DRAWING)

    def recovered(self, note: dict[str, Any]) -> None:
        self.events.append(note)


def check_readiness(spec: dict[str, Any], probe: dict[str, Any]) -> list[Issue]:
    """Paint open and in front, the canvas the one that was specified, tools available, undo available."""
    issues: list[Issue] = []
    cv = spec.get("canvas", {})
    if not probe.get("window_ok"):
        issues.append(Issue(Level.BLOCKING, "NO_WINDOW", "paint", "the Paint window is not open"))
    size = probe.get("canvas")
    if size is None or (cv and (size[0], size[1]) != (cv.get("canvas_width"), cv.get("canvas_height"))):
        issues.append(Issue(Level.BLOCKING, "CANVAS_CHANGED", "canvas",
                            f"the canvas is {size}, the specification was made for "
                            f"{cv.get('canvas_width')}x{cv.get('canvas_height')}"))
    scale = probe.get("scale")
    if cv.get("scale") and scale and abs(scale - cv["scale"]) > SCALE_SLACK:
        issues.append(Issue(Level.BLOCKING, "ZOOM_CHANGED", "canvas.scale", f"zoom changed from {cv['scale']} to {scale}"))
    if scale is not None and scale <= 0:
        issues.append(Issue(Level.BLOCKING, "CALIBRATION", "canvas.scale", "the coordinate transform is not calibrated"))
    if probe.get("undo") is False:
        issues.append(Issue(Level.BLOCKING, "NO_UNDO", "paint", "no undo / checkpoint mechanism is available"))
    tools, brushes = probe.get("tools"), probe.get("brushes")
    for s in spec.get("strokes", []):
        if s.get("tool_type") in ("shape", "line") and tools and s.get("brush_id") not in tools:
            issues.append(Issue(Level.BLOCKING, "TOOL_UNAVAILABLE", s.get("plan_id", "?"), f"tool {s.get('brush_id')!r} is not available"))
            break
        if s.get("tool_type") in ("brush",) and brushes and s.get("brush_id") not in brushes:
            issues.append(Issue(Level.BLOCKING, "TOOL_UNAVAILABLE", s.get("plan_id", "?"), f"brush {s.get('brush_id')!r} is not available"))
            break
    return issues


# ----------------------------------------------------------------------------------------------- pipeline


def _write(directory: Path | None, name: str, payload: Any) -> str | None:
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return str(path)


def specification_summary(gate: ExecutionGate, issues: list[Issue] | None = None) -> dict[str, Any]:
    spec = gate.spec or {}
    admin = spec.get("admin", {})
    return {"artwork_id": spec.get("metadata", {}).get("artwork_id"), "path": gate.spec_path,
            "status": admin.get("specification_status"), "state": gate.run.state.value,
            "completion_status": spec.get("execution", {}).get("completion_status"),
            "warnings": [i.to_dict() for i in (issues or []) if i.level == Level.WARNING][:10],
            "assumptions": len(admin.get("assumption_log", [])), "unresolved": admin.get("unresolved_fields", []),
            "objects": len(spec.get("objects", [])), "operations": len(spec.get("strokes", [])),
            "estimated_seconds": spec.get("execution", {}).get("estimated_cost", {}).get("estimated_seconds")}


def run_gated(plan: Plan, driver: Any, *, request: str, directory: Path | None, brief: dict[str, Any] | None = None,
              mode: Mode | None = None, source: dict[str, Any] | None = None, reference: Any = None,
              user_supplied_plan: bool = True, should_stop: Callable[[], bool] = lambda: False,
              tolerance: float = 0.0, resume: bool = False) -> tuple[engine.DrawResult, ExecutionGate]:
    """Specification -> validation -> plan -> readiness -> drawing -> result validation. The only way to draw."""
    gate = ExecutionGate()
    run = gate.run
    run.go(State.ANALYZING_REQUEST, request=request[:120])
    if brief and brief.get("questions") and brief.get("resolution") == "AMBIGUOUS":
        run.go(State.AWAITING_USER_INPUT, questions=brief["questions"])
        raise ToolError(ErrorCode.CONFLICT, "the request is ambiguous: " + " ".join(brief["questions"]), retryable=False,
                        details={"state": "AWAITING_USER_INPUT", "questions": brief["questions"]})
    run.go(State.ANALYZING_REFERENCE, reference=bool(reference is not None or (source or {}).get("reference_path")))
    run.go(State.BUILDING_SPECIFICATION)
    ordered = engine.draw_order(plan.objects)
    stages = engine._stages(ordered)
    probe = driver.probe() if hasattr(driver, "probe") else {"window_ok": True, "canvas": (plan.canvas.width, plan.canvas.height),
                                                            "scale": getattr(driver, "scale", 1.0), "undo": True}
    caps = probe.get("capabilities") or {"tools": probe.get("tools"), "brushes": probe.get("brushes"), "version": probe.get("version")}
    spec = spec_mod.build_spec(
        request=request, plan=plan, draw_order=ordered, stages=stages, brief=brief, mode=mode, source=source, caps=caps,
        canvas_state={"scale": probe.get("scale"), "screen_rect": probe.get("screen_rect"), "dpi_scaling": probe.get("dpi_scaling")},
        reference=reference, user_supplied_plan=user_supplied_plan)
    gate.spec = spec
    run.go(State.VALIDATING_SPECIFICATION)
    result = validate_spec(spec, plan=plan, draw_order=ordered, caps=caps)
    spec["admin"]["validation_errors"] = [i.to_dict() for i in result.issues]
    stem = spec["metadata"]["artwork_id"]
    if result.blocking:
        spec["admin"]["specification_status"] = "INVALID"
        gate.spec_path = _write(directory, f"spec-{stem}.json", {"spec": spec, "log": run.history})
        run.go(State.FAILED, reason="specification blocked", issues=len(result.blocking))
        _write(directory, f"gate-{stem}.json", {"history": run.history})
        top = "; ".join(f"{i.path}: {i.message}" for i in result.blocking[:4])
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"the artwork specification did not pass validation ({len(result.blocking)} blocking): {top}",
                        retryable=False, details={"state": "FAILED", "issues": [i.to_dict() for i in result.blocking[:20]],
                                                  "specification": gate.spec_path})
    spec["admin"]["specification_status"] = "VALIDATED"
    gate.validated = True
    run.go(State.BUILDING_DRAWING_PLAN, stages=len(stages), operations=len(spec["strokes"]))
    run.go(State.CHECKING_READINESS)
    ready = check_readiness(spec, probe)
    if any(i.level == Level.BLOCKING for i in ready):
        spec["admin"]["validation_errors"] += [i.to_dict() for i in ready]
        gate.spec_path = _write(directory, f"spec-{stem}.json", {"spec": spec, "log": run.history})
        run.go(State.FAILED, reason="not ready")
        raise ToolError(ErrorCode.EXECUTION_FAILED, "Paint is not ready to draw: " + "; ".join(i.message for i in ready if i.level == Level.BLOCKING),
                        retryable=True, details={"state": "FAILED", "issues": [i.to_dict() for i in ready]})
    gate.ready = True
    run.go(State.READY_TO_DRAW)
    gate.spec_path = _write(directory, f"spec-{stem}.json", {"spec": spec, "log": run.history})
    gate.begin(driver.probe() if hasattr(driver, "probe") else probe)
    spec["execution"]["completion_status"] = "running"
    if hasattr(driver, "gate"):
        driver.gate = gate
    try:
        outcome = engine.run_plan(plan, driver, should_stop=should_stop, tolerance=tolerance, resume=resume)
    except ToolError as exc:
        run.go(State.RECOVERING, error=exc.message[:200])
        run.go(State.FAILED, reason=exc.message[:200])
        spec["execution"]["completion_status"] = "failed"
        spec["execution"]["actual_result"] = {"ok": False, "error": exc.to_dict()}
        spec["admin"]["updated_at"] = spec_mod.datetime.now(spec_mod.timezone.utc).isoformat(timespec="seconds")
        gate.spec_path = _write(directory, f"spec-{stem}.json", {"spec": spec, "log": run.history})
        exc.details = {**(exc.details or {}), "specification": gate.spec_path, "state": "FAILED"}
        raise
    for e in outcome.events:
        if e.get("event") == "recovery":
            gate.recovered(e)
    run.go(State.VALIDATING_RESULT, drawn=outcome.drawn)
    shot = None
    try:
        img = driver.capture()
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            shot = str(directory / f"result-{stem}.png")
            img.save(shot, format="PNG")
    except Exception:  # noqa: BLE001 - a missing screenshot simply means no evidence
        shot = None
    actual = {"ok": bool(outcome.ok), "drawn": outcome.drawn, "failed_objects": outcome.final.failed_ids,
              "spill_ratio": round(outcome.final.spill_ratio, 4), "summary": outcome.final.summary(), "screenshot": shot,
              "events": outcome.events, "skipped_fills": outcome.skipped_fills}
    spec["execution"]["actual_result"] = actual
    spec["admin"]["evidence"] = ([{"kind": "screenshot", "path": shot, "at": spec_mod.datetime.now(spec_mod.timezone.utc).isoformat(timespec="seconds")}]
                                 if shot else [])
    if outcome.ok and shot:
        spec["execution"]["completion_status"] = "verified"
        check = validate_spec(spec, caps=caps)  # a verified claim is re-checked for its evidence
        if check.blocking:
            spec["execution"]["completion_status"] = "failed"
            run.go(State.FAILED, reason="evidence check failed")
        else:
            run.go(State.COMPLETED)
    else:
        spec["execution"]["completion_status"] = "failed"
        run.go(State.FAILED, reason="result did not match the plan" if not outcome.ok else "no screenshot evidence")
    spec["admin"]["updated_at"] = spec_mod.datetime.now(spec_mod.timezone.utc).isoformat(timespec="seconds")
    gate.spec_path = _write(directory, f"spec-{stem}.json", {"spec": spec, "log": run.history, "events": gate.events})
    return outcome, gate
