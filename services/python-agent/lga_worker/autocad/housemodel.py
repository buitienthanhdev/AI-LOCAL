"""One shared data model for a generated house: the plan, the elevations, the sections and the schedules read it.

`build_house_model` takes the same parameters as `autocad.draw_floor_plan` (width, depth/area, bedrooms, altar, study,
porch) plus a few vertical parameters, runs `floorplan.plan_house` and turns its geometry into the intermediate model of
the specification (section 3): levels, wall runs with thickness and centre line, doors/windows with unique codes, sizes
and sill heights linked to their host wall, rooms with a closed boundary and an area recomputed from that boundary.

Nothing here talks to AutoCAD and no number is typed twice: the drawing code (`houseviews`) and the validation model
(`HouseModel.validation_model`) read these objects only.

Honesty about numbers: every number that does not come from the plan geometry or from an argument of the caller is a
WORKING DEFAULT (`WORKING_DEFAULTS`). It is flagged ASSUMED with a source note in `HouseModel.assumptions` and in the
entities; it is not a code requirement and nothing here checks any regulation. Structure (slab thickness, foundations)
is a graphic placeholder, not an engineer's design.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lga_worker.autocad import floorplan, geom2d
from lga_worker.autocad.provenance import Entity, EntityType, Source, SourceStatus

LEVEL_GROUND = "LV-GROUND"
LEVEL_FLOOR = "LV-F1"
LEVEL_CEILING = "LV-CEIL"
LEVEL_ROOF = "LV-ROOF"
LEVEL_RIDGE = "LV-RIDGE"
LEVEL_PARAPET = "LV-PARAPET"
DATUM_NOTE = "+0.000 = finished floor level of the ground floor (project convention, to be confirmed)"
AREA_RULE = "centreline"  # the plan prints "BẢNG DIỆN TÍCH (tim tường)": room rectangles between wall centre lines
SIDES = ("front", "rear", "left", "right")

# name -> (value, unit, why). Used when the caller gives no value; always reported as ASSUMED.
WORKING_DEFAULTS: dict[str, tuple[Any, str, str]] = {
    "storey_height": (3.3, "m", "clear height floor to ceiling; working default for a single-storey house, not a code requirement"),
    "plinth_height": (0.45, "m", "floor level above the outside ground; working default, depends on the site"),
    "slab_thickness": (0.1, "m", "graphic placeholder for the ceiling/roof slab and floor slab; no structural design"),
    "roof_type": ("pitched", "", "gable roof with the ridge along the depth; working default"),
    "roof_slope_deg": (30.0, "deg", "roof pitch; working default, depends on the roofing material"),
    "roof_overhang": (0.6, "m", "eaves overhang; working default"),
    "parapet_height": (0.6, "m", "parapet of a flat roof; working default"),
    "door_height": (2.1, "m", "single door height; working default"),
    "double_door_height": (2.4, "m", "double door height; working default"),
    "window_height": (1.2, "m", "window height; working default"),
    "window_sill": (0.9, "m", "window sill height above the floor; working default"),
    "small_window_height": (0.6, "m", "height of a narrow (<= 0.7 m) high window, e.g. a bathroom; working default"),
    "small_window_sill": (1.5, "m", "sill of a narrow high window; working default"),
}
DEFAULT_NOTE_WALLS = ("wall thickness 0.20 m exterior / 0.10 m interior are the planner's working constants "
                      "(floorplan.EXTERIOR_WALL / INTERIOR_WALL), not a structural or code value")


class HouseModelError(ValueError):
    pass


def _clean(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HouseModelError(f"{name} must be a number, got {value!r}")
    if not low <= float(value) <= high:
        raise HouseModelError(f"{name} must be between {low:g} and {high:g}, got {value:g}")
    return float(value)


@dataclass
class HouseParams:
    width: float | None = None
    depth: float | None = None
    area: float | None = None
    bedrooms: int | None = None
    altar: bool | None = None
    study: bool | None = None
    porch: float | None = None
    storey_height: float | None = None
    plinth_height: float | None = None
    slab_thickness: float | None = None
    roof_type: str | None = None
    roof_slope_deg: float | None = None
    roof_overhang: float | None = None
    parapet_height: float | None = None
    door_height: float | None = None
    double_door_height: float | None = None
    window_height: float | None = None
    window_sill: float | None = None

    @classmethod
    def from_args(cls, args: Mapping[str, Any]) -> HouseParams:
        names = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: args[k] for k in names if args.get(k) is not None})

    def provided(self) -> set[str]:
        return {k for k in self.__dataclass_fields__ if getattr(self, k) is not None}


@dataclass
class Level:
    id: str
    name: str
    elevation: float
    status: SourceStatus
    note: str


@dataclass
class Wall:
    id: str
    orient: str
    line: float
    a: float
    b: float
    thickness: float
    exterior: bool
    facade: str | None
    level_id: str

    @property
    def centreline(self) -> tuple[tuple[float, float], tuple[float, float]]:
        if self.orient == "v":
            return (self.line, self.a), (self.line, self.b)
        return (self.a, self.line), (self.b, self.line)

    @property
    def length(self) -> float:
        return self.b - self.a


@dataclass
class Opening:
    code: str
    kind: str  # door | double | window | open
    orient: str
    line: float
    a: float
    b: float
    swing: int
    hinge: str
    sill: float
    height: float
    wall_id: str
    facade: str | None
    level_id: str
    status: SourceStatus

    @property
    def etype(self) -> str:
        return "WINDOW" if self.kind == "window" else "OPENING" if self.kind == "open" else "DOOR"

    @property
    def width(self) -> float:
        return round(self.b - self.a, 6)

    @property
    def head(self) -> float:
        return self.sill + self.height

    @property
    def centre(self) -> tuple[float, float]:
        m = (self.a + self.b) / 2
        return (self.line, m) if self.orient == "v" else (m, self.line)


@dataclass
class Room:
    code: str
    name: str
    kind: str
    boundary: list[tuple[float, float]]
    listed: bool  # appears in the area table (the plan lists every room but the corridor and the annex alcoves)
    level_id: str

    @property
    def area(self) -> float:
        return geom2d.polygon_area(self.boundary)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = [p[0] for p in self.boundary], [p[1] for p in self.boundary]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Stair:
    """A stair as the section draws it. `plan_house` makes none (single storey); a caller or a test may add one."""

    id: str
    from_level: str
    to_level: str
    total_height: float
    riser_count: int
    tread: float
    width: float
    footprint: tuple[float, float, float, float]  # x0, y0, x1, y1 in plan metres
    run_axis: str  # "x" or "y": the direction the flight climbs along
    run_sign: int = 1  # +1: rises towards +axis


@dataclass
class HouseModel:
    params: HouseParams
    plan: floorplan.Plan
    levels: list[Level]
    walls: list[Wall]
    openings: list[Opening]
    rooms: list[Room]
    stairs: list[Stair]
    porch_columns: list[tuple[float, float]]  # x0, x1 of the porch posts (depth 0..0.3 m in front of the house)
    roof: dict[str, Any]
    values: dict[str, Any]  # the resolved vertical numbers (caller's or working default)
    assumptions: list[dict[str, Any]] = field(default_factory=list)

    # ---- simple accessors
    @property
    def width(self) -> float:
        return self.plan.width

    @property
    def depth(self) -> float:
        return self.plan.depth

    @property
    def porch(self) -> float:
        return self.plan.porch

    @property
    def text_height(self) -> float:
        return self.plan.text_height

    def level(self, level_id: str) -> Level:
        for lv in self.levels:
            if lv.id == level_id:
                return lv
        raise KeyError(level_id)

    def elevation(self, level_id: str) -> float:
        return self.level(level_id).elevation

    def has_level(self, level_id: str) -> bool:
        return any(lv.id == level_id for lv in self.levels)

    def wall(self, wall_id: str) -> Wall:
        return next(w for w in self.walls if w.id == wall_id)

    def openings_of(self, *kinds: str) -> list[Opening]:
        return [o for o in self.openings if o.kind in kinds]

    @property
    def doors(self) -> list[Opening]:
        return self.openings_of("door", "double")

    @property
    def windows(self) -> list[Opening]:
        return self.openings_of("window")

    @property
    def listed_rooms(self) -> list[Room]:
        return [r for r in self.rooms if r.listed]

    def roof_z_at(self, x: float) -> float:
        """Top of the roof at plan x, pitched roof with the ridge along y (the sloped line goes on past the walls)."""
        z_e = self.elevation(LEVEL_ROOF)
        if self.roof["type"] != "pitched":
            return z_e
        return z_e + min(x, self.width - x) * math.tan(math.radians(self.roof["slope_deg"]))

    # ---- schedules: rows shared by the drawn tables and the validation model
    def door_rows(self) -> list[dict[str, Any]]:
        return [{"code": o.code, "type": "double" if o.kind == "double" else "single", "width": o.width,
                 "height": o.height, "sill_height": o.sill, "wall_id": o.wall_id, "facade": o.facade or "",
                 "level_id": o.level_id, "leaf_count": 2 if o.kind == "double" else 1} for o in self.doors]

    def window_rows(self) -> list[dict[str, Any]]:
        return [{"code": o.code, "width": o.width, "height": o.height, "sill_height": o.sill,
                 "head_height": round(o.head, 6), "wall_id": o.wall_id, "facade": o.facade or "",
                 "level_id": o.level_id} for o in self.windows]

    def room_rows(self) -> list[dict[str, Any]]:
        return [{"code": r.code, "name": r.name, "area": round(r.area, 4), "level_id": r.level_id,
                 "area_rule": AREA_RULE} for r in self.listed_rooms]

    # ---- the intermediate model of the specification
    def _datum(self, key: str) -> dict[str, Any]:
        provided = key in self.params.provided()
        return {"value": self.values[key], "source_status": "PROVIDED" if provided else "ASSUMED",
                "source": {"note": "argument of the caller" if provided else WORKING_DEFAULTS[key][2]}}

    def _size_note(self, o: Opening) -> str:
        key = "double_door_height" if o.kind == "double" else "door_height" if o.etype == "DOOR" else None
        if key is not None:
            return "argument of the caller" if key in self.params.provided() else WORKING_DEFAULTS[key][2]
        return ("width from the plan layout (floorplan.WINDOW_WIDTH); height and sill are working defaults "
                "(window_height / window_sill, narrow windows small_window_*)")

    def entities(self) -> list[Entity]:
        out: list[Entity] = []
        generated = Source(note="generated by lga_worker.autocad.housemodel from the plan_house parameters")
        for lv in self.levels:
            out.append(Entity(lv.id, EntityType.LEVEL, level_id=None, elevation=lv.elevation, units="m",
                              properties={"name": lv.name, "datum": DATUM_NOTE, "elevation_status": lv.status.value},
                              source=Source(note=lv.note), status=lv.status))
        for w in self.walls:
            a, b = w.centreline
            out.append(Entity(
                w.id, EntityType.WALL, geometry={"type": "line", "start": list(a), "end": list(b)}, units="m",
                level_id=w.level_id, elevation=0.0,
                properties={"thickness": w.thickness, "exterior": w.exterior, "centerline_rule": "centreline",
                            "facade": w.facade, "thickness_status": "ASSUMED", "thickness_note": DEFAULT_NOTE_WALLS},
                source=generated, status=SourceStatus.DERIVED))
        for o in self.openings:
            cx, cy = o.centre
            props: dict[str, Any] = {"code": o.code, "kind": o.kind, "width": o.width, "facade": o.facade}
            if o.etype != "OPENING":
                props["height"] = o.height
                props["sill_height"] = o.sill
                props["head_height"] = round(o.head, 6)
                props["swing"] = o.swing
                props["hinge"] = o.hinge
                props["size_status"] = o.status.value
                props["size_note"] = self._size_note(o)
            out.append(Entity(
                o.code, EntityType(o.etype), geometry={"type": "point", "point": [cx, cy]}, units="m",
                level_id=o.level_id, elevation=0.0, properties=props, relationships={"host_wall": o.wall_id},
                source=generated, status=o.status))
        for r in self.rooms:
            out.append(Entity(
                r.code, EntityType.ROOM, geometry={"type": "polyline", "points": [list(p) for p in r.boundary],
                                                   "closed": True}, units="m", level_id=r.level_id, elevation=0.0,
                properties={"code": r.code, "name": r.name, "kind": r.kind, "stated_area": round(r.area, 4), "area_rule": AREA_RULE,
                            "listed": r.listed},
                source=generated, status=SourceStatus.DERIVED))
        for s in self.stairs:
            out.append(Entity(
                s.id, EntityType.STAIR, units="m", level_id=s.from_level,
                properties={"from_level": s.from_level, "to_level": s.to_level, "total_height": s.total_height,
                            "riser_count": s.riser_count, "riser_height": round(s.total_height / s.riser_count, 6),
                            "tread_depth": s.tread, "width": s.width, "footprint": list(s.footprint),
                            "run_axis": s.run_axis, "run_sign": s.run_sign},
                source=Source(note="stair supplied to the model"), status=SourceStatus.PROVIDED))
        z_e = self.elevation(LEVEL_ROOF)
        out.append(Entity("SLAB-FLOOR", EntityType.SLAB, level_id=LEVEL_FLOOR, elevation=0.0, units="m",
                          properties={"thickness": self.values["slab_thickness"], "role": "floor",
                                      "thickness_status": "ASSUMED", "structural_design": False},
                          source=Source(note=WORKING_DEFAULTS["slab_thickness"][2]), status=SourceStatus.ASSUMED))
        out.append(Entity("SLAB-ROOF", EntityType.SLAB, level_id=LEVEL_CEILING,
                          elevation=self.elevation(LEVEL_CEILING), units="m",
                          properties={"thickness": z_e - self.elevation(LEVEL_CEILING), "role": "ceiling/roof",
                                      "structural_design": False},
                          source=Source(note=WORKING_DEFAULTS["slab_thickness"][2]), status=SourceStatus.ASSUMED))
        out.append(Entity("ROOF-1", EntityType.ROOF, units="m", level_id=LEVEL_ROOF, elevation=z_e,
                          properties=dict(self.roof), source=Source(note="roof parameters are working defaults"),
                          status=SourceStatus.ASSUMED))
        return out

    def validation_model(self, views: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """`autocad.validate_model` input. `views` are the records the draw tools measured from what they drew;
        without them the cross-view rules have nothing to compare and report NOT_RUN."""
        entities = [e.to_dict() for e in self.entities()]
        schedules: dict[str, list[dict[str, Any]]] = {"door": self.door_rows(), "window": self.window_rows(),
                                                      "room": self.room_rows()}
        records: list[dict[str, Any]] = [{"id": "PLAN-F1", "kind": "plan", "code": "A-101", "level_id": LEVEL_FLOOR}]
        sheets = ["A-101"]
        for rec in views:
            rec = dict(rec)
            records.append(rec)
            if rec.get("sheet"):
                sheets.append(str(rec["sheet"]))
            if rec.get("kind") == "schedule" and rec.get("schedule") in schedules and rec.get("rows") is not None:
                schedules[str(rec["schedule"])] = [dict(r) for r in rec["rows"]]
            marker = rec.get("marker")
            if rec.get("kind") == "section" and isinstance(marker, Mapping):
                entities.append(Entity(
                    f"MK-{rec.get('section_code', 'SEC')}", EntityType.DETAIL_MARKER,
                    geometry={"type": "line", "start": list(marker["start"]), "end": list(marker["end"])}, units="m",
                    properties={"section_code": rec.get("section_code"), "target_view": marker.get("target_view"),
                                "target_sheet": marker.get("target_sheet"), "axis": marker.get("axis"),
                                "position": marker.get("position"), "direction": marker.get("direction")},
                    source=Source(note="measured from the marker drawn by autocad.draw_section"),
                    status=SourceStatus.MEASURED).to_dict())
        meta = {"units": "m", "datum": DATUM_NOTE, "sheets": sorted(set(sheets)), "views": records,
                "area_rule": AREA_RULE, "assumptions": self.assumptions,
                "generator": "lga_worker.autocad.housemodel"}
        return {"meta": meta, "entities": entities, "schedules": schedules}

    def summary(self) -> dict[str, Any]:
        return {"width": self.width, "depth": self.depth, "porch": self.porch,
                "levels": [{"id": lv.id, "name": lv.name, "elevation": lv.elevation, "status": lv.status.value}
                           for lv in self.levels],
                "walls": len(self.walls), "doors": [o.code for o in self.doors],
                "windows": [o.code for o in self.windows],
                "rooms": [{"code": r.code, "name": r.name, "area_m2": round(r.area, 2)} for r in self.listed_rooms],
                "roof": dict(self.roof), "stairs": [s.id for s in self.stairs]}


def facade_of(orient: str, line: float, exterior: bool, w: float, d: float, p: float) -> str | None:
    if not exterior:
        return None
    if orient == "h":
        return "front" if abs(line - p) < floorplan.EPS else "rear" if abs(line - d) < floorplan.EPS else None
    return "left" if abs(line) < floorplan.EPS else "right" if abs(line - w) < floorplan.EPS else None


def _room_boundary(room: floorplan.Room, annexes: list[floorplan.Room]) -> list[tuple[float, float]]:
    """Rectangle of the room between wall centre lines; a bedroom with an annex alcove gets the L-shaped union."""
    x0, y0, x1, y1 = room.x0, room.y0, room.x1, room.y1
    for ann in annexes:
        if abs(ann.y0 - y0) < floorplan.EPS and ann.y1 < y1 - floorplan.EPS:
            if abs(ann.x0 - x1) < floorplan.EPS:  # alcove on the +x side
                return [(x0, y0), (ann.x1, y0), (ann.x1, ann.y1), (x1, ann.y1), (x1, y1), (x0, y1)]
            if abs(ann.x1 - x0) < floorplan.EPS:  # alcove on the -x side
                return [(ann.x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, ann.y1), (ann.x0, ann.y1)]
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _resolve(params: HouseParams) -> dict[str, Any]:
    values: dict[str, Any] = {k: v[0] for k, v in WORKING_DEFAULTS.items()}
    for key in params.provided() & set(WORKING_DEFAULTS):
        values[key] = getattr(params, key)
    values["storey_height"] = _clean(values["storey_height"], "storey_height", 2.2, 6.0)
    values["plinth_height"] = _clean(values["plinth_height"], "plinth_height", 0.0, 1.5)
    values["slab_thickness"] = _clean(values["slab_thickness"], "slab_thickness", 0.05, 0.4)
    values["roof_slope_deg"] = _clean(values["roof_slope_deg"], "roof_slope_deg", 5.0, 60.0)
    values["roof_overhang"] = _clean(values["roof_overhang"], "roof_overhang", 0.0, 2.0)
    values["parapet_height"] = _clean(values["parapet_height"], "parapet_height", 0.0, 2.0)
    values["door_height"] = _clean(values["door_height"], "door_height", 1.8, 3.0)
    values["double_door_height"] = _clean(values["double_door_height"], "double_door_height", 1.8, 3.5)
    values["window_height"] = _clean(values["window_height"], "window_height", 0.3, 2.4)
    values["window_sill"] = _clean(values["window_sill"], "window_sill", 0.0, 2.0)
    if str(values["roof_type"]) not in ("pitched", "flat"):
        raise HouseModelError(f"roof_type must be 'pitched' or 'flat', got {values['roof_type']!r}")
    values["roof_type"] = str(values["roof_type"])
    top_window = values["window_sill"] + values["window_height"]
    if top_window >= values["storey_height"]:
        raise HouseModelError(f"window sill {values['window_sill']:g} + height {values['window_height']:g} reaches the "
                              f"ceiling ({values['storey_height']:g})")
    if max(values["door_height"], values["double_door_height"]) >= values["storey_height"]:
        raise HouseModelError("a door is as tall as the storey; raise storey_height or lower the door height")
    return values


def build_house_model(params: HouseParams | Mapping[str, Any]) -> HouseModel:
    """The one model. Raises HouseModelError (a ValueError) when the plan or a vertical number is not acceptable."""
    if not isinstance(params, HouseParams):
        params = HouseParams.from_args(params)
    try:
        plan = floorplan.plan_house(width=params.width, depth=params.depth, area=params.area,
                                    bedrooms=params.bedrooms, altar=params.altar, study=params.study,
                                    porch=params.porch)
    except ValueError as exc:
        raise HouseModelError(str(exc)) from exc
    v = _resolve(params)
    w, d, p = plan.width, plan.depth, plan.porch
    provided = params.provided()

    def status(*keys: str) -> SourceStatus:
        return SourceStatus.PROVIDED if all(k in provided for k in keys) else SourceStatus.ASSUMED

    # levels
    slab = v["slab_thickness"]
    z_ceil = v["storey_height"]
    z_roof = round(z_ceil + slab, 6)
    levels = [
        Level(LEVEL_GROUND, "Cốt sân (đất tự nhiên)", round(-v["plinth_height"], 6), status("plinth_height"),
              WORKING_DEFAULTS["plinth_height"][2] if "plinth_height" not in provided else "argument of the caller"),
        Level(LEVEL_FLOOR, "Sàn tầng 1", 0.0, SourceStatus.ASSUMED, DATUM_NOTE),
        Level(LEVEL_CEILING, "Trần tầng 1", round(z_ceil, 6), status("storey_height"),
              WORKING_DEFAULTS["storey_height"][2] if "storey_height" not in provided else "argument of the caller"),
        Level(LEVEL_ROOF, "Đỉnh tường / mái", z_roof, SourceStatus.DERIVED,
              "ceiling level + slab thickness (working default)"),
    ]
    roof: dict[str, Any] = {"type": v["roof_type"], "overhang": v["roof_overhang"], "slope_deg": v["roof_slope_deg"],
                            "eaves_z": z_roof}
    if v["roof_type"] == "pitched":
        ridge = round(z_roof + (w / 2) * math.tan(math.radians(v["roof_slope_deg"])), 6)
        levels.append(Level(LEVEL_RIDGE, "Đỉnh nóc", ridge, SourceStatus.DERIVED,
                            "eaves level + half the width x tan(roof slope); slope is a working default"))
        roof["ridge_z"] = ridge
    else:
        top = round(z_roof + v["parapet_height"], 6)
        levels.append(Level(LEVEL_PARAPET, "Đỉnh lan can mái", top, SourceStatus.DERIVED,
                            "roof level + parapet height (working default)"))
        roof["parapet_z"] = top
        roof["parapet_height"] = v["parapet_height"]

    # walls: whole runs between junctions; openings are linked to the run that contains them
    runs = floorplan.wall_runs(plan)
    walls: list[Wall] = []
    for i, run in enumerate(runs, 1):
        walls.append(Wall(f"WL{i:02d}", run.orient, run.line, run.a, run.b, run.thickness, run.exterior,
                          facade_of(run.orient, run.line, run.exterior, w, d, p), LEVEL_FLOOR))

    openings: list[Opening] = []
    counters = {"door": 0, "window": 0, "open": 0}
    for op in plan.openings:
        host = next((wl for wl in walls if wl.orient == op.orient and abs(wl.line - op.line) < floorplan.EPS
                     and wl.a - floorplan.EPS <= op.a and op.b <= wl.b + floorplan.EPS), None)
        if host is None:
            raise HouseModelError(f"{op.kind} at {op.orient}={op.line:g} [{op.a:g}, {op.b:g}] has no host wall run")
        group = "door" if op.kind in ("door", "double") else op.kind
        counters[group] += 1
        prefix = {"door": "D", "window": "W", "open": "P"}[group]
        code = f"{prefix}{counters[group]}"
        width = op.b - op.a
        if op.kind == "window":
            small = width <= 0.7 + floorplan.EPS
            sill = v["small_window_sill"] if small else v["window_sill"]
            height = v["small_window_height"] if small else v["window_height"]
            st = SourceStatus.ASSUMED
        elif op.kind == "open":
            sill, height, st = 0.0, v["door_height"], SourceStatus.ASSUMED
        else:
            sill = 0.0
            height = v["double_door_height"] if op.kind == "double" else v["door_height"]
            st = status("double_door_height" if op.kind == "double" else "door_height")
        openings.append(Opening(code, op.kind, op.orient, op.line, op.a, op.b, op.swing, op.hinge, sill, height,
                                host.id, host.facade, LEVEL_FLOOR, st))
    # the model must not exceed the ceiling
    for o in openings:
        if o.head >= z_ceil:
            raise HouseModelError(f"{o.code} head {o.head:g} reaches the ceiling {z_ceil:g}")

    # rooms
    rooms: list[Room] = []
    annexes: dict[str, list[floorplan.Room]] = {}
    for r in plan.rooms:
        if r.kind == "annex":
            annexes.setdefault(r.name, []).append(r)
    n = 0
    for r in plan.rooms:
        if r.kind == "annex":
            continue
        n += 1
        listed = r.kind != "corridor"
        rooms.append(Room(f"R{n:02d}", r.name, r.kind, _room_boundary(r, annexes.get(r.name, []) if r.kind == "bedroom" else []),
                          listed, LEVEL_FLOOR))

    columns = [(s.values[0], s.values[2]) for s in plan.porch_shapes if s.kind == "rect"]

    model = HouseModel(params=params, plan=plan, levels=levels, walls=walls, openings=openings, rooms=rooms,
                       stairs=[], porch_columns=columns, roof=roof, values=v)
    model.assumptions = _assumption_list(model)
    return model


def _assumption_list(model: HouseModel) -> list[dict[str, Any]]:
    provided = model.params.provided()
    out: list[dict[str, Any]] = []
    for key, (default, unit, why) in WORKING_DEFAULTS.items():
        if key in provided:
            continue
        used = key in ("roof_type", "roof_slope_deg", "roof_overhang") and model.values["roof_type"] != "pitched" \
            and key != "roof_type"
        if key == "parapet_height" and model.values["roof_type"] != "flat":
            continue
        if used:
            continue
        out.append({"id": f"ASM-{key}", "field": key, "value": model.values[key], "unit": unit,
                    "source_status": "ASSUMED", "source": why})
    out.append({"id": "ASM-datum", "field": "datum", "value": "+0.000", "unit": "m", "source_status": "ASSUMED",
                "source": DATUM_NOTE})
    out.append({"id": "ASM-walls", "field": "wall_thickness", "value": {"exterior": floorplan.EXTERIOR_WALL,
                                                                      "interior": floorplan.INTERIOR_WALL},
                "unit": "m", "source_status": "ASSUMED", "source": DEFAULT_NOTE_WALLS})
    return out


def add_stair(model: HouseModel, stair: Stair) -> None:
    """Put a stair into the model (the section draws it when the cut passes through its footprint)."""
    if not (model.has_level(stair.from_level) and model.has_level(stair.to_level)):
        raise HouseModelError(f"stair {stair.id}: unknown level")
    if stair.riser_count < 2 or stair.run_axis not in ("x", "y") or stair.run_sign not in (-1, 1):
        raise HouseModelError(f"stair {stair.id}: needs at least 2 risers, run_axis x|y and run_sign +-1")
    model.stairs.append(stair)
