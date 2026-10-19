"""Validation rule engine over the intermediate drawing model (spec sections 7, 15 and 18). Pure Python.

Every rule has a rule_id, description, scope, parameters, a default severity (BLOCKER / ERROR / WARNING / INFO) and
produces a result PASS / FAIL / WARN / NOT_RUN with evidence. Principles:

* Tolerances are configurable per purpose (zero_length, closure_gap, duplicate, dimension, ...) and expressed in
  millimetres (or explicitly in drawing units); there is no single global epsilon. If the drawing unit is unknown a
  length tolerance cannot be applied and the rule is NOT_RUN, not silently "PASS".
* A rule with nothing to check is NOT_RUN ("no applicable entities"), never a vacuous PASS.
* Any open BLOCKER makes the overall result FAIL. ERRORs close only by a fix or by a complete waiver (approver,
  reason, date, scope); a BLOCKER can never be waived.
* Duplicate entities are only reported (they may be intentional overstrike); nothing is deleted or rounded here.
* A dimension whose displayed value differs from its measured geometry is a CONFLICT, reported with both values.
* Limits (maximum riser height, ...) are applied only when supplied as parameters from a verified profile.
"""

from __future__ import annotations

import enum
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lga_worker.autocad import geom2d, layers
from lga_worker.autocad.provenance import Entity, EntityType, load_entities
from lga_worker.autocad.standards import profile_value


class Severity(str, enum.Enum):
    BLOCKER = "BLOCKER"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


SEVERITY_RANK = {Severity.BLOCKER: 3, Severity.ERROR: 2, Severity.WARNING: 1, Severity.INFO: 0}


class Result(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    NOT_RUN = "NOT_RUN"


SCOPES = ("FILE", "GEOMETRY", "ANNOTATION", "COORDINATION", "PLOT")

# --------------------------------------------------------------------------- tolerances

# Defaults per purpose, in millimetres (lengths) or as a ratio (the *_rel entries). A project should override them.
DEFAULT_TOLERANCES_MM: dict[str, float] = {
    "zero_length": 0.1, "closure_gap": 1.0, "duplicate": 0.5, "intersection": 0.01, "dimension": 1.0,
    "opening_offset": 5.0, "elevation": 5.0, "schedule_dimension": 1.0, "riser_total": 2.0,
}
DEFAULT_TOLERANCES_REL: dict[str, float] = {"area_rel": 0.005, "scale_rel": 0.001}


class ToleranceError(ValueError):
    pass


class Tolerances:
    """Per-purpose tolerances. Overrides: {purpose: number_in_mm} or {purpose: {"value": v, "unit": "mm"|"drawing"}}."""

    def __init__(self, unit_mm: float | None, overrides: Mapping[str, Any] | None = None) -> None:
        self.unit_mm = unit_mm
        self.mm = dict(DEFAULT_TOLERANCES_MM)
        self.rel = dict(DEFAULT_TOLERANCES_REL)
        self.drawing_units: dict[str, float] = {}
        for purpose, raw in (overrides or {}).items():
            unit = "mm"
            value = raw
            if isinstance(raw, Mapping):
                value, unit = raw.get("value"), str(raw.get("unit", "mm")).lower()
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ToleranceError(f"tolerance {purpose!r} must be a positive number, got {raw!r}")
            if purpose in self.rel:
                self.rel[purpose] = float(value)
            elif purpose in self.mm:
                if unit == "drawing":
                    self.drawing_units[purpose] = float(value)
                elif unit == "mm":
                    self.mm[purpose] = float(value)
                else:
                    raise ToleranceError(f"tolerance {purpose!r}: unit must be 'mm' or 'drawing', got {unit!r}")
            else:
                known = sorted(set(self.mm) | set(self.rel))
                raise ToleranceError(f"unknown tolerance purpose {purpose!r}; known: {known}")

    def length(self, purpose: str) -> float | None:
        """The tolerance in drawing units, or None when the drawing unit is unknown and none was given in drawing units."""
        if purpose in self.drawing_units:
            return self.drawing_units[purpose]
        if purpose not in self.mm:
            raise ToleranceError(f"unknown tolerance purpose {purpose!r}")
        if not self.unit_mm:
            return None
        return self.mm[purpose] / self.unit_mm

    def relative(self, purpose: str) -> float:
        return self.rel[purpose]

    def describe(self) -> dict[str, Any]:
        return {"drawing_unit_mm": self.unit_mm, "lengths_mm": self.mm, "lengths_drawing_units": self.drawing_units,
                "relative": self.rel}


# --------------------------------------------------------------------------- model

@dataclass
class DrawingModel:
    meta: dict[str, Any]
    entities: list[Entity]
    schedules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DrawingModel:
        if not isinstance(raw, Mapping):
            raise ValueError("model must be an object with meta/entities/schedules/profile")
        extra = set(raw) - {"meta", "entities", "schedules", "profile"}
        if extra:
            raise ValueError(f"unknown model key(s) {sorted(extra)}")
        return cls(meta=dict(raw.get("meta") or {}), entities=load_entities(list(raw.get("entities") or [])),
                   schedules={k: list(v) for k, v in (raw.get("schedules") or {}).items()},
                   profile=dict(raw.get("profile") or {}))

    @property
    def units(self) -> str | None:
        """Drawing unit name: INSUNITS when read from the file, else the declared meta.units, else None."""
        from_file = layers.insunits_name(self.meta.get("insunits"))
        declared = self.meta.get("units")
        name = from_file or (str(declared).strip().lower() if declared else None)
        return name if name in layers.UNIT_TO_MM else None

    @property
    def unit_mm(self) -> float | None:
        return layers.unit_to_mm(self.units)

    def of_type(self, *types: EntityType) -> list[Entity]:
        return [e for e in self.entities if e.entity_type in types]

    def by_id(self) -> dict[str, Entity]:
        return {e.entity_id: e for e in self.entities}


@dataclass
class Issue:
    message: str
    entity_ids: list[str] = field(default_factory=list)
    severity: Severity | None = None
    kind: str = "ISSUE"          # ISSUE | CONFLICT | INCOMPLETE
    detail: dict[str, Any] = field(default_factory=dict)
    waived: bool = False

    def to_dict(self, default: Severity) -> dict[str, Any]:
        return {"message": self.message, "entity_ids": self.entity_ids, "severity": (self.severity or default).value,
                "kind": self.kind, "detail": self.detail, "waived": self.waived}


@dataclass
class Outcome:
    issues: list[Issue] = field(default_factory=list)
    evidence: str = ""
    checked: int = 0
    not_run: str | None = None

    @classmethod
    def skipped(cls, reason: str) -> Outcome:
        return cls(not_run=reason)


@dataclass
class RuleContext:
    model: DrawingModel
    tol: Tolerances
    params: dict[str, Any]

    def length_tol(self, purpose: str) -> float | None:
        return self.tol.length(purpose)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    description: str
    scope: str
    severity: Severity
    check: Callable[[RuleContext], Outcome]
    parameters: Mapping[str, Any] = field(default_factory=dict)
    tolerance_purposes: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "description": self.description, "scope": self.scope,
                "severity": self.severity.value, "parameters": dict(self.parameters),
                "tolerances": list(self.tolerance_purposes)}


@dataclass
class RuleResult:
    rule_id: str
    description: str
    scope: str
    severity: str
    result: Result
    evidence: str
    parameters: dict[str, Any]
    issues: list[dict[str, Any]]
    checked: int

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "description": self.description, "scope": self.scope,
                "severity": self.severity, "result": self.result.value, "evidence": self.evidence,
                "parameters": self.parameters, "issues": self.issues, "checked": self.checked}


# --------------------------------------------------------------------------- geometry helpers

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip().replace(",", "."))
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _ids(entities: Sequence[Entity]) -> list[str]:
    return [e.entity_id for e in entities]


def _need_tol(ctx: RuleContext, *purposes: str) -> tuple[dict[str, float], Outcome | None]:
    tols: dict[str, float] = {}
    for purpose in purposes:
        value = ctx.length_tol(purpose)
        if value is None:
            return {}, Outcome.skipped(f"drawing units unknown: tolerance '{purpose}' cannot be applied "
                                       "(declare units or give the tolerance in drawing units)")
        tols[purpose] = value
    return tols, None


def _same_geometry(a: Mapping[str, Any], b: Mapping[str, Any], tol: float) -> bool:
    kind = a.get("type")
    if kind != b.get("type"):
        return False

    def close(p: Sequence[float], q: Sequence[float]) -> bool:
        return geom2d.dist(geom2d.pt(p), geom2d.pt(q)) <= tol

    if kind == "line":
        return (close(a["start"], b["start"]) and close(a["end"], b["end"])) or \
               (close(a["start"], b["end"]) and close(a["end"], b["start"]))
    if kind == "polyline":
        pa, pb = a.get("points", []), b.get("points", [])
        if len(pa) != len(pb) or bool(a.get("closed")) != bool(b.get("closed")):
            return False
        return all(close(p, q) for p, q in zip(pa, pb)) or all(close(p, q) for p, q in zip(pa, reversed(pb)))
    if kind in ("circle", "arc"):
        same = close(a["center"], b["center"]) and abs(float(a["radius"]) - float(b["radius"])) <= tol
        if kind == "arc":
            same = same and abs(float(a.get("start_angle", 0)) - float(b.get("start_angle", 0))) < 1e-6 \
                and abs(float(a.get("end_angle", 0)) - float(b.get("end_angle", 0))) < 1e-6
        return same
    if kind == "point":
        return close(a["point"], b["point"])
    if kind == "segments":
        sa, sb = a.get("segments", []), b.get("segments", [])
        return len(sa) == len(sb) and all(any(_same_geometry({"type": "line", "start": x[0], "end": x[1]},
                                                             {"type": "line", "start": y[0], "end": y[1]}, tol)
                                              for y in sb) for x in sa)
    return False


def _entity_points(entity: Entity) -> list[geom2d.Point]:
    g = entity.geometry or {}
    kind = g.get("type")
    if kind == "line":
        return [geom2d.pt(g["start"]), geom2d.pt(g["end"])]
    if kind == "polyline":
        return [geom2d.pt(p) for p in g.get("points", [])]
    if kind == "segments":
        return [geom2d.pt(p) for s in g.get("segments", []) for p in s]
    if kind in ("circle", "arc"):
        cx, cy = geom2d.pt(g["center"])
        r = float(g["radius"])
        return [(cx - r, cy - r), (cx + r, cy + r)]
    if kind == "point":
        return [geom2d.pt(g["point"])]
    return []


def _room_polygon(entity: Entity, closure_gap: float) -> list[geom2d.Point] | None:
    g = entity.geometry or {}
    if g.get("type") == "polyline" and g.get("closed"):
        return geom2d.polygon_points(g)
    if g.get("type") == "segments":
        return geom2d.ring_from_segments(geom2d.geometry_segments(g), closure_gap)
    return None


def _area_m2(points: Sequence[geom2d.Point], unit_mm: float) -> float:
    return geom2d.polygon_area(points) * (unit_mm / 1000.0) ** 2


# --------------------------------------------------------------------------- FILE rules

def rule_units(ctx: RuleContext) -> Outcome:
    model = ctx.model
    expected = profile_value(model.profile, "units")
    if model.meta.get("insunits") is not None:
        check = layers.check_units(model.meta.get("insunits"), str(expected) if expected else None)
        if check["result"] == "PASS":
            return Outcome(evidence=check["evidence"], checked=1)
        sev = Severity(check.get("severity", "WARNING"))
        return Outcome([Issue(check["evidence"], severity=sev)], check["evidence"], 1)
    if model.units is None:
        msg = "drawing units are unknown (no INSUNITS read, none declared): model-space geometry cannot be trusted"
        return Outcome([Issue(msg, severity=Severity.BLOCKER)], msg, 1)
    if expected and str(expected).strip().lower() != model.units:
        msg = f"model declares units {model.units!r} but the profile requires {expected!r}"
        return Outcome([Issue(msg, severity=Severity.BLOCKER)], msg, 1)
    return Outcome(evidence=f"model declares units {model.units} (INSUNITS not read from a file, so this is a declaration)",
                   checked=1)


def rule_model_scale(ctx: RuleContext) -> Outcome:
    ref = ctx.model.meta.get("scale_reference")
    if not isinstance(ref, Mapping):
        return Outcome.skipped("no scale_reference {real_length_mm, measured_length} given: 1:1 model space not provable")
    check = layers.check_model_scale_1to1(ctx.model.units, _num(ref.get("measured_length")),
                                          _num(ref.get("real_length_mm")), ctx.tol.relative("scale_rel"))
    if check["result"] == "NOT_RUN":
        return Outcome.skipped(check["evidence"])
    if check["result"] == "FAIL":
        return Outcome([Issue(check["evidence"], severity=Severity.BLOCKER)], check["evidence"], 1)
    return Outcome(evidence=check["evidence"], checked=1)


def rule_xref(ctx: RuleContext) -> Outcome:
    xrefs = ctx.model.meta.get("xrefs")
    if xrefs is None:
        return Outcome.skipped("xref list not provided (file inventory not run)")
    bad = [x for x in xrefs if not x.get("resolved")]
    issues = [Issue(f"xref {x.get('name')!r} not resolved (path {x.get('path')!r})") for x in bad]
    return Outcome(issues, f"{len(xrefs)} xref(s), {len(bad)} unresolved", len(xrefs)) if xrefs else \
        Outcome(evidence="file has no xrefs", checked=1)


def rule_fonts(ctx: RuleContext) -> Outcome:
    fonts = ctx.model.meta.get("fonts")
    if fonts is None:
        return Outcome.skipped("font list not provided (file inventory not run)")
    issues = [Issue(f"font {f.get('name')!r} {'substituted by ' + str(f.get('substitute')) if f.get('substitute') else 'missing'}")
              for f in fonts if not f.get("resolved") or f.get("substitute")]
    return Outcome(issues, f"{len(fonts)} font(s), {len(issues)} missing/substituted", max(1, len(fonts)))


def rule_audit(ctx: RuleContext) -> Outcome:
    audit = ctx.model.meta.get("audit")
    if not isinstance(audit, Mapping) or "ran" not in audit:
        return Outcome.skipped("AUDIT was not run or its log was not provided")
    if not audit["ran"]:
        return Outcome([Issue("AUDIT was not run")], "AUDIT not run", 1)
    left = int(audit.get("errors_found", 0)) - int(audit.get("errors_fixed", 0))
    if left > 0:
        return Outcome([Issue(f"AUDIT left {left} unfixed error(s)")], f"AUDIT found {audit.get('errors_found')}, "
                       f"fixed {audit.get('errors_fixed')}", 1)
    return Outcome(evidence=f"AUDIT ran: found {audit.get('errors_found', 0)}, fixed {audit.get('errors_fixed', 0)}"
                            f"{'; log ' + str(audit['log']) if audit.get('log') else '; no log path given'}", checked=1)


def rule_recovery(ctx: RuleContext) -> Outcome:
    flag = ctx.model.meta.get("recovery_warning")
    if flag is None:
        return Outcome.skipped("open/recovery state not provided (file not opened by AutoCAD in this run)")
    if flag:
        return Outcome([Issue("the DWG opened with an unhandled recovery warning", severity=Severity.BLOCKER)],
                       "recovery warning present", 1)
    return Outcome(evidence="opened without recovery warning", checked=1)


# --------------------------------------------------------------------------- GEOMETRY rules

def rule_zero_length(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "zero_length")
    if skip:
        return skip
    tol = tols["zero_length"]
    issues: list[Issue] = []
    checked = 0
    for e in ctx.model.entities:
        segments = geom2d.geometry_segments(e.geometry)
        if not segments:
            continue
        checked += 1
        for i, (a, b) in enumerate(segments):
            if geom2d.dist(a, b) <= tol:
                issues.append(Issue(f"{e.entity_id}: segment {i} has length {geom2d.dist(a, b):g} <= {tol:g}",
                                    [e.entity_id], detail={"segment": i, "tolerance": tol}))
    if not checked:
        return Outcome.skipped("no line/polyline entities")
    return Outcome(issues, f"{checked} entities checked with zero_length tolerance {tol:g}", checked)


def rule_arc_valid(ctx: RuleContext) -> Outcome:
    arcs = [e for e in ctx.model.entities if (e.geometry or {}).get("type") in ("arc", "circle")]
    if not arcs:
        return Outcome.skipped("no arcs or circles")
    issues = []
    for e in arcs:
        g = e.geometry or {}
        r = _num(g.get("radius"))
        if r is None or r <= 0:
            issues.append(Issue(f"{e.entity_id}: invalid radius {g.get('radius')!r}", [e.entity_id]))
        elif g.get("type") == "arc":
            s, t = _num(g.get("start_angle")), _num(g.get("end_angle"))
            if s is None or t is None or math.isclose((t - s) % 360.0, 0.0, abs_tol=1e-9):
                issues.append(Issue(f"{e.entity_id}: arc has no sweep (start {g.get('start_angle')!r}, "
                                    f"end {g.get('end_angle')!r})", [e.entity_id]))
    return Outcome(issues, f"{len(arcs)} arc/circle entities checked", len(arcs))


def rule_closure(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "closure_gap")
    if skip:
        return skip
    gap = tols["closure_gap"]
    issues: list[Issue] = []
    checked = 0
    for e in ctx.model.entities:
        g = e.geometry or {}
        if g.get("type") != "polyline":
            continue
        must_close = e.entity_type is EntityType.ROOM or bool(e.properties.get("expect_closed"))
        if not must_close:
            continue
        checked += 1
        points = [geom2d.pt(p) for p in g.get("points", [])]
        if g.get("closed"):
            if len(points) < 3:
                issues.append(Issue(f"{e.entity_id}: closed polyline with only {len(points)} vertices", [e.entity_id]))
            continue
        if len(points) >= 3 and geom2d.dist(points[0], points[-1]) <= gap:
            issues.append(Issue(f"{e.entity_id}: polyline is open although its ends meet within {gap:g}; "
                                "set it closed", [e.entity_id], detail={"gap": geom2d.dist(points[0], points[-1])}))
        else:
            issues.append(Issue(f"{e.entity_id}: polyline expected to be closed is open", [e.entity_id]))
    if not checked:
        return Outcome.skipped("no polylines that must be closed (rooms or expect_closed)")
    return Outcome(issues, f"{checked} must-be-closed polylines checked (closure gap {gap:g})", checked)


def rule_room_boundary(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "closure_gap")
    if skip:
        return skip
    gap = tols["closure_gap"]
    rooms = ctx.model.of_type(EntityType.ROOM)
    if not rooms:
        return Outcome.skipped("no ROOM entities")
    issues: list[Issue] = []
    for room in rooms:
        g = room.geometry or {}
        if g.get("type") == "polyline":
            if not g.get("closed"):
                issues.append(Issue(f"room {room.entity_id}: boundary polyline is open: no area may be stated",
                                    [room.entity_id]))
        elif g.get("type") == "segments":
            ok, why = geom2d.chain_closed(geom2d.geometry_segments(g), gap)
            if not ok:
                issues.append(Issue(f"room {room.entity_id}: {why}", [room.entity_id]))
        else:
            issues.append(Issue(f"room {room.entity_id}: no boundary geometry", [room.entity_id]))
    return Outcome(issues, f"{len(rooms)} room boundaries checked (closure gap {gap:g})", len(rooms))


def rule_self_intersection(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "intersection")
    if skip:
        return skip
    tol = tols["intersection"]
    issues: list[Issue] = []
    checked = 0
    for e in ctx.model.entities:
        g = e.geometry or {}
        if g.get("type") not in ("polyline", "segments") or len(geom2d.geometry_segments(g)) < 3:
            continue
        checked += 1
        hits = geom2d.self_intersections(g, tol)
        if hits:
            issues.append(Issue(f"{e.entity_id}: {len(hits)} self-intersection(s), first between segments "
                                f"{hits[0][0]} and {hits[0][1]}", [e.entity_id], detail={"pairs": hits[:10]}))
    if not checked:
        return Outcome.skipped("no polylines with 3 or more segments")
    return Outcome(issues, f"{checked} polylines checked for self-intersection", checked)


def rule_duplicates(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "duplicate")
    if skip:
        return skip
    tol = tols["duplicate"]
    buckets: dict[tuple[Any, ...], list[Entity]] = defaultdict(list)
    for e in ctx.model.entities:
        g = e.geometry
        if g:
            n = len(g.get("points", [])) if g.get("type") == "polyline" else 0
            buckets[(e.entity_type, e.layer, e.level_id, g.get("type"), n)].append(e)  # one storey's wall is not a duplicate of the next storey's
    pairs = sum(len(b) * (len(b) - 1) // 2 for b in buckets.values())
    limit = int(ctx.params.get("max_pairs", 2_000_000))
    if pairs > limit:
        return Outcome.skipped(f"{pairs} candidate pairs exceed max_pairs={limit}; raise the parameter to run")
    issues: list[Issue] = []
    for bucket in buckets.values():
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                if _same_geometry(a.geometry or {}, b.geometry or {}, tol):
                    issues.append(Issue(f"{a.entity_id} and {b.entity_id} have the same geometry and layer "
                                        "(duplicate or intentional overstrike; not deleted)",
                                        [a.entity_id, b.entity_id]))
    total = sum(len(b) for b in buckets.values())
    if not total:
        return Outcome.skipped("no entities with geometry")
    return Outcome(issues, f"{total} entities compared within tolerance {tol:g}", total)


def rule_bounds(ctx: RuleContext) -> Outcome:
    bounds = ctx.params.get("drawing_bounds")
    if not bounds:
        return Outcome.skipped("no drawing_bounds [minx, miny, maxx, maxy] parameter given")
    minx, miny, maxx, maxy = (float(v) for v in bounds)
    issues = []
    checked = 0
    for e in ctx.model.entities:
        points = _entity_points(e)
        if not points:
            continue
        checked += 1
        if any(not (minx <= x <= maxx and miny <= y <= maxy) for x, y in points):
            issues.append(Issue(f"{e.entity_id}: geometry lies outside the drawing area", [e.entity_id]))
    if not checked:
        return Outcome.skipped("no entities with geometry")
    return Outcome(issues, f"{checked} entities checked against bounds {list(bounds)}", checked)


def rule_wall_thickness(ctx: RuleContext) -> Outcome:
    walls = ctx.model.of_type(EntityType.WALL)
    if not walls:
        return Outcome.skipped("no WALL entities")
    issues = []
    for w in walls:
        thickness = _num(w.prop("thickness"))
        rule = str(w.prop("centerline_rule") or "").strip()
        if not w.geometry:
            issues.append(Issue(f"wall {w.entity_id}: no geometry", [w.entity_id]))
        if w.prop("thickness") is not None and (thickness is None or thickness <= 0):
            issues.append(Issue(f"wall {w.entity_id}: thickness {w.prop('thickness')!r} is not a positive number",
                                [w.entity_id]))
        elif thickness is None and not rule:
            issues.append(Issue(f"wall {w.entity_id}: no thickness and no stated centreline convention", [w.entity_id]))
    return Outcome(issues, f"{len(walls)} walls checked", len(walls))


def rule_opening_host(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "opening_offset")
    if skip:
        return skip
    offset = tols["opening_offset"]
    openings = ctx.model.of_type(EntityType.DOOR, EntityType.WINDOW)
    if not openings:
        return Outcome.skipped("no DOOR/WINDOW entities")
    by_id = ctx.model.by_id()
    issues: list[Issue] = []
    for o in openings:
        host_id = o.relationships.get("host_wall")
        label = f"{o.entity_type.value.lower()} {o.entity_id}"
        if not host_id:
            issues.append(Issue(f"{label}: no host wall (a symbol drawn over a wall is not an opening)", [o.entity_id]))
            continue
        host = by_id.get(str(host_id))
        if host is None or host.entity_type is not EntityType.WALL:
            issues.append(Issue(f"{label}: host_wall {host_id!r} is not a WALL in the model", [o.entity_id]))
            continue
        if o.level_id and host.level_id and o.level_id != host.level_id:
            issues.append(Issue(f"{label}: on level {o.level_id} but host wall {host.entity_id} is on {host.level_id}",
                                [o.entity_id, host.entity_id]))
        opening_ref = o.relationships.get("opening")
        if opening_ref and (by_id.get(str(opening_ref)) is None or by_id[str(opening_ref)].entity_type is not EntityType.OPENING):
            issues.append(Issue(f"{label}: opening {opening_ref!r} is not an OPENING entity", [o.entity_id]))
        position = (o.geometry or {}).get("point") if (o.geometry or {}).get("type") == "point" else None
        segments = geom2d.geometry_segments(host.geometry)
        if position is not None and segments:
            half = (_num(host.prop("thickness")) or 0.0) / 2.0
            nearest = min(geom2d.point_segment_distance(geom2d.pt(position), a, b) for a, b in segments)
            if nearest > half + offset:
                issues.append(Issue(f"{label}: sits {nearest:g} from wall {host.entity_id}'s axis "
                                    f"(max {half + offset:g}): not inside the wall", [o.entity_id, host.entity_id],
                                    detail={"distance": nearest, "allowed": half + offset}))
        width = _num(o.prop("width"))
        if o.prop("width") is not None and (width is None or width <= 0):
            issues.append(Issue(f"{label}: width {o.prop('width')!r} is not positive", [o.entity_id]))
    return Outcome(issues, f"{len(openings)} doors/windows checked against their host walls", len(openings))


# --------------------------------------------------------------------------- ANNOTATION rules

def _dim_values(e: Entity) -> tuple[float | None, float | None, bool]:
    """(measured length from the definition points, displayed value, displayed is non numeric text)."""
    g = e.geometry or {}
    measured = geom2d.dist(geom2d.pt(g["p1"]), geom2d.pt(g["p2"])) if "p1" in g and "p2" in g else None
    raw = e.prop("displayed_value", e.prop("override_value"))
    shown = _num(raw)
    return measured, shown, raw is not None and shown is None


def rule_dimension_override(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "dimension")
    if skip:
        return skip
    tol = tols["dimension"]
    dims = ctx.model.of_type(EntityType.DIMENSION)
    if not dims:
        return Outcome.skipped("no DIMENSION entities")
    issues: list[Issue] = []
    checked = 0
    for d in dims:
        measured, shown, text_only = _dim_values(d)
        if measured is None:
            issues.append(Issue(f"dimension {d.entity_id}: no definition points p1/p2 to measure", [d.entity_id],
                                kind="INCOMPLETE", severity=Severity.WARNING))
            continue
        if text_only:
            issues.append(Issue(f"dimension {d.entity_id}: displayed text {d.prop('displayed_value', d.prop('override_value'))!r} "
                                "is not numeric and cannot be compared with the geometry", [d.entity_id],
                                kind="INCOMPLETE", severity=Severity.WARNING))
            continue
        if shown is None:
            continue
        checked += 1
        if abs(shown - measured) > tol:
            issues.append(Issue(f"CONFLICT dimension {d.entity_id}: shows {shown:g} but the geometry measures "
                                f"{measured:g} (difference {abs(shown - measured):g} > {tol:g}); neither side was changed",
                                [d.entity_id], kind="CONFLICT",
                                detail={"displayed": shown, "measured": measured, "tolerance": tol}))
    return Outcome(issues, f"{checked} dimensions with a displayed value compared to geometry (tolerance {tol:g})",
                   max(checked, 1) if issues else checked)


def rule_dimension_chain(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "dimension")
    if skip:
        return skip
    tol = tols["dimension"]
    chains: dict[str, dict[str, list[Entity]]] = defaultdict(lambda: {"segment": [], "overall": []})
    for d in ctx.model.of_type(EntityType.DIMENSION):
        cid, role = d.prop("chain_id"), str(d.prop("chain_role") or "")
        if cid and role in ("segment", "overall"):
            chains[str(cid)][role].append(d)
    if not chains:
        return Outcome.skipped("no dimension chains (chain_id + chain_role segment/overall)")
    issues = []
    for cid, parts in chains.items():
        if len(parts["overall"]) != 1 or not parts["segment"]:
            issues.append(Issue(f"chain {cid}: needs exactly one overall and at least one segment dimension",
                                _ids(parts["overall"] + parts["segment"]), kind="INCOMPLETE"))
            continue

        def value(d: Entity) -> float | None:
            measured, shown, _ = _dim_values(d)
            return shown if shown is not None else measured

        values = [value(d) for d in parts["segment"]]
        overall = value(parts["overall"][0])
        if overall is None or any(v is None for v in values):
            issues.append(Issue(f"chain {cid}: a dimension has no usable value", _ids(parts["segment"]), kind="INCOMPLETE"))
        elif abs(sum(values) - overall) > tol * (len(values) + 1):  # type: ignore[arg-type]
            issues.append(Issue(f"chain {cid}: segments sum to {sum(values):g} but overall is {overall:g}",  # type: ignore[arg-type]
                                _ids(parts["segment"] + parts["overall"]), kind="CONFLICT",
                                detail={"sum": sum(values), "overall": overall}))  # type: ignore[arg-type]
    return Outcome(issues, f"{len(chains)} dimension chain(s) checked", len(chains))


def rule_text(ctx: RuleContext) -> Outcome:
    texts = [e for e in ctx.model.of_type(EntityType.ANNOTATION) if "text" in e.properties]
    if not texts:
        return Outcome.skipped("no ANNOTATION entities with text")
    issues = [Issue(f"annotation {e.entity_id}: empty text", [e.entity_id]) for e in texts
              if not str(e.properties.get("text") or "").strip()]
    return Outcome(issues, f"{len(texts)} text annotations checked", len(texts))


def rule_grid_labels(ctx: RuleContext) -> Outcome:
    grids = ctx.model.of_type(EntityType.GRID)
    if not grids:
        return Outcome.skipped("no GRID entities")
    seen: dict[str, str] = {}
    issues = []
    for g in grids:
        label = str(g.prop("label") or "").strip()
        if not label:
            issues.append(Issue(f"grid {g.entity_id}: no label", [g.entity_id]))
        elif label in seen:
            issues.append(Issue(f"grid label {label!r} used by {seen[label]} and {g.entity_id}", [seen[label], g.entity_id]))
        else:
            seen[label] = g.entity_id
    return Outcome(issues, f"{len(grids)} grid lines checked", len(grids))


def rule_marker_sheets(ctx: RuleContext) -> Outcome:
    markers = ctx.model.of_type(EntityType.DETAIL_MARKER)
    sheets = ctx.model.meta.get("sheets")
    if not markers:
        return Outcome.skipped("no DETAIL_MARKER entities")
    if sheets is None:
        return Outcome.skipped("sheet list (meta.sheets) not provided")
    known = {str(s) for s in sheets}
    issues = []
    for m in markers:
        target = m.prop("target_sheet")
        if not target:
            issues.append(Issue(f"marker {m.entity_id}: no target sheet", [m.entity_id]))
        elif str(target) not in known:
            issues.append(Issue(f"marker {m.entity_id}: target sheet {target!r} does not exist", [m.entity_id]))
    return Outcome(issues, f"{len(markers)} markers checked against {len(known)} sheets", len(markers))


# --------------------------------------------------------------------------- COORDINATION rules

def rule_code_unique(ctx: RuleContext) -> Outcome:
    items = ctx.model.of_type(EntityType.DOOR, EntityType.WINDOW)
    if not items:
        return Outcome.skipped("no DOOR/WINDOW entities")
    issues: list[Issue] = []
    for etype in (EntityType.DOOR, EntityType.WINDOW):
        seen: dict[str, str] = {}
        for e in (x for x in items if x.entity_type is etype):
            code = str(e.prop("code") or "").strip()
            if not code:
                issues.append(Issue(f"{etype.value.lower()} {e.entity_id}: no code", [e.entity_id]))
            elif code in seen:
                issues.append(Issue(f"{etype.value.lower()} code {code!r} is used by {seen[code]} and {e.entity_id}: "
                                    "codes must be unique", [seen[code], e.entity_id]))
            else:
                seen[code] = e.entity_id
    return Outcome(issues, f"{len(items)} door/window codes checked", len(items))


def rule_schedule_match(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "schedule_dimension")
    if skip:
        return skip
    tol = tols["schedule_dimension"]
    issues: list[Issue] = []
    checked = 0
    for key, etype in (("door", EntityType.DOOR), ("window", EntityType.WINDOW)):
        rows = ctx.model.schedules.get(key)
        symbols = ctx.model.of_type(etype)
        if rows is None:
            continue
        checked += 1
        by_code = {}
        for s in symbols:
            by_code.setdefault(str(s.prop("code") or ""), s)
        row_codes = set()
        for row in rows:
            code = str(row.get("code") or "")
            if code in row_codes:
                issues.append(Issue(f"{key} schedule lists {code!r} twice", []))
            row_codes.add(code)
            symbol = by_code.get(code)
            if symbol is None:
                issues.append(Issue(f"{key} schedule row {code!r} has no symbol in the plan", []))
                continue
            for dim in ("width", "height"):
                a, b = _num(row.get(dim)), _num(symbol.prop(dim))
                if a is not None and b is not None and abs(a - b) > tol:
                    issues.append(Issue(f"{key} {code}: schedule {dim} {a:g} vs symbol {b:g}", [symbol.entity_id],
                                        detail={"schedule": a, "symbol": b, "tolerance": tol}))
            if row.get("level_id") and symbol.level_id and row["level_id"] != symbol.level_id:
                issues.append(Issue(f"{key} {code}: schedule level {row['level_id']} vs symbol level {symbol.level_id}",
                                    [symbol.entity_id]))
        for code, symbol in by_code.items():
            if code not in row_codes:
                issues.append(Issue(f"{key} symbol {symbol.entity_id} (code {code!r}) is missing from the schedule",
                                    [symbol.entity_id]))
    if not checked:
        return Outcome.skipped("no door/window schedule provided")
    return Outcome(issues, f"{checked} schedule(s) compared to plan symbols (tolerance {tol:g})", checked)


def rule_area(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "closure_gap")
    if skip:
        return skip
    gap = tols["closure_gap"]
    unit_mm = ctx.model.unit_mm
    rel = ctx.tol.relative("area_rel")
    rooms = ctx.model.of_type(EntityType.ROOM)
    default_rule = ctx.model.meta.get("area_rule") or profile_value(ctx.model.profile, "area_rule")
    issues: list[Issue] = []
    computed: dict[str, float] = {}
    checked = 0
    for room in rooms:
        stated = _num(room.prop("stated_area"))
        if room.prop("stated_area") is not None and stated is None:
            issues.append(Issue(f"room {room.entity_id}: stated area {room.prop('stated_area')!r} is not a number",
                                [room.entity_id]))
            continue
        polygon = _room_polygon(room, gap)
        if polygon is not None and unit_mm:
            computed[room.entity_id] = _area_m2(polygon, unit_mm)
        if stated is None:
            continue
        checked += 1
        if not (room.prop("area_rule") or default_rule):
            issues.append(Issue(f"room {room.entity_id}: area {stated:g} is stated without a calculation rule "
                                "(centreline / inner face / finish face ...)", [room.entity_id]))
        if polygon is None:
            issues.append(Issue(f"room {room.entity_id}: area {stated:g} stated but there is no closed boundary to "
                                "recompute it from", [room.entity_id]))
        elif room.entity_id in computed and not math.isclose(computed[room.entity_id], stated, rel_tol=rel, abs_tol=1e-9):
            issues.append(Issue(f"room {room.entity_id}: stated {stated:g} m2 but the boundary gives "
                                f"{computed[room.entity_id]:.4g} m2 (relative tolerance {rel:g})", [room.entity_id],
                                detail={"stated": stated, "recomputed": computed[room.entity_id], "rel_tol": rel}))
    rows = ctx.model.schedules.get("room")
    if rows is not None:
        checked += 1
        by_code = {str(r.prop("code") or ""): r for r in rooms}
        for row in rows:
            if row.get("total"):  # a total line of the table is not a room
                continue
            code, area = str(row.get("code") or ""), _num(row.get("area"))
            room = by_code.get(code)
            if room is None:
                issues.append(Issue(f"room schedule row {code!r} has no room in the plan", []))
            elif area is not None and room.entity_id in computed and \
                    not math.isclose(computed[room.entity_id], area, rel_tol=rel, abs_tol=1e-9):
                issues.append(Issue(f"room schedule {code}: {area:g} m2 vs boundary {computed[room.entity_id]:.4g} m2",
                                    [room.entity_id]))
    total = _num(ctx.model.meta.get("total_area_m2"))
    if total is not None:
        checked += 1
        if len(computed) != len(rooms):
            issues.append(Issue("total area stated but not every room has a closed boundary to sum", []))
        elif not math.isclose(sum(computed.values()), total, rel_tol=rel, abs_tol=1e-9):
            issues.append(Issue(f"total area stated {total:g} m2 vs sum of boundaries {sum(computed.values()):.4g} m2", [],
                                detail={"stated": total, "recomputed": sum(computed.values())}))
    if not checked:
        return Outcome.skipped("no stated room areas, room schedule or total area to verify")
    return Outcome(issues, f"areas recomputed from boundaries for {len(computed)} of {len(rooms)} rooms "
                           f"(relative tolerance {rel:g})", checked)


def rule_room_overlap(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "closure_gap")
    if skip:
        return skip
    gap = tols["closure_gap"]
    rooms = ctx.model.of_type(EntityType.ROOM)
    polygons = [(r, _room_polygon(r, gap)) for r in rooms]
    usable = [(r, p) for r, p in polygons if p]
    if len(usable) < 1:
        return Outcome.skipped("no rooms with a closed boundary")
    issues: list[Issue] = []
    for r in rooms:
        if not r.level_id:
            issues.append(Issue(f"room {r.entity_id}: no level_id", [r.entity_id]))
    for i, (a, pa) in enumerate(usable):
        for b, pb in usable[i + 1:]:
            if (a.level_id or None) == (b.level_id or None) and geom2d.polygons_overlap(pa, pb, gap):  # type: ignore[arg-type]
                issues.append(Issue(f"rooms {a.entity_id} and {b.entity_id} overlap on level {a.level_id}",
                                    [a.entity_id, b.entity_id]))
    return Outcome(issues, f"{len(usable)} room boundaries compared pairwise per level", len(usable))


def rule_stairs(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "riser_total", "elevation")
    if skip:
        return skip
    stairs = ctx.model.of_type(EntityType.STAIR)
    if not stairs:
        return Outcome.skipped("no STAIR entities")
    levels = {e.entity_id: e for e in ctx.model.of_type(EntityType.LEVEL)}
    max_riser = _num(ctx.params.get("max_riser_height"))
    min_tread = _num(ctx.params.get("min_tread_depth"))
    issues: list[Issue] = []
    notes = []
    for s in stairs:
        total, count, riser = _num(s.prop("total_height")), _num(s.prop("riser_count")), _num(s.prop("riser_height"))
        if total is None or count is None or count < 1 or count != int(count):
            issues.append(Issue(f"stair {s.entity_id}: total_height and a whole riser_count are required to check the "
                                "arithmetic", [s.entity_id], kind="INCOMPLETE", severity=Severity.WARNING))
            continue
        if riser is not None and abs(count * riser - total) > tols["riser_total"]:
            issues.append(Issue(f"stair {s.entity_id}: {int(count)} risers x {riser:g} = {count * riser:g} "
                                f"but total height is {total:g}", [s.entity_id],
                                detail={"computed": count * riser, "total": total}))
        if riser is None:
            notes.append(f"{s.entity_id} riser height derived {total / count:g}")
        frm, to = levels.get(str(s.prop("from_level"))), levels.get(str(s.prop("to_level")))
        if s.prop("from_level") and s.prop("to_level"):
            if frm is None or to is None or frm.elevation is None or to.elevation is None:
                issues.append(Issue(f"stair {s.entity_id}: from/to level missing or without elevation", [s.entity_id]))
            elif abs((to.elevation - frm.elevation) - total) > tols["elevation"]:
                issues.append(Issue(f"stair {s.entity_id}: total height {total:g} vs level difference "
                                    f"{to.elevation - frm.elevation:g}", [s.entity_id, frm.entity_id, to.entity_id]))
        actual_riser = riser if riser is not None else total / count
        if frm is not None and to is not None and frm.elevation is not None and to.elevation is not None:
            by_levels = (to.elevation - frm.elevation) / count  # recomputed from the levels, not from the stated riser
            if riser is not None and abs(by_levels - riser) * count > tols["riser_total"]:
                issues.append(Issue(f"stair {s.entity_id}: stated riser {riser:g} but the level difference over "
                                    f"{int(count)} risers is {by_levels:g}", [s.entity_id], kind="CONFLICT"))
        flights = s.prop("flights")
        if isinstance(flights, list) and flights:
            risers_sum = sum(int(f.get("risers", 0)) for f in flights)
            if risers_sum != int(count):
                issues.append(Issue(f"stair {s.entity_id}: the flights have {risers_sum} risers but the stair says "
                                    f"{int(count)}", [s.entity_id], kind="CONFLICT"))
            for i, f in enumerate(flights, 1):
                if int(f.get("treads", -1)) != int(f.get("risers", 0)) - 1:
                    issues.append(Issue(f"stair {s.entity_id}: flight {i} has {f.get('risers')} risers and "
                                        f"{f.get('treads')} treads (a flight has one tread fewer than risers)", [s.entity_id]))
        applied = (s.prop("limits") or {}).get("applied", {}) if isinstance(s.prop("limits"), Mapping) else {}
        v_max = _num((applied.get("max_riser_height") or {}).get("value")) if applied else None
        v_tread = _num((applied.get("min_tread_depth") or {}).get("value")) if applied else None
        v_width = _num((applied.get("min_stair_width") or {}).get("value")) if applied else None
        for limit, label in ((max_riser, "supplied"), (v_max, "verified-profile")):
            if limit is not None and actual_riser > limit + 1e-9:
                issues.append(Issue(f"stair {s.entity_id}: riser {actual_riser:g} exceeds the {label} limit {limit:g}",
                                    [s.entity_id]))
        tread = _num(s.prop("tread_depth"))
        for limit, label in ((min_tread, "supplied"), (v_tread, "verified-profile")):
            if limit is not None and tread is not None and tread < limit - 1e-9:
                issues.append(Issue(f"stair {s.entity_id}: tread {tread:g} below the {label} limit {limit:g}",
                                    [s.entity_id]))
        width = _num(s.prop("width"))
        if v_width is not None and width is not None and width < v_width - 1e-9:
            issues.append(Issue(f"stair {s.entity_id}: width {width:g} below the verified-profile limit {v_width:g}",
                                [s.entity_id]))
        assumed = sorted(k for k, how in (s.prop("setting_status") or {}).items() if how == "ASSUMED") \
            if isinstance(s.prop("setting_status"), Mapping) else []
        if assumed:
            issues.append(Issue(f"stair {s.entity_id}: {', '.join(assumed)} are ASSUMED working defaults, not code limits; "
                                f"{'verified-profile limits applied: ' + ', '.join(sorted(applied)) if applied else 'no verified limit was applied'}",
                                [s.entity_id], severity=Severity.INFO, kind="ASSUMED"))
    limits = "limits applied: " + (", ".join(k for k in ("max_riser_height", "min_tread_depth")
                                             if ctx.params.get(k) is not None) or "none (no verified profile limit given)")
    return Outcome(issues, f"{len(stairs)} stairs: riser arithmetic and level heights; {limits}"
                           + (f"; {'; '.join(notes)}" if notes else ""), len(stairs))


def rule_levels(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "elevation")
    if skip:
        return skip
    tol = tols["elevation"]
    levels = ctx.model.of_type(EntityType.LEVEL)
    if not levels:
        return Outcome.skipped("no LEVEL entities")
    issues: list[Issue] = []
    by_id = {l.entity_id: l for l in levels}
    datums = {str(l.prop("datum")) for l in levels if l.prop("datum")}
    if len(datums) > 1:
        issues.append(Issue(f"levels use different datums {sorted(datums)}", _ids(levels)))
    elif not datums and not ctx.model.meta.get("datum"):
        issues.append(Issue("no elevation datum declared (e.g. +-0.000 convention)", _ids(levels),
                            severity=Severity.WARNING, kind="INCOMPLETE"))
    with_elev = sorted((l for l in levels if l.elevation is not None), key=lambda l: l.elevation)  # type: ignore[arg-type,return-value]
    for l in levels:
        if l.elevation is None:
            issues.append(Issue(f"level {l.entity_id}: elevation unknown", [l.entity_id], kind="INCOMPLETE"))
    for a, b in zip(with_elev, with_elev[1:]):
        if abs(b.elevation - a.elevation) <= tol:  # type: ignore[operator]
            issues.append(Issue(f"levels {a.entity_id} and {b.entity_id} have the same elevation {a.elevation:g}",
                                [a.entity_id, b.entity_id]))
    for e in ctx.model.entities:
        if e.level_id is None or e.entity_type is EntityType.LEVEL:
            continue
        level = by_id.get(e.level_id)
        if level is None:
            issues.append(Issue(f"{e.entity_id}: level_id {e.level_id!r} does not exist", [e.entity_id]))
        elif e.entity_type in (EntityType.ROOM, EntityType.SLAB) and e.elevation is not None \
                and level.elevation is not None and abs(e.elevation - level.elevation) > tol:
            issues.append(Issue(f"{e.entity_type.value.lower()} {e.entity_id}: elevation {e.elevation:g} differs from "
                                f"level {level.entity_id} ({level.elevation:g}); finish thickness must be explicit",
                                [e.entity_id, level.entity_id], severity=Severity.WARNING))
    return Outcome(issues, f"{len(levels)} levels and level references checked (elevation tolerance {tol:g})",
                   len(levels))


def rule_view_consistency(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "schedule_dimension")
    if skip:
        return skip
    tol = tols["schedule_dimension"]
    checked = 0
    issues: list[Issue] = []
    for e in ctx.model.entities:
        views = e.properties.get("views")
        if not isinstance(views, Mapping) or not views:
            continue
        checked += 1
        for key in ("width", "height", "sill_height", "head_height", "elevation"):
            values = {name: _num(v.get(key)) for name, v in views.items() if isinstance(v, Mapping) and key in v}
            primary = _num(e.prop(key))
            if primary is not None and "plan" not in values:
                values["model"] = primary
            numbers = [v for v in values.values() if v is not None]
            if len(numbers) > 1 and max(numbers) - min(numbers) > tol:
                issues.append(Issue(f"{e.entity_id}: {key} disagrees between views {values}", [e.entity_id],
                                    kind="CONFLICT", detail={"values": values, "tolerance": tol}))
    if not checked:
        return Outcome.skipped("no entity carries per-view values (properties.views)")
    return Outcome(issues, f"{checked} entities compared across plan/elevation/section values", checked)


# --------------------------------------------------------------------------- cross-view coordination (spec 9, 10)

def _view_records(ctx: RuleContext, kind: str) -> list[Mapping[str, Any]] | None:
    """The view records of `meta.views` of one kind; None when the model carries no view list at all."""
    views = ctx.model.meta.get("views")
    if not isinstance(views, list):
        return None
    return [v for v in views if isinstance(v, Mapping) and v.get("kind") == kind]


def _opening_key_issues(names: tuple[str, str], code: str, expected: Mapping[str, Any], actual: Mapping[str, Any],
                        tol: float, ids: list[str]) -> list[Issue]:
    issues = []
    for key in ("width", "height", "sill_height"):
        a, b = _num(expected.get(key)), _num(actual.get(key))
        if a is not None and b is not None and abs(a - b) > tol:
            issues.append(Issue(f"{code}: {key} {a:g} in {names[0]} vs {b:g} in {names[1]}", ids, kind="CONFLICT",
                                detail={"code": code, "key": key, names[0]: a, names[1]: b, "tolerance": tol}))
    return issues


def rule_elevation_matches_plan(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "schedule_dimension")
    if skip:
        return skip
    tol = tols["schedule_dimension"]
    elevations = _view_records(ctx, "elevation")
    if not elevations:
        return Outcome.skipped("no elevation view records (meta.views kind 'elevation')")
    plan_items = ctx.model.of_type(EntityType.DOOR, EntityType.WINDOW)
    if not plan_items:
        return Outcome.skipped("no DOOR/WINDOW entities in the plan to compare with")
    by_code = {str(e.prop("code") or ""): e for e in plan_items}
    issues: list[Issue] = []
    checked = 0
    for view in elevations:
        side = view.get("side")
        seen: set[str] = set()
        for o in view.get("openings") or []:
            checked += 1
            code = str(o.get("code") or "")
            seen.add(code)
            e = by_code.get(code)
            if e is None:
                issues.append(Issue(f"elevation {view.get('id')}: opening {code!r} has no door/window with that code "
                                    "in the plan", []))
                continue
            if o.get("type") and o["type"] != e.entity_type.value:
                issues.append(Issue(f"{code}: {o['type']} in the elevation but {e.entity_type.value} in the plan",
                                    [e.entity_id]))
            plan_facade = e.prop("facade")
            if plan_facade and side and plan_facade != side:
                issues.append(Issue(f"{code}: drawn on the {side} elevation but the plan puts it on the {plan_facade} "
                                    "facade", [e.entity_id]))
            plan_values = {k: e.prop(k) for k in ("width", "height", "sill_height")}
            issues += _opening_key_issues(("plan", f"elevation {side}"), code, plan_values, o, tol, [e.entity_id])
        for e in plan_items:
            if e.prop("facade") == side and str(e.prop("code") or "") not in seen:
                issues.append(Issue(f"{e.prop('code')}: on the {side} facade in the plan but missing from the "
                                    f"{side} elevation", [e.entity_id]))
    return Outcome(issues, f"{checked} elevation openings compared with the plan symbols (tolerance {tol:g})", checked)


def _schedule_rows(ctx: RuleContext, kind: str) -> tuple[list[Mapping[str, Any]] | None, str]:
    drawn = [rec for rec in (_view_records(ctx, "schedule") or []) if rec.get("schedule") == kind and rec.get("rows") is not None]
    if drawn:
        return [row for rec in drawn for row in rec["rows"]], "drawn table" if len(drawn) == 1 else f"{len(drawn)} drawn tables"
    rows = ctx.model.schedules.get(kind)
    return (list(rows), "schedule data") if rows is not None else (None, "")


def rule_schedule_matches_elevation(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "schedule_dimension")
    if skip:
        return skip
    tol = tols["schedule_dimension"]
    elevations = _view_records(ctx, "elevation")
    if not elevations:
        return Outcome.skipped("no elevation view records (meta.views kind 'elevation')")
    tables = {"DOOR": _schedule_rows(ctx, "door"), "WINDOW": _schedule_rows(ctx, "window")}
    if all(rows is None for rows, _ in tables.values()):
        return Outcome.skipped("no door/window schedule (drawn table or schedule data)")
    issues: list[Issue] = []
    checked = 0
    drawn_sides = {v.get("side") for v in elevations}
    by_side = {v.get("side"): {str(o.get("code")) for o in v.get("openings") or []} for v in elevations}
    for view in elevations:
        for o in view.get("openings") or []:
            rows, origin = tables.get(str(o.get("type")), (None, ""))
            if rows is None:
                continue
            checked += 1
            code = str(o.get("code") or "")
            row = next((r for r in rows if str(r.get("code") or "") == code), None)
            if row is None:
                issues.append(Issue(f"{code}: drawn on the {view.get('side')} elevation but missing from the "
                                    f"{str(o.get('type')).lower()} schedule ({origin})", []))
                continue
            issues += _opening_key_issues((f"{str(o.get('type')).lower()} schedule", f"elevation {view.get('side')}"),
                                          code, row, o, tol, [])
    for kind, (rows, origin) in tables.items():
        for row in rows or []:
            side = row.get("facade")
            if side in drawn_sides and str(row.get("code")) not in by_side[side]:
                issues.append(Issue(f"{row.get('code')}: the {kind.lower()} schedule puts it on the {side} facade but the "
                                    f"{side} elevation does not show it", []))
    return Outcome(issues, f"{checked} elevation openings compared with the schedules (tolerance {tol:g})", checked)


def rule_levels_elevation_section(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "elevation")
    if skip:
        return skip
    tol = tols["elevation"]
    views = [v for v in (ctx.model.meta.get("views") or []) if isinstance(v, Mapping) and v.get("levels")]
    if not views:
        return Outcome.skipped("no elevation or section view carries level marks (meta.views[].levels)")
    levels = {l.entity_id: l for l in ctx.model.of_type(EntityType.LEVEL)}
    if not levels:
        return Outcome.skipped("no LEVEL entities to compare the marks with")
    stairs = {s.entity_id: s for s in ctx.model.of_type(EntityType.STAIR)}
    issues: list[Issue] = []
    checked = 0
    seen: dict[str, tuple[str, float]] = {}
    for view in views:
        for mark in view["levels"]:
            checked += 1
            lid, z = str(mark.get("level_id")), _num(mark.get("elevation"))
            level = levels.get(lid)
            if level is None or level.elevation is None or z is None:
                issues.append(Issue(f"{view.get('id')}: level mark {lid!r} has no matching LEVEL with an elevation", []))
                continue
            if abs(z - level.elevation) > tol:
                issues.append(Issue(f"{view.get('id')}: {lid} is drawn at {z:g} but the level is {level.elevation:g}",
                                    [lid], kind="CONFLICT", detail={"drawn": z, "level": level.elevation, "tolerance": tol}))
            label = _num(mark.get("label_value"))
            if label is None:
                issues.append(Issue(f"{view.get('id')}: level {lid} has no readable label text", [lid]))
            elif abs(label - z) > max(tol, 0.0005):
                issues.append(Issue(f"{view.get('id')}: level {lid} label reads {label:g} but is drawn at {z:g}", [lid],
                                    kind="CONFLICT"))
            if lid in seen and abs(seen[lid][1] - z) > tol:
                issues.append(Issue(f"level {lid} is at {seen[lid][1]:g} in {seen[lid][0]} but {z:g} in {view.get('id')}",
                                    [lid], kind="CONFLICT"))
            seen.setdefault(lid, (str(view.get("id")), z))
        for ch in view.get("clear_heights") or []:
            checked += 1
            frm, to = levels.get(str(ch.get("from_level"))), levels.get(str(ch.get("to_level")))
            h = _num(ch.get("height"))
            if frm is None or to is None or frm.elevation is None or to.elevation is None or h is None:
                issues.append(Issue(f"{view.get('id')}: clear height of {ch.get('room_id')} has no valid levels", []))
            elif abs(h - (to.elevation - frm.elevation)) > tol:
                issues.append(Issue(f"{view.get('id')}: clear height {h:g} of {ch.get('room_id')} vs level difference "
                                    f"{to.elevation - frm.elevation:g}", [frm.entity_id, to.entity_id], kind="CONFLICT"))
        for st in view.get("stairs") or []:
            checked += 1
            entity = stairs.get(str(st.get("id")))
            drawn = _num(st.get("total_height"))
            if entity is None:
                issues.append(Issue(f"{view.get('id')}: stair {st.get('id')} is not in the model", []))
                continue
            total = _num(entity.prop("total_height"))
            if drawn is not None and total is not None and abs(drawn - total) > tol:
                issues.append(Issue(f"stair {entity.entity_id}: {drawn:g} high in {view.get('id')} vs {total:g} in the model",
                                    [entity.entity_id], kind="CONFLICT"))
            if st.get("riser_count") is not None and entity.prop("riser_count") is not None \
                    and int(st["riser_count"]) != int(entity.prop("riser_count")):
                issues.append(Issue(f"stair {entity.entity_id}: {st['riser_count']} risers drawn vs "
                                    f"{entity.prop('riser_count')} in the model", [entity.entity_id], kind="CONFLICT"))
    return Outcome(issues, f"{checked} level marks / clear heights / stairs in {len(views)} views compared with the "
                           f"LEVEL entities (tolerance {tol:g})", checked)


def rule_section_marker_view(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "opening_offset")
    if skip:
        return skip
    tol = tols["opening_offset"]
    markers = [e for e in ctx.model.entities if e.prop("section_code")]
    sections = _view_records(ctx, "section")
    if not markers and not sections:
        return Outcome.skipped("no section markers and no section views")
    if sections is None:
        return Outcome.skipped("no view list (meta.views) to resolve the markers against")
    by_id = {str(v.get("id")): v for v in sections}
    sheets = {str(s) for s in (ctx.model.meta.get("sheets") or [])}
    issues: list[Issue] = []
    for m in markers:
        target = str(m.prop("target_view") or "")
        view = by_id.get(target)
        if view is None:
            issues.append(Issue(f"marker {m.entity_id}: target view {target!r} does not exist (views: {sorted(by_id)})",
                                [m.entity_id]))
            continue
        if view.get("section_code") != m.prop("section_code"):
            issues.append(Issue(f"marker {m.entity_id}: code {m.prop('section_code')!r} but the view is "
                                f"{view.get('section_code')!r}", [m.entity_id]))
        cut = view.get("cut") or {}
        pos_view, pos_marker = _num(cut.get("position")), _num(m.prop("position"))
        if cut.get("axis") != m.prop("axis") or cut.get("direction") != m.prop("direction") or \
                pos_view is None or pos_marker is None or abs(pos_view - pos_marker) > tol:
            issues.append(Issue(f"marker {m.entity_id}: drawn at {m.prop('axis')}={m.prop('position')} looking "
                                f"{m.prop('direction')} but the section view was cut at {cut.get('axis')}="
                                f"{cut.get('position')} looking {cut.get('direction')}", [m.entity_id], kind="CONFLICT"))
        if view.get("sheet") and m.prop("target_sheet") != view.get("sheet"):
            issues.append(Issue(f"marker {m.entity_id}: points to sheet {m.prop('target_sheet')!r} but the view is on "
                                f"{view.get('sheet')!r}", [m.entity_id]))
        if sheets and m.prop("target_sheet") and str(m.prop("target_sheet")) not in sheets:
            issues.append(Issue(f"marker {m.entity_id}: sheet {m.prop('target_sheet')!r} is not in the sheet list", [m.entity_id]))
    marked = {str(m.prop("target_view")) for m in markers}
    for vid in sorted(set(by_id) - marked):
        issues.append(Issue(f"section view {vid} has no marker on the plan", [], severity=Severity.WARNING))
    return Outcome(issues, f"{len(markers)} markers resolved against {len(by_id)} section views", len(markers) + len(by_id))


def rule_room_table_area(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "closure_gap")
    if skip:
        return skip
    unit_mm = ctx.model.unit_mm
    rel = ctx.tol.relative("area_rel")
    rows, origin = _schedule_rows(ctx, "room")
    if rows is None:
        return Outcome.skipped("no room/area table (drawn table or schedule data)")
    rooms = {r.entity_id: r for r in ctx.model.of_type(EntityType.ROOM)}
    polygons = {rid: _room_polygon(r, tols["closure_gap"]) for rid, r in rooms.items()}
    if not any(polygons.values()):
        return Outcome.skipped("no room with a closed boundary to recompute an area from")
    issues: list[Issue] = []
    listed: set[str] = set()
    recomputed_total = 0.0
    recomputed_by_level: dict[Any, float] = {}
    listed_by_level: dict[Any, int] = {}
    checked = 0
    abs_tol = 0.0051  # the table is printed to 0.01 m2
    for row in rows:
        if row.get("total"):
            continue
        code = str(row.get("code") or "")
        area = _num(row.get("area"))
        checked += 1
        listed.add(code)
        room, poly = rooms.get(code), polygons.get(code)
        if room is None:
            issues.append(Issue(f"area table row {code!r} has no room in the model", []))
        elif poly is None:
            issues.append(Issue(f"area table row {code!r}: room has no closed boundary to recompute the area", [code]))
        elif area is None:
            issues.append(Issue(f"area table row {code!r}: area {row.get('area')!r} is not a number", [code]))
        else:
            value = _area_m2(poly, unit_mm)  # type: ignore[arg-type]
            recomputed_total += value
            recomputed_by_level[row.get("level_id")] = recomputed_by_level.get(row.get("level_id"), 0.0) + value
            listed_by_level[row.get("level_id")] = listed_by_level.get(row.get("level_id"), 0) + 1
            if not math.isclose(value, area, rel_tol=rel, abs_tol=abs_tol):
                issues.append(Issue(f"area table {code}: {area:g} m2 but the boundary gives {value:.4g} m2 (relative "
                                    f"tolerance {rel:g}, table printed to 0.01)", [code], kind="CONFLICT",
                                    detail={"table": area, "recomputed": value}))
    per_table = len({r.get("level_id") for r in rows if r.get("level_id") is not None}) > 1
    for row in rows:
        if row.get("total"):
            total = _num(row.get("area"))
            level = row.get("level_id")
            expected_total = recomputed_by_level.get(level, 0.0) if per_table else recomputed_total
            count = listed_by_level.get(level, 0) if per_table else len(listed)
            if total is not None and not math.isclose(expected_total, total, rel_tol=rel,
                                                       abs_tol=abs_tol * max(1, count)):
                where = f" of {level}" if per_table else ""
                issues.append(Issue(f"area table total{where} {total:g} m2 vs the sum of the listed boundaries "
                                    f"{expected_total:.4g} m2", [], kind="CONFLICT"))
    for rid, room in rooms.items():
        if room.prop("listed") is True and rid not in listed:
            issues.append(Issue(f"room {rid} ({room.prop('name')}) is missing from the area table", [rid]))
    return Outcome(issues, f"{checked} table rows recomputed from room boundaries ({origin}, relative tolerance {rel:g})",
                   checked)


# --------------------------------------------------------------------------- PLOT rules

def rule_layouts(ctx: RuleContext) -> Outcome:
    layouts = ctx.model.meta.get("layouts")
    if layouts is None:
        return Outcome.skipped("layout list (meta.layouts) not provided")
    papers = profile_value(ctx.model.profile, "paper_size")
    allowed = {str(p).upper() for p in (papers if isinstance(papers, list) else [papers])} if papers else set()
    issues = []
    for lay in layouts:
        name = lay.get("name")
        if not lay.get("paper"):
            issues.append(Issue(f"layout {name!r}: paper size not set"))
        elif allowed and str(lay["paper"]).upper() not in allowed:
            issues.append(Issue(f"layout {name!r}: paper {lay['paper']} is not in the profile {sorted(allowed)}"))
        if not lay.get("scale"):
            issues.append(Issue(f"layout {name!r}: viewport scale not set"))
        if lay.get("viewport_locked") is False:
            issues.append(Issue(f"layout {name!r}: viewport not locked", severity=Severity.WARNING))
        if not lay.get("title_block"):
            issues.append(Issue(f"layout {name!r}: no title block", severity=Severity.WARNING))
    return Outcome(issues, f"{len(layouts)} layouts checked", max(1, len(layouts)))


def rule_pdf_rendered(ctx: RuleContext) -> Outcome:
    state = ctx.model.meta.get("pdf_render_check")
    if not isinstance(state, Mapping):
        return Outcome.skipped("the PDF was not rendered to an image and inspected")
    if not state.get("ok"):
        return Outcome([Issue(f"rendered PDF check failed: {state.get('note', 'no detail')}")],
                       "rendered PDF has problems", 1)
    return Outcome(evidence=f"PDF rendered and inspected ({state.get('note', 'no note')})", checked=1)


# --------------------------------------------------------------------------- multi-storey, stairs, sheets, plot (spec 8-13, 18)

def _rect_inside(rect: Sequence[float], outer: Sequence[float], tol: float) -> bool:
    return (rect[0] >= outer[0] - tol and rect[1] >= outer[1] - tol and rect[2] <= outer[2] + tol
            and rect[3] <= outer[3] + tol)


def _rect_area(rect: Sequence[float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _same_rects(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]], tol: float) -> bool:
    if len(a) != len(b):
        return False
    left = sorted(tuple(r) for r in a)
    right = sorted(tuple(r) for r in b)
    return all(all(abs(x - y) <= tol for x, y in zip(p, q)) for p, q in zip(left, right))


def rule_stair_opening(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "opening_offset", "elevation")
    if skip:
        return skip
    stairs = [s for s in ctx.model.of_type(EntityType.STAIR) if s.prop("to_level")]
    if not stairs:
        return Outcome.skipped("no STAIR entities")
    tol = tols["opening_offset"]
    slabs = {(s.level_id, str(s.prop("role"))): s for s in ctx.model.of_type(EntityType.SLAB)}
    if not slabs:
        return Outcome.skipped("no SLAB entities in the model: floor slabs are not modelled, so no opening can be checked")
    plans = [v for v in (_view_records(ctx, "plan") or []) if v.get("slab_openings") is not None and v.get("stairs") is not None]
    issues: list[Issue] = []
    for st in stairs:
        slab = slabs.get((str(st.prop("to_level")), "floor"))
        foot = st.prop("footprint")
        if slab is None:
            issues.append(Issue(f"stair {st.entity_id}: there is no floor slab on {st.prop('to_level')} to open", [st.entity_id]))
            continue
        opening = slab.prop("stair_opening") or []
        if not opening:
            issues.append(Issue(f"stair {st.entity_id}: slab {slab.entity_id} on {st.prop('to_level')} has no stair opening",
                                [st.entity_id, slab.entity_id]))
            continue
        if not isinstance(foot, (list, tuple)) or len(foot) != 4:
            issues.append(Issue(f"stair {st.entity_id}: no footprint to compare the slab opening with", [st.entity_id],
                                kind="INCOMPLETE", severity=Severity.WARNING))
            continue
        outside = [r for r in opening if not _rect_inside(r, foot, tol)]
        if outside:
            issues.append(Issue(f"stair {st.entity_id}: slab opening {outside[0]} lies outside the stair footprint {list(foot)}",
                                [st.entity_id, slab.entity_id], kind="CONFLICT"))
        covered = sum(_rect_area(r) for r in opening)
        ratio = covered / _rect_area(foot) if _rect_area(foot) > 0 else 0.0
        if ratio < 0.4:
            issues.append(Issue(f"stair {st.entity_id}: the slab opening covers only {ratio:.0%} of the stair footprint "
                                "(the arrival flight cannot pass the slab)", [st.entity_id, slab.entity_id]))
        model_open = st.prop("slab_opening")
        if model_open and not _same_rects(model_open, opening, tol):
            issues.append(Issue(f"stair {st.entity_id}: slab {slab.entity_id} opening differs from the stair's own opening",
                                [st.entity_id, slab.entity_id], kind="CONFLICT"))
        # drawn evidence on the plan of the upper floor
        for plan in plans:
            if plan.get("level_id") != st.prop("to_level"):
                continue
            drawn = [r for item in plan["slab_openings"] if item.get("stair_id") == st.entity_id for r in item.get("rects", [])]
            if not drawn:
                issues.append(Issue(f"stair {st.entity_id}: the plan of {st.prop('to_level')} ({plan.get('id')}) shows no "
                                    "slab opening above it", [st.entity_id]))
            elif not _same_rects(drawn, opening, tol):
                issues.append(Issue(f"stair {st.entity_id}: the opening drawn on {plan.get('id')} differs from the slab "
                                    f"opening of the model ({drawn} vs {opening})", [st.entity_id, slab.entity_id],
                                    kind="CONFLICT"))
    return Outcome(issues, f"{len(stairs)} stairs: opening present in the slab above, inside the footprint, "
                           f"{len(plans)} drawn plan(s) compared (tolerance {tol:g})", len(stairs))


def rule_stair_plan(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "opening_offset")
    if skip:
        return skip
    plans = [v for v in (_view_records(ctx, "plan") or []) if v.get("stairs") is not None]
    if not plans:
        return Outcome.skipped("no drawn plan view carries stair symbols (meta.views kind 'plan' with 'stairs')")
    stairs = {s.entity_id: s for s in ctx.model.of_type(EntityType.STAIR)}
    if not stairs:
        return Outcome.skipped("no STAIR entities in the model")
    tol = tols["opening_offset"]
    issues: list[Issue] = []
    checked = 0
    for plan in plans:
        lid = plan.get("level_id")
        for item in plan["stairs"]:
            sid = str(item.get("id"))
            entity = stairs.get(sid)
            if entity is None:
                issues.append(Issue(f"{plan.get('id')}: draws a stair {sid!r} that is not in the model", []))
                continue
            checked += 1
            lines = item.get("riser_lines")
            if lines is not None and entity.prop("riser_count") is not None and int(lines) != int(entity.prop("riser_count")):
                issues.append(Issue(f"{plan.get('id')}: stair {sid} has {lines} riser lines but the model has "
                                    f"{entity.prop('riser_count')} risers", [sid], kind="CONFLICT"))
            foot, mfoot = item.get("footprint"), entity.prop("footprint")
            if foot and mfoot and any(abs(a - b) > tol for a, b in zip(foot, mfoot)):
                issues.append(Issue(f"{plan.get('id')}: stair {sid} footprint {foot} differs from the model {list(mfoot)}",
                                    [sid], kind="CONFLICT"))
        for sid, entity in stairs.items():
            if entity.prop("from_level") == lid and not any(str(i.get("id")) == sid for i in plan["stairs"]):
                issues.append(Issue(f"{plan.get('id')}: stair {sid} starts on {lid} but is not drawn on its plan", [sid]))
    return Outcome(issues, f"{checked} stair symbols on {len(plans)} plans compared with the model", max(checked, 1))


def rule_floor_levels(ctx: RuleContext) -> Outcome:
    tols, skip = _need_tol(ctx, "elevation")
    if skip:
        return skip
    floors = ctx.model.meta.get("floors")
    if not isinstance(floors, list) or not floors:
        return Outcome.skipped("no floor list (meta.floors)")
    tol = tols["elevation"]
    levels = {l.entity_id: l for l in ctx.model.of_type(EntityType.LEVEL)}
    if not levels:
        return Outcome.skipped("no LEVEL entities")
    slabs = {s.level_id: s for s in ctx.model.of_type(EntityType.SLAB) if s.prop("role") == "floor"}
    issues: list[Issue] = []
    checked = 0
    prev_ceiling: Entity | None = None
    for f in floors:
        checked += 1
        lid, cid = str(f.get("level_id")), str(f.get("ceiling_id"))
        floor, ceiling = levels.get(lid), levels.get(cid)
        if floor is None or ceiling is None or floor.elevation is None or ceiling.elevation is None:
            issues.append(Issue(f"floor {lid}: floor or ceiling level missing or without elevation", [lid, cid]))
            continue
        clear = _num(f.get("clear_height"))
        if clear is not None and abs((ceiling.elevation - floor.elevation) - clear) > tol:
            issues.append(Issue(f"floor {lid}: clear height {clear:g} but the levels give {ceiling.elevation - floor.elevation:g}",
                                [lid, cid], kind="CONFLICT"))
        if ceiling.elevation <= floor.elevation + tol:
            issues.append(Issue(f"floor {lid}: ceiling {ceiling.elevation:g} is not above the floor {floor.elevation:g}", [lid, cid]))
        if prev_ceiling is not None:
            slab = slabs.get(lid)
            thick = _num(slab.prop("thickness")) if slab is not None else None
            gap = floor.elevation - prev_ceiling.elevation  # type: ignore[operator]
            if thick is None:
                issues.append(Issue(f"floor {lid}: no slab with a thickness between {prev_ceiling.entity_id} and the floor",
                                    [lid], severity=Severity.WARNING, kind="INCOMPLETE"))
            elif abs(gap - thick) > tol:
                issues.append(Issue(f"floor {lid}: {gap:g} m between the ceiling below and this floor but the slab is "
                                    f"{thick:g} m thick", [lid, prev_ceiling.entity_id], kind="CONFLICT"))
        prev_ceiling = ceiling
        owned = [e for e in ctx.model.entities if e.level_id == lid]
        types = {e.entity_type for e in owned}
        for needed in (EntityType.WALL, EntityType.ROOM):
            if needed not in types:
                issues.append(Issue(f"floor {lid}: no {needed.value} entity belongs to this floor", [lid]))
        for e in owned:
            if e.entity_type in (EntityType.DOOR, EntityType.WINDOW):
                head = _num(e.prop("head_height"))
                if head is not None and head >= ceiling.elevation - floor.elevation - 1e-9:
                    issues.append(Issue(f"{e.entity_id}: head {head:g} reaches the ceiling of {lid}", [e.entity_id]))
    top = levels.get(str(floors[-1].get("ceiling_id")))
    roof = levels.get("LV-ROOF")
    if top is not None and roof is not None and top.elevation is not None and roof.elevation is not None \
            and roof.elevation <= top.elevation + tol:
        issues.append(Issue(f"roof level {roof.elevation:g} is not above the top ceiling {top.elevation:g}", ["LV-ROOF"]))
    if len(floors) > 1 and not ctx.model.of_type(EntityType.STAIR):
        issues.append(Issue(f"{len(floors)} floors but no stair connects them", []))
    # every elevation and section must mark every floor level
    wanted = [str(f.get("level_id")) for f in floors]
    for v in ctx.model.meta.get("views") or []:
        if isinstance(v, Mapping) and v.get("kind") in ("elevation", "section") and v.get("levels"):
            checked += 1
            seen = {str(m.get("level_id")) for m in v["levels"]}
            lacking = [w for w in wanted if w not in seen]
            if lacking:
                issues.append(Issue(f"{v.get('id')}: does not mark the floor level(s) {lacking}", lacking))
            if v.get("kind") == "section" and v.get("floors") not in (None, len(floors)):
                issues.append(Issue(f"{v.get('id')}: section drawn for {v.get('floors')} floors, model has {len(floors)}", []))
    views = ctx.model.meta.get("views")
    if len(floors) > 1 and isinstance(views, list):
        drawn_plans = [v for v in views if isinstance(v, Mapping) and v.get("kind") == "plan" and v.get("stairs") is not None]
        if drawn_plans:
            for w in wanted:
                if not any(v.get("level_id") == w for v in drawn_plans):
                    issues.append(Issue(f"no plan was drawn for floor {w}", [w], severity=Severity.WARNING))
        if drawn_plans and not any(isinstance(v, Mapping) and v.get("kind") == "roof_plan" for v in views):
            issues.append(Issue("no roof plan was drawn", [], severity=Severity.WARNING))
    return Outcome(issues, f"{len(floors)} floors: heights, slab gaps, ownership, level marks in the views (tolerance {tol:g})",
                   checked)


def _sheet_index(ctx: RuleContext) -> list[Mapping[str, Any]] | None:
    raw = ctx.model.meta.get("sheet_index")
    return [s for s in raw if isinstance(s, Mapping)] if isinstance(raw, list) else None


def _layout_of(ctx: RuleContext, sheet: Mapping[str, Any]) -> Mapping[str, Any] | None:
    want = str(sheet.get("layout") or sheet.get("code") or "").casefold()
    for lay in ctx.model.meta.get("layouts") or []:
        if isinstance(lay, Mapping) and str(lay.get("name", "")).casefold() == want:
            return lay
    return None


def _our_viewports(layout: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    vps = [v for v in layout.get("viewports") or [] if isinstance(v, Mapping)]
    marked = [v for v in vps if v.get("role") == "viewport" or v.get("marked")]
    return marked or [v for v in vps if v.get("scale_custom")]


def rule_sheet_layouts(ctx: RuleContext) -> Outcome:
    index = _sheet_index(ctx)
    if index is None or ctx.model.meta.get("layouts") is None:
        return Outcome.skipped("sheet index (meta.sheet_index) or measured layouts (meta.layouts) not provided")
    issues: list[Issue] = []
    names = {str(l.get("name", "")).casefold(): l for l in ctx.model.meta["layouts"] if isinstance(l, Mapping)}
    codes = [str(s.get("code")) for s in index]
    for code in sorted({c for c in codes if codes.count(c) > 1}):
        issues.append(Issue(f"sheet code {code} appears more than once in the sheet index", [code]))
    for s in index:
        if _layout_of(ctx, s) is None:
            issues.append(Issue(f"sheet {s.get('code')} ({s.get('title')}) has no layout named {s.get('layout') or s.get('code')!r}",
                                [str(s.get("code"))]))
    listed = {str(s.get("layout") or s.get("code")).casefold() for s in index}
    for n, lay in names.items():
        if n not in listed:
            issues.append(Issue(f"layout {lay.get('name')!r} is not in the sheet index", [], severity=Severity.INFO))
    return Outcome(issues, f"{len(index)} sheets of the index resolved against {len(names)} layouts", len(index))


def rule_viewport_scale(ctx: RuleContext) -> Outcome:
    index = _sheet_index(ctx)
    if index is None or ctx.model.meta.get("layouts") is None:
        return Outcome.skipped("sheet index (meta.sheet_index) or measured layouts (meta.layouts) not provided")
    rel = ctx.tol.relative("scale_rel")
    issues: list[Issue] = []
    checked = 0
    for s in index:
        lay = _layout_of(ctx, s)
        declared = _num(s.get("scale_denominator"))
        if lay is None or declared is None:
            continue
        vps = _our_viewports(lay)
        checked += 1
        if not vps:
            issues.append(Issue(f"sheet {s.get('code')}: the layout has no viewport", [str(s.get("code"))]))
            continue
        for vp in vps:
            measured = _num(vp.get("scale_denominator"))
            if measured is None:
                issues.append(Issue(f"sheet {s.get('code')}: viewport scale could not be read", [str(s.get("code"))],
                                    kind="INCOMPLETE"))
            elif abs(measured - declared) > rel * declared:
                issues.append(Issue(f"sheet {s.get('code')}: viewport scale is 1:{measured:g} but the sheet declares "
                                    f"1:{declared:g}", [str(s.get("code"))], kind="CONFLICT",
                                    detail={"measured": measured, "declared": declared}))
    if not checked:
        return Outcome.skipped("no sheet with both a layout and a declared scale")
    return Outcome(issues, f"{checked} sheets: viewport scale equals the declared scale (relative tolerance {rel:g})", checked)


def rule_viewport_locked(ctx: RuleContext) -> Outcome:
    index = _sheet_index(ctx)
    if index is None or ctx.model.meta.get("layouts") is None:
        return Outcome.skipped("sheet index (meta.sheet_index) or measured layouts (meta.layouts) not provided")
    issues: list[Issue] = []
    checked = 0
    for s in index:
        lay = _layout_of(ctx, s)
        if lay is None:
            continue
        for vp in _our_viewports(lay):
            checked += 1
            if vp.get("locked") is not True:
                issues.append(Issue(f"sheet {s.get('code')}: viewport is not locked (DisplayLocked={vp.get('locked')!r})",
                                    [str(s.get("code"))]))
    if not checked:
        return Outcome.skipped("no viewport to check")
    return Outcome(issues, f"{checked} viewports checked for the lock", checked)


def rule_title_block(ctx: RuleContext) -> Outcome:
    index = _sheet_index(ctx)
    if index is None or ctx.model.meta.get("layouts") is None:
        return Outcome.skipped("sheet index (meta.sheet_index) or measured layouts (meta.layouts) not provided")
    required = ("project", "client", "designer", "prepared_by", "checked_by", "approved_by", "sheet_code", "sheet_title",
                "scale", "date", "revision", "status")
    must_fill = {"sheet_code", "sheet_title", "scale"}
    people = {"prepared_by", "checked_by", "approved_by"}
    issues: list[Issue] = []
    checked = 0
    for s in index:
        lay = _layout_of(ctx, s)
        if lay is None:
            continue
        code = str(s.get("code"))
        block = lay.get("title_block")
        checked += 1
        if not isinstance(block, Mapping) or not block:
            issues.append(Issue(f"sheet {code}: no title block", [code]))
            continue
        for key in required:
            entry = block.get(key)
            if not isinstance(entry, Mapping) or not str(entry.get("label", "")).strip():
                issues.append(Issue(f"sheet {code}: title block field {key!r} is missing", [code]))
                continue
            value = str(entry.get("value", "")).strip()
            if key in must_fill and not value:
                issues.append(Issue(f"sheet {code}: title block field {key!r} is empty", [code]))
            elif key == "project" and not value:
                issues.append(Issue(f"sheet {code}: project name is blank (not supplied)", [code], severity=Severity.WARNING))
            elif key in people and value:
                issues.append(Issue(f"sheet {code}: {key} is filled in ({value!r}); this tool never writes names or "
                                    "signatures, check that the responsible person entered it", [code],
                                    severity=Severity.WARNING))
            elif key in ("client", "designer", "date", "revision") and not value:
                issues.append(Issue(f"sheet {code}: {key} is blank (left for the responsible person)", [code],
                                    severity=Severity.INFO))
        if isinstance(block.get("sheet_code"), Mapping) and str(block["sheet_code"].get("value", "")).strip() not in ("", code):
            issues.append(Issue(f"sheet {code}: the title block says {block['sheet_code'].get('value')!r}", [code],
                                kind="CONFLICT"))
        declared = str(s.get("scale") or "")
        if declared and isinstance(block.get("scale"), Mapping) and str(block["scale"].get("value", "")).strip() \
                not in ("", declared):
            issues.append(Issue(f"sheet {code}: the title block scale {block['scale'].get('value')!r} differs from the "
                                f"declared {declared!r}", [code], kind="CONFLICT"))
    if not checked:
        return Outcome.skipped("no sheet with a layout")
    return Outcome(issues, f"{checked} title blocks: fields present, sheet code and scale equal the sheet index, people "
                           "fields untouched", checked)


def rule_paper(ctx: RuleContext) -> Outcome:
    index = _sheet_index(ctx)
    if index is None or ctx.model.meta.get("layouts") is None:
        return Outcome.skipped("sheet index (meta.sheet_index) or measured layouts (meta.layouts) not provided")
    issues: list[Issue] = []
    checked = 0
    for s in index:
        lay = _layout_of(ctx, s)
        want = s.get("paper_mm")
        if lay is None or not isinstance(want, (list, tuple)) or len(want) != 2:
            continue
        got = lay.get("paper_mm")
        checked += 1
        if not isinstance(got, (list, tuple)) or len(got) != 2:
            issues.append(Issue(f"sheet {s.get('code')}: paper size of the layout could not be read", [str(s.get("code"))],
                                kind="INCOMPLETE"))
        elif abs(got[0] - want[0]) > 1.0 or abs(got[1] - want[1]) > 1.0:
            issues.append(Issue(f"sheet {s.get('code')}: paper is {got[0]:g} x {got[1]:g} mm but {want[0]:g} x {want[1]:g} "
                                f"({s.get('paper')} {s.get('orientation')}) was declared", [str(s.get("code"))],
                                kind="CONFLICT"))
    if not checked:
        return Outcome.skipped("no sheet declares a paper size")
    return Outcome(issues, f"{checked} layouts: paper size and orientation equal the declared sheet paper", checked)


def rule_pdf_pages(ctx: RuleContext) -> Outcome:
    state = ctx.model.meta.get("pdf_render_check")
    index = _sheet_index(ctx)
    if not isinstance(state, Mapping) or not state.get("page_checks"):
        return Outcome.skipped("no per-page PDF check (meta.pdf_render_check.page_checks): the PDF was not rendered page by page")
    issues: list[Issue] = []
    pages = state["page_checks"]
    expected = _num(state.get("expected_pages"))
    if expected is None and index is not None:
        expected = float(len(index))
    if expected is not None and len(pages) != int(expected):
        issues.append(Issue(f"the PDF has {len(pages)} pages but {int(expected)} sheets were plotted", []))
    for i, page in enumerate(pages, 1):
        if (_num(page.get("nonwhite_ratio")) or 0.0) < 0.0005:
            issues.append(Issue(f"PDF page {i} looks blank when rendered (non-white ratio {page.get('nonwhite_ratio')})", []))
        if not (_num(page.get("text_chars")) or 0):
            issues.append(Issue(f"PDF page {i} has no text layer", [], severity=Severity.WARNING))
        if page.get("sheet_code") and page.get("codes_found") is not None and page["sheet_code"] not in page["codes_found"]:
            issues.append(Issue(f"PDF page {i}: the sheet code {page['sheet_code']} was not found in its text", []))
        want = page.get("paper_mm")
        got = page.get("size_mm")
        if want and got and (abs(want[0] - got[0]) > 2.0 or abs(want[1] - got[1]) > 2.0):
            issues.append(Issue(f"PDF page {i}: size {got} mm differs from the layout paper {want} mm", [], kind="CONFLICT"))
    return Outcome(issues, f"{len(pages)} PDF pages: count, non-blank, text layer, sheet codes, page size", len(pages))


def rule_reopen(ctx: RuleContext) -> Outcome:
    state = ctx.model.meta.get("reopen_check")
    if not isinstance(state, Mapping):
        return Outcome.skipped("the saved DWG was not re-opened in AutoCAD (meta.reopen_check)")
    issues: list[Issue] = []
    if not state.get("opened"):
        issues.append(Issue("the saved DWG could not be re-opened in AutoCAD", []))
    for diff in state.get("differences") or []:
        issues.append(Issue(f"re-opened DWG differs from the drawing that was saved: {diff}", [], kind="CONFLICT"))
    return Outcome(issues, f"saved DWG re-opened in AutoCAD; counts compared ({state.get('compared', 'n/a')})", 1)


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("FILE.UNITS_DECLARED", "Drawing units are known and match the profile", "FILE", Severity.BLOCKER, rule_units),
    Rule("FILE.MODEL_SCALE_1TO1", "Model space is drawn 1:1 (checked against a real reference length)", "FILE",
         Severity.BLOCKER, rule_model_scale, tolerance_purposes=()),
    Rule("FILE.OPENS_WITHOUT_RECOVERY", "DWG opens without an unhandled recovery warning", "FILE", Severity.BLOCKER,
         rule_recovery),
    Rule("FILE.XREF_RESOLVED", "All xrefs resolve to an existing source", "FILE", Severity.ERROR, rule_xref),
    Rule("FILE.FONT_RESOLVED", "Fonts are present and not substituted", "FILE", Severity.WARNING, rule_fonts),
    Rule("FILE.AUDIT_LOGGED", "AUDIT ran, its result is logged and no error is left", "FILE", Severity.ERROR,
         rule_audit),
    Rule("GEOM.ZERO_LENGTH", "No zero-length segments", "GEOMETRY", Severity.ERROR, rule_zero_length,
         tolerance_purposes=("zero_length",)),
    Rule("GEOM.ARC_VALID", "Arcs and circles have a positive radius and a real sweep", "GEOMETRY", Severity.ERROR,
         rule_arc_valid),
    Rule("GEOM.POLYLINE_CLOSURE", "Polylines that must be closed (rooms, expect_closed) are closed", "GEOMETRY",
         Severity.ERROR, rule_closure, tolerance_purposes=("closure_gap",)),
    Rule("GEOM.ROOM_BOUNDARY_CLOSED", "Room boundaries form a closed ring (required before any area)", "GEOMETRY",
         Severity.ERROR, rule_room_boundary, tolerance_purposes=("closure_gap",)),
    Rule("GEOM.SELF_INTERSECTION", "Polylines do not cross or overlap themselves", "GEOMETRY", Severity.ERROR,
         rule_self_intersection, tolerance_purposes=("intersection",)),
    Rule("GEOM.DUPLICATE_ENTITIES", "Candidate duplicate entities (reported, never auto-deleted)", "GEOMETRY",
         Severity.WARNING, rule_duplicates, {"max_pairs": 2_000_000}, ("duplicate",)),
    Rule("GEOM.OUT_OF_BOUNDS", "Geometry lies inside the drawing area", "GEOMETRY", Severity.WARNING, rule_bounds,
         {"drawing_bounds": None}),
    Rule("GEOM.WALL_THICKNESS", "Walls have a thickness or a stated centreline convention", "GEOMETRY", Severity.ERROR,
         rule_wall_thickness),
    Rule("GEOM.OPENING_HOST_WALL", "Doors/windows are linked to a host wall and sit inside it", "GEOMETRY",
         Severity.ERROR, rule_opening_host, tolerance_purposes=("opening_offset",)),
    Rule("ANNO.DIMENSION_OVERRIDE", "Displayed dimension value matches the measured geometry (else CONFLICT)",
         "ANNOTATION", Severity.ERROR, rule_dimension_override, tolerance_purposes=("dimension",)),
    Rule("ANNO.DIMENSION_CHAIN", "Dimension chains add up to their overall dimension", "ANNOTATION", Severity.ERROR,
         rule_dimension_chain, tolerance_purposes=("dimension",)),
    Rule("ANNO.TEXT_NOT_EMPTY", "Text annotations are not empty", "ANNOTATION", Severity.WARNING, rule_text),
    Rule("ANNO.GRID_LABELS_UNIQUE", "Grid labels exist and are unique", "ANNOTATION", Severity.ERROR, rule_grid_labels),
    Rule("ANNO.MARKER_SHEET_EXISTS", "Detail/section markers reference existing sheets", "ANNOTATION", Severity.ERROR,
         rule_marker_sheets),
    Rule("COORD.OPENING_CODES_UNIQUE", "Door and window codes exist and are unique", "COORDINATION", Severity.ERROR,
         rule_code_unique),
    Rule("COORD.SCHEDULE_MATCHES_SYMBOLS", "Door/window schedules match the plan symbols", "COORDINATION",
         Severity.ERROR, rule_schedule_match, tolerance_purposes=("schedule_dimension",)),
    Rule("COORD.AREA_RECOMPUTED", "Stated room/total areas equal the area recomputed from the boundary, with a rule",
         "COORDINATION", Severity.ERROR, rule_area, tolerance_purposes=("closure_gap", "area_rel")),
    Rule("COORD.ROOM_OVERLAP_LEVEL", "Rooms do not overlap and each has a level", "COORDINATION", Severity.ERROR,
         rule_room_overlap, tolerance_purposes=("closure_gap",)),
    Rule("COORD.STAIR_RISERS", "Stair riser count x height equals total height and the level difference",
         "COORDINATION", Severity.ERROR, rule_stairs, {"max_riser_height": None, "min_tread_depth": None},
         ("riser_total", "elevation")),
    Rule("COORD.LEVELS_CONSISTENT", "Levels share a datum, are distinct and every level reference resolves",
         "COORDINATION", Severity.ERROR, rule_levels, tolerance_purposes=("elevation",)),
    Rule("COORD.VIEWS_CONSISTENT", "Plan/elevation/section values of one entity agree", "COORDINATION",
         Severity.ERROR, rule_view_consistency, tolerance_purposes=("schedule_dimension",)),
    Rule("COORD.ELEVATION_MATCHES_PLAN", "Door/window codes, widths, heights, sills and facade in each elevation equal the plan",
         "COORDINATION", Severity.ERROR, rule_elevation_matches_plan, tolerance_purposes=("schedule_dimension",)),
    Rule("COORD.SCHEDULE_MATCHES_ELEVATION", "Door/window schedule rows (codes, widths, heights) equal the elevations",
         "COORDINATION", Severity.ERROR, rule_schedule_matches_elevation, tolerance_purposes=("schedule_dimension",)),
    Rule("COORD.LEVELS_ELEVATION_SECTION", "Level marks, labels and clear heights agree across elevations, sections and LEVEL data",
         "COORDINATION", Severity.ERROR, rule_levels_elevation_section, tolerance_purposes=("elevation",)),
    Rule("COORD.SECTION_MARKER_VIEW", "Each section marker references an existing section view cut where the marker is drawn",
         "COORDINATION", Severity.ERROR, rule_section_marker_view, tolerance_purposes=("opening_offset",)),
    Rule("COORD.ROOM_TABLE_AREA", "Area table rows and total equal the area recomputed from the room boundaries",
         "COORDINATION", Severity.ERROR, rule_room_table_area, tolerance_purposes=("closure_gap", "area_rel")),
    Rule("COORD.STAIR_OPENING_IN_SLAB", "Each stair has an opening in the slab it arrives at, inside its footprint and equal to the "
         "opening drawn on that floor's plan", "COORDINATION", Severity.ERROR, rule_stair_opening,
         tolerance_purposes=("opening_offset", "elevation")),
    Rule("COORD.STAIR_PLAN_MATCHES_MODEL", "Stair symbols on the plans have the model's riser count and footprint and every stair is "
         "drawn on the floor it starts on", "COORDINATION", Severity.ERROR, rule_stair_plan,
         tolerance_purposes=("opening_offset",)),
    Rule("COORD.FLOOR_LEVELS_ALL_FLOORS", "Floor/ceiling levels, slab gaps, floor contents and the level marks of every view are "
         "consistent for all floors", "COORDINATION", Severity.ERROR, rule_floor_levels, tolerance_purposes=("elevation",)),
    Rule("PLOT.LAYOUT_SETUP", "Layouts have the profile paper size, a viewport scale and a title block", "PLOT",
         Severity.ERROR, rule_layouts),
    Rule("PLOT.SHEET_LAYOUT_EXISTS", "Every sheet code of the sheet index has a layout (and codes are unique)", "PLOT",
         Severity.ERROR, rule_sheet_layouts),
    Rule("PLOT.VIEWPORT_SCALE", "Each sheet's viewport scale equals the scale declared in the sheet index", "PLOT",
         Severity.ERROR, rule_viewport_scale),
    Rule("PLOT.VIEWPORT_LOCKED", "Every sheet viewport is locked", "PLOT", Severity.ERROR, rule_viewport_locked),
    Rule("PLOT.TITLE_BLOCK_FIELDS", "Title block has all fields; sheet code and scale equal the index; people fields are not "
         "filled by the tool", "PLOT", Severity.ERROR, rule_title_block),
    Rule("PLOT.PAPER_ORIENTATION", "Layout paper size and orientation equal the declared sheet paper", "PLOT", Severity.ERROR,
         rule_paper),
    Rule("PLOT.PDF_PAGES_MATCH_SHEETS", "The plotted PDF has one non-blank page per sheet, the right size, text and sheet codes",
         "PLOT", Severity.ERROR, rule_pdf_pages),
    Rule("FILE.REOPEN_COUNTS", "The saved DWG re-opens in AutoCAD with the same entity counts", "FILE", Severity.ERROR,
         rule_reopen),
    Rule("PLOT.PDF_RENDER_CHECKED", "The PDF was rendered to an image and inspected", "PLOT", Severity.ERROR,
         rule_pdf_rendered),
)


def rule_catalog() -> list[dict[str, Any]]:
    return [r.describe() for r in DEFAULT_RULES]


# --------------------------------------------------------------------------- waivers and the run

WAIVER_FIELDS = ("rule_id", "approved_by", "reason", "date", "scope")


def check_waiver(waiver: Mapping[str, Any]) -> str | None:
    """None when the waiver is complete, else why it is rejected. Spec 16: approver, reason, date and scope."""
    missing = [f for f in WAIVER_FIELDS if not str(waiver.get(f) or "").strip()]
    return f"missing {', '.join(missing)}" if missing else None


@dataclass
class ValidationReport:
    results: list[RuleResult]
    overall: str
    counts: dict[str, int]
    by_scope: dict[str, str]
    not_run: list[str]
    waived: list[dict[str, Any]]
    rejected_waivers: list[dict[str, Any]]
    tolerances: dict[str, Any]
    open_blockers: list[str]
    open_errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"overall": self.overall, "counts": self.counts, "by_scope": self.by_scope, "not_run": self.not_run,
                "waived": self.waived, "rejected_waivers": self.rejected_waivers, "tolerances": self.tolerances,
                "open_blockers": self.open_blockers, "open_errors": self.open_errors,
                "results": [r.to_dict() for r in self.results]}


def run_validation(model: DrawingModel, *, tolerances: Mapping[str, Any] | None = None,
                   params: Mapping[str, Mapping[str, Any]] | None = None,
                   waivers: Sequence[Mapping[str, Any]] | None = None, scopes: Sequence[str] | None = None,
                   rule_ids: Sequence[str] | None = None, rules: Sequence[Rule] | None = None) -> ValidationReport:
    tol = Tolerances(model.unit_mm, tolerances)
    selected = [r for r in (rules or DEFAULT_RULES)
                if (not scopes or r.scope in scopes) and (not rule_ids or r.rule_id in rule_ids)]
    if rule_ids:
        unknown = set(rule_ids) - {r.rule_id for r in (rules or DEFAULT_RULES)}
        if unknown:
            raise ValueError(f"unknown rule_id(s) {sorted(unknown)}")
    good_waivers: list[Mapping[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for w in waivers or []:
        problem = check_waiver(w)
        if problem:
            rejected.append({"waiver": dict(w), "reason": problem})
        else:
            good_waivers.append(w)
    results: list[RuleResult] = []
    waived: list[dict[str, Any]] = []
    for rule in selected:
        merged = {**rule.parameters, **dict((params or {}).get(rule.rule_id) or {})}
        try:
            outcome = rule.check(RuleContext(model, tol, merged))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            outcome = Outcome([Issue(f"rule crashed on malformed data: {type(exc).__name__}: {exc}", kind="RULE_ERROR",
                                     severity=Severity.ERROR)], "rule could not evaluate the data", 0)
        if outcome.not_run is not None:
            results.append(RuleResult(rule.rule_id, rule.description, rule.scope, rule.severity.value, Result.NOT_RUN,
                                      outcome.not_run, merged, [], 0))
            continue
        if not outcome.issues and outcome.checked == 0:
            results.append(RuleResult(rule.rule_id, rule.description, rule.scope, rule.severity.value, Result.NOT_RUN,
                                      "nothing applicable to check", merged, [], 0))
            continue
        for issue in outcome.issues:
            sev = issue.severity or rule.severity
            for w in good_waivers:
                if w["rule_id"] == rule.rule_id and sev is not Severity.BLOCKER and \
                        (not w.get("entity_ids") or set(w["entity_ids"]) & set(issue.entity_ids)):
                    issue.waived = True
                    waived.append({"rule_id": rule.rule_id, "message": issue.message, "approved_by": w["approved_by"],
                                   "reason": w["reason"], "date": w["date"], "scope": w["scope"]})
                    break
        worst = max((i.severity or rule.severity for i in outcome.issues), key=lambda s: SEVERITY_RANK[s],
                    default=rule.severity)
        if not outcome.issues:
            res, sev_text = Result.PASS, rule.severity.value
        elif SEVERITY_RANK[worst] >= SEVERITY_RANK[Severity.ERROR]:
            res, sev_text = Result.FAIL, worst.value
        elif worst is Severity.WARNING:
            res, sev_text = Result.WARN, worst.value
        else:
            res, sev_text = Result.PASS, worst.value
        results.append(RuleResult(rule.rule_id, rule.description, rule.scope, sev_text, res, outcome.evidence, merged,
                                  [i.to_dict(rule.severity) for i in outcome.issues], outcome.checked))
    return _summarise(results, waived, rejected, tol)


def _summarise(results: list[RuleResult], waived: list[dict[str, Any]], rejected: list[dict[str, Any]],
               tol: Tolerances) -> ValidationReport:
    counts = {s.value: 0 for s in Severity}
    open_blockers: list[str] = []
    open_errors: list[str] = []
    for r in results:
        for issue in r.issues:
            if issue["waived"]:
                continue
            counts[issue["severity"]] += 1
            if issue["severity"] == "BLOCKER":
                open_blockers.append(r.rule_id)
            elif issue["severity"] == "ERROR":
                open_errors.append(r.rule_id)
    ran = [r for r in results if r.result is not Result.NOT_RUN]
    if open_blockers or open_errors:
        overall = "FAIL"
    elif not ran:
        overall = "NOT_RUN"
    elif counts["WARNING"]:
        overall = "WARN"
    else:
        overall = "PASS"
    by_scope: dict[str, str] = {}
    for scope in SCOPES:
        scoped = [r for r in results if r.scope == scope]
        if not scoped:
            continue
        live = [r for r in scoped if r.result is not Result.NOT_RUN]
        open_issues = [i for r in scoped for i in r.issues if not i["waived"]]
        if any(i["severity"] in ("BLOCKER", "ERROR") for i in open_issues):
            by_scope[scope] = "FAIL"
        elif not live:
            by_scope[scope] = "NOT_RUN"
        elif any(i["severity"] == "WARNING" for i in open_issues):
            by_scope[scope] = "WARN"
        elif len(live) < len(scoped):
            by_scope[scope] = "PARTIAL"   # everything that ran passed, but some rules could not run
        else:
            by_scope[scope] = "PASS"
    return ValidationReport(results, overall, counts, by_scope, [r.rule_id for r in results if r.result is Result.NOT_RUN],
                            waived, rejected, tol.describe(), sorted(set(open_blockers)), sorted(set(open_errors)))
