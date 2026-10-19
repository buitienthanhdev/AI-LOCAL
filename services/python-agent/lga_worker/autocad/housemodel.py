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
from dataclasses import dataclass, field, replace
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
MAX_FLOORS = 4


def floor_level_id(n: int) -> str:
    return f"LV-F{n}"


def ceiling_level_id(n: int) -> str:
    """Ceiling of floor n; floor 1 keeps the id of the single-storey model."""
    return LEVEL_CEILING if n == 1 else f"LV-CEIL{n}"


def plan_sheet_code(n: int) -> str:
    """Default sheet code of the plan of floor n (spec 12: A-101, A-102, ...); the layout tool can override it."""
    return f"A-10{n}"


def floor_number(level_id: str) -> int | None:
    if level_id.startswith("LV-F") and level_id[4:].isdigit():
        return int(level_id[4:])
    return None

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
    floors: int | None = None
    floor_heights: list[float] | None = None
    stair: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None

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
    flights: list[dict[str, Any]] = field(default_factory=list)  # empty: one uniform flight over the whole footprint
    landing: float = 0.0  # depth of the landing between two flights, m
    shape: str = "straight"  # straight | U
    opening: list[tuple[float, float, float, float]] = field(default_factory=list)  # slab opening on the to_level floor
    headroom: float = 2.0
    status: dict[str, str] = field(default_factory=dict)  # field -> PROVIDED | ASSUMED | VERIFIED_PROFILE
    limits: dict[str, Any] = field(default_factory=dict)  # limits applied, with their source


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
    floors: int = 1

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

    @property
    def floor_ids(self) -> list[str]:
        return [floor_level_id(n) for n in range(1, self.floors + 1)]

    def floor_of(self, level_id: str) -> int:
        n = floor_number(level_id)
        return n if n is not None else 1

    def storey_clear(self, n: int) -> float:
        return float(self.values["floor_heights"][n - 1])

    def floor_openings(self, n: int) -> list[Opening]:
        return [o for o in self.openings if o.level_id == floor_level_id(n)]

    def floor_rooms(self, n: int) -> list[Room]:
        return [r for r in self.rooms if r.level_id == floor_level_id(n)]

    @property
    def top_floor_z(self) -> float:
        return self.elevation(floor_level_id(self.floors))

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
                level_id=w.level_id, elevation=self.elevation(w.level_id),
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
                level_id=o.level_id, elevation=self.elevation(o.level_id), properties=props,
                relationships={"host_wall": o.wall_id},
                source=generated, status=o.status))
        for r in self.rooms:
            out.append(Entity(
                r.code, EntityType.ROOM, geometry={"type": "polyline", "points": [list(p) for p in r.boundary],
                                                   "closed": True}, units="m", level_id=r.level_id, elevation=self.elevation(r.level_id),
                properties={"code": r.code, "name": r.name, "kind": r.kind, "stated_area": round(r.area, 4), "area_rule": AREA_RULE,
                            "listed": r.listed},
                source=generated, status=SourceStatus.DERIVED))
        for s in self.stairs:
            out.append(Entity(
                s.id, EntityType.STAIR, units="m", level_id=s.from_level,
                properties={"from_level": s.from_level, "to_level": s.to_level, "total_height": s.total_height,
                            "riser_count": s.riser_count, "riser_height": round(s.total_height / s.riser_count, 6),
                            "tread_depth": s.tread, "width": s.width, "footprint": list(s.footprint),
                            "run_axis": s.run_axis, "run_sign": s.run_sign, "shape": s.shape,
                            "landing_depth": s.landing, "flights": [dict(f) for f in s.flights],
                            "slab_opening": [list(r) for r in s.opening], "headroom_assumed": s.headroom,
                            "setting_status": dict(s.status), "limits": dict(s.limits)},
                source=Source(note="stair designed from the floor-to-floor height (autocad.house_model) or supplied"
                              if s.flights else "stair supplied to the model"),
                status=SourceStatus.DERIVED if s.flights else SourceStatus.PROVIDED))
        z_e = self.elevation(LEVEL_ROOF)
        top_ceiling = ceiling_level_id(self.floors)
        for n in range(2, self.floors + 1):
            opening: list[list[float]] = []
            for st in self.stairs:
                if st.to_level == floor_level_id(n):
                    opening += [list(r) for r in st.opening]
            out.append(Entity(f"SLAB-F{n}", EntityType.SLAB, level_id=floor_level_id(n),
                              elevation=self.elevation(floor_level_id(n)), units="m",
                              properties={"thickness": self.values["slab_thickness"], "role": "floor",
                                          "thickness_status": "ASSUMED", "structural_design": False,
                                          "stair_opening": opening},
                              source=Source(note=WORKING_DEFAULTS["slab_thickness"][2]), status=SourceStatus.ASSUMED))
        out.append(Entity("SLAB-FLOOR", EntityType.SLAB, level_id=LEVEL_FLOOR, elevation=0.0, units="m",
                          properties={"thickness": self.values["slab_thickness"], "role": "floor",
                                      "thickness_status": "ASSUMED", "structural_design": False},
                          source=Source(note=WORKING_DEFAULTS["slab_thickness"][2]), status=SourceStatus.ASSUMED))
        out.append(Entity("SLAB-ROOF", EntityType.SLAB, level_id=top_ceiling,
                          elevation=self.elevation(top_ceiling), units="m",
                          properties={"thickness": z_e - self.elevation(top_ceiling), "role": "ceiling/roof",
                                      "structural_design": False},
                          source=Source(note=WORKING_DEFAULTS["slab_thickness"][2]), status=SourceStatus.ASSUMED))
        out.append(Entity("ROOF-1", EntityType.ROOF, units="m", level_id=LEVEL_ROOF, elevation=z_e,
                          properties=dict(self.roof), source=Source(note="roof parameters are working defaults"),
                          status=SourceStatus.ASSUMED))
        return out

    def floor_meta(self) -> list[dict[str, Any]]:
        return [{"level_id": floor_level_id(n), "ceiling_id": ceiling_level_id(n), "clear_height": self.storey_clear(n),
                 "plan_view": f"PLAN-F{n}", "plan_sheet": plan_sheet_code(n)} for n in range(1, self.floors + 1)]

    def validation_model(self, views: Sequence[Mapping[str, Any]] = (),
                         extra_meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """`autocad.validate_model` input. `views` are the records the draw tools measured from what they drew;
        without them the cross-view rules have nothing to compare and report NOT_RUN. `extra_meta` carries
        measured data such as meta.layouts, meta.sheet_index or meta.pdf_render_check."""
        entities = [e.to_dict() for e in self.entities()]
        schedules: dict[str, list[dict[str, Any]]] = {"door": self.door_rows(), "window": self.window_rows(),
                                                      "room": self.room_rows()}
        records: list[dict[str, Any]] = [
            {"id": f"PLAN-F{n}", "kind": "plan", "code": plan_sheet_code(n), "level_id": floor_level_id(n)}
            for n in range(1, self.floors + 1)]
        sheets = [plan_sheet_code(n) for n in range(1, self.floors + 1)]
        scheds: dict[str, list[dict[str, Any]]] = {}
        for rec in views:
            rec = dict(rec)
            same = next((i for i, r in enumerate(records)
                         if r["id"] == rec.get("id") and r.get("kind") == rec.get("kind")), None)
            if same is not None:
                records[same] = rec
            else:
                records.append(rec)
            if rec.get("sheet"):
                sheets.append(str(rec["sheet"]))
            if rec.get("kind") == "schedule" and rec.get("schedule") in schedules and rec.get("rows") is not None:
                scheds.setdefault(str(rec["schedule"]), []).extend(dict(r) for r in rec["rows"])
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
        schedules.update(scheds)
        meta = {"units": "m", "datum": DATUM_NOTE, "sheets": sorted(set(sheets)), "views": records,
                "area_rule": AREA_RULE, "assumptions": self.assumptions,
                "generator": "lga_worker.autocad.housemodel", "floors": self.floor_meta()}
        meta.update(dict(extra_meta or {}))
        return {"meta": meta, "entities": entities, "schedules": schedules}

    def summary(self) -> dict[str, Any]:
        return {"width": self.width, "depth": self.depth, "porch": self.porch,
                "levels": [{"id": lv.id, "name": lv.name, "elevation": lv.elevation, "status": lv.status.value}
                           for lv in self.levels],
                "walls": len(self.walls), "doors": [o.code for o in self.doors],
                "windows": [o.code for o in self.windows],
                "rooms": [{"code": r.code, "name": r.name, "area_m2": round(r.area, 2)} for r in self.listed_rooms],
                "roof": dict(self.roof), "stairs": [s.id for s in self.stairs], "floors": self.floors,
                "floor_heights": list(self.values["floor_heights"]),
                "stair_detail": [{"id": s.id, "risers": s.riser_count,
                                  "riser_height": round(s.total_height / s.riser_count, 4),
                                  "tread": s.tread, "width": s.width, "shape": s.shape, "landing": s.landing,
                                  "footprint": list(s.footprint), "flights": [dict(f) for f in s.flights]}
                                 for s in self.stairs]}


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
    floors = 1 if params.floors is None else params.floors
    if isinstance(floors, bool) or not isinstance(floors, int) or not 1 <= floors <= MAX_FLOORS:
        raise HouseModelError(f"floors must be a whole number from 1 to {MAX_FLOORS}, got {params.floors!r}")
    values["floors"] = floors
    if params.floor_heights is not None:
        heights = params.floor_heights
        if not isinstance(heights, (list, tuple)) or len(heights) != floors:
            raise HouseModelError(f"floor_heights needs exactly one clear height per floor ({floors}), got {heights!r}")
        values["floor_heights"] = [_clean(h, f"floor_heights[{i}]", 2.2, 6.0) for i, h in enumerate(heights)]
        if "storey_height" not in params.provided():
            values["storey_height"] = values["floor_heights"][0]
    values["storey_height"] = _clean(values["storey_height"], "storey_height", 2.2, 6.0)
    if params.floor_heights is None:
        values["floor_heights"] = [values["storey_height"]] * floors
    elif "storey_height" in params.provided() and abs(values["storey_height"] - values["floor_heights"][0]) > 1e-9:
        raise HouseModelError("storey_height and floor_heights[0] disagree; give only one of them")
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
    lowest = min(values["floor_heights"])
    top_window = values["window_sill"] + values["window_height"]
    if top_window >= lowest:
        raise HouseModelError(f"window sill {values['window_sill']:g} + height {values['window_height']:g} reaches the "
                              f"ceiling ({lowest:g})")
    if max(values["door_height"], values["double_door_height"]) >= lowest:
        raise HouseModelError("a door is as tall as the storey; raise storey_height or lower the door height")
    return values


def model_floor(level_id: str) -> int:
    n = floor_number(level_id)
    return 1 if n is None else n


def build_house_model(params: HouseParams | Mapping[str, Any]) -> HouseModel:
    """The one model. Raises HouseModelError (a ValueError) when the plan or a vertical number is not acceptable.

    floors == 1 (default) is the single-storey model. floors 2..4 repeat the ground-floor wall layout on every floor
    (an ASSUMED layout: upper rooms are the same rectangles, exterior doors become windows) and add a stair, a slab
    opening above it and per-floor codes (D1-T2 = door 1 of floor 2); see `design_stairs`.
    """
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
    floors = v["floors"]
    heights = v["floor_heights"]

    def status(*keys: str) -> SourceStatus:
        return SourceStatus.PROVIDED if all(k in provided for k in keys) else SourceStatus.ASSUMED

    # levels: floor n at the previous ceiling + slab
    slab = v["slab_thickness"]
    height_status = SourceStatus.PROVIDED if ({"storey_height", "floor_heights"} & provided) else SourceStatus.ASSUMED
    height_note = ("argument of the caller" if height_status is SourceStatus.PROVIDED
                   else WORKING_DEFAULTS["storey_height"][2])
    levels = [Level(LEVEL_GROUND, "Cốt sân (đất tự nhiên)", round(-v["plinth_height"], 6), status("plinth_height"),
                    WORKING_DEFAULTS["plinth_height"][2] if "plinth_height" not in provided else "argument of the caller")]
    z = 0.0
    floor_z: dict[int, float] = {}
    ceil_z: dict[int, float] = {}
    for n in range(1, floors + 1):
        floor_z[n] = round(z, 6)
        ceil_z[n] = round(z + heights[n - 1], 6)
        levels.append(Level(floor_level_id(n), f"Sàn tầng {n}", floor_z[n],
                            SourceStatus.ASSUMED if n == 1 else SourceStatus.DERIVED,
                            DATUM_NOTE if n == 1 else f"ceiling of floor {n - 1} + slab thickness (slab is a working default)"))
        levels.append(Level(ceiling_level_id(n), f"Trần tầng {n}", ceil_z[n], height_status, height_note))
        z = ceil_z[n] + slab
    z_roof = round(ceil_z[floors] + slab, 6)
    levels.append(Level(LEVEL_ROOF, "Đỉnh tường / mái", z_roof, SourceStatus.DERIVED,
                        "top ceiling level + slab thickness (working default)"))
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
    for n in range(1, floors + 1):
        sfx = "" if n == 1 else f"-F{n}"
        for i, run in enumerate(runs, 1):
            walls.append(Wall(f"WL{i:02d}{sfx}", run.orient, run.line, run.a, run.b, run.thickness, run.exterior,
                              facade_of(run.orient, run.line, run.exterior, w, d, p), floor_level_id(n)))

    openings: list[Opening] = []
    for n in range(1, floors + 1):
        level_id = floor_level_id(n)
        counters = {"door": 0, "window": 0, "open": 0}
        for op in plan.openings:
            host1 = next((wl for wl in walls if wl.level_id == floor_level_id(1) and wl.orient == op.orient
                          and abs(wl.line - op.line) < floorplan.EPS
                          and wl.a - floorplan.EPS <= op.a and op.b <= wl.b + floorplan.EPS), None)
            if host1 is None:
                raise HouseModelError(f"{op.kind} at {op.orient}={op.line:g} [{op.a:g}, {op.b:g}] has no host wall run")
            host = host1 if n == 1 else next(wl for wl in walls if wl.id == f"{host1.id}-F{n}")
            kind = op.kind
            if n > 1 and kind in ("door", "double") and host.exterior:
                kind = "window"  # no entrance doors on upper floors (ASSUMED; there is no balcony in the model)
            group = "door" if kind in ("door", "double") else kind
            counters[group] += 1
            prefix = {"door": "D", "window": "W", "open": "P"}[group]
            code = f"{prefix}{counters[group]}" if n == 1 else f"{prefix}{counters[group]}-T{n}"
            width = op.b - op.a
            if kind == "window":
                small = width <= 0.7 + floorplan.EPS and op.kind == "window"
                sill = v["small_window_sill"] if small else v["window_sill"]
                height = v["small_window_height"] if small else v["window_height"]
                st = SourceStatus.ASSUMED
            elif kind == "open":
                sill, height, st = 0.0, v["door_height"], SourceStatus.ASSUMED
            else:
                sill = 0.0
                height = v["double_door_height"] if kind == "double" else v["door_height"]
                st = status("double_door_height" if kind == "double" else "door_height")
            openings.append(Opening(code, kind, op.orient, op.line, op.a, op.b, op.swing, op.hinge, sill, height,
                                    host.id, host.facade, level_id, st))
    for o in openings:  # the model must not exceed the ceiling of its floor
        limit = heights[model_floor(o.level_id) - 1]
        if o.head >= limit:
            raise HouseModelError(f"{o.code} head {o.head:g} reaches the ceiling {limit:g}")

    # rooms
    rooms: list[Room] = []
    annexes: dict[str, list[floorplan.Room]] = {}
    for r in plan.rooms:
        if r.kind == "annex":
            annexes.setdefault(r.name, []).append(r)
    for n in range(1, floors + 1):
        k = 0
        for r in plan.rooms:
            if r.kind == "annex":
                continue
            k += 1
            listed = r.kind != "corridor"
            code = f"R{k:02d}" if n == 1 else f"T{n}-R{k:02d}"
            name = r.name if n == 1 else f"{r.name} (T{n})"
            rooms.append(Room(code, name, r.kind, _room_boundary(r, annexes.get(r.name, []) if r.kind == "bedroom" else []),
                              listed, floor_level_id(n)))

    columns = [(s.values[0], s.values[2]) for s in plan.porch_shapes if s.kind == "rect"]

    model = HouseModel(params=params, plan=plan, levels=levels, walls=walls, openings=openings, rooms=rooms,
                       stairs=[], porch_columns=columns, roof=roof, values=v, floors=floors)
    if floors > 1:
        model.stairs = design_stairs(model)
    model.assumptions = _assumption_list(model)
    return model


# --------------------------------------------------------------------------- stairs

STAIR_DEFAULTS: dict[str, tuple[Any, str, str]] = {
    "riser_target": (0.17, "m", "target riser height used to choose the riser count (ceil of total height / target); working default, not a code limit"),
    "tread": (0.25, "m", "tread depth; working default, not a code limit"),
    "width": (0.9, "m", "flight width; working default, not a code limit"),
    "landing": (None, "m", "landing depth between two flights; default = flight width"),
    "flights": (2, "", "2 = U-shaped stair with a landing (stacks over several floors); 1 = straight (two floors only)"),
    "headroom": (2.0, "m", "clear height assumed above the flight to size the slab opening; working default, not a code limit"),
    "gap": (0.1, "m", "well between the two flights of a U stair; working default"),
    "room": ("living", "", "kind of room that gets the stair"),
}
LIMIT_FIELDS = ("max_riser_height", "min_tread_depth", "min_stair_width")
STAIR_EPS = 1e-6


def stair_limits(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Limits from the profile argument. A limit counts only when its entry carries `verified_by`; anything else is
    reported as ASSUMED and is NOT applied as a requirement."""
    from lga_worker.autocad.standards import _unwrap  # noqa: PLC0415

    applied: dict[str, Any] = {}
    unverified: dict[str, Any] = {}
    for key in LIMIT_FIELDS:
        value, _status, wrapper = _unwrap((profile or {}).get(key))
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            continue
        if str(wrapper.get("verified_by") or "").strip():
            applied[key] = {"value": float(value), "verified_by": str(wrapper["verified_by"]), "source": "VERIFIED_PROFILE"}
        else:
            unverified[key] = {"value": float(value), "source": "ASSUMED (profile entry without verified_by; not applied)"}
    return {"applied": applied, "unverified": unverified}


def stair_flights(total: float, *, tread: float, width: float, landing: float, flights: int, riser_target: float,
                  max_riser: float | None = None) -> dict[str, Any]:
    """Riser count and flights from the floor-to-floor height. n = ceil(total / target); riser = total / n exactly."""
    target = riser_target if max_riser is None else min(riser_target, max_riser)
    n = max(2, math.ceil(total / target - 1e-9))
    riser = total / n
    if flights == 1:
        parts = [{"risers": n, "treads": n - 1}]
    else:
        n1 = math.ceil(n / 2)
        parts = [{"risers": n1, "treads": n1 - 1}, {"risers": n - n1, "treads": n - n1 - 1}]
        if parts[1]["risers"] < 2:
            raise HouseModelError(f"a two-flight stair needs at least 4 risers, the height {total:g} m gives {n}")
    for part in parts:
        part["run"] = round(part["treads"] * tread, 6)
        part["rise"] = round(part["risers"] * riser, 6)
    depth = max(p["run"] for p in parts) + (landing if flights == 2 else 0.0)
    return {"riser_count": n, "riser_height": riser, "flights": parts, "footprint_depth": depth}


def design_stairs(model: HouseModel) -> list[Stair]:
    """One stair per floor pair, all in the same footprint inside one room (default: the living room)."""
    raw = dict(model.params.stair or {})
    unknown = set(raw) - {"riser_target", "tread", "width", "landing", "flights", "headroom", "room", "position"}
    if unknown:
        raise HouseModelError(f"unknown stair setting(s) {sorted(unknown)}")
    limits = stair_limits(model.params.profile)
    max_riser = limits["applied"].get("max_riser_height", {}).get("value")
    min_tread = limits["applied"].get("min_tread_depth", {}).get("value")
    min_width = limits["applied"].get("min_stair_width", {}).get("value")
    status: dict[str, str] = {}
    val: dict[str, Any] = {}
    for key, (default, _unit, _why) in STAIR_DEFAULTS.items():
        if key in raw and raw[key] is not None:
            val[key] = raw[key]
            status[key] = "PROVIDED"
        else:
            val[key] = default
            status[key] = "ASSUMED"
    for key in ("riser_target", "tread", "width", "headroom", "gap"):
        val[key] = _clean(val[key], f"stair.{key}", 0.05 if key == "gap" else 0.1, 3.0)
    if isinstance(val["flights"], bool) or val["flights"] not in (1, 2):
        raise HouseModelError("stair.flights must be 1 or 2")
    if val["flights"] == 1 and model.floors > 2:
        raise HouseModelError("a straight (1 flight) stair cannot be stacked over 3 or more floors: the arrival of one "
                              "flight and the start of the next would share the same slab opening; use flights=2")
    val["landing"] = _clean(val["landing"] if val["landing"] is not None else val["width"], "stair.landing", 0.3, 4.0)
    if max_riser is not None and "riser_target" in raw and raw["riser_target"] > max_riser + STAIR_EPS:
        raise HouseModelError(f"stair.riser_target {raw['riser_target']:g} exceeds the verified profile limit "
                              f"max_riser_height {max_riser:g}")
    if min_tread is not None and val["tread"] < min_tread - STAIR_EPS:
        raise HouseModelError(f"stair.tread {val['tread']:g} is below the verified profile limit min_tread_depth {min_tread:g}")
    if min_width is not None and val["width"] < min_width - STAIR_EPS:
        raise HouseModelError(f"stair.width {val['width']:g} is below the verified profile limit min_stair_width {min_width:g}")

    flights = int(val["flights"])
    width = float(val["width"])
    landing = float(val["landing"])
    foot_w = width if flights == 1 else 2 * width + float(val["gap"])
    # all stairs share one footprint, so it is sized for the tallest floor-to-floor height
    pairs = []
    for n in range(1, model.floors):
        total = model.elevation(floor_level_id(n + 1)) - model.elevation(floor_level_id(n))
        pairs.append((n, total, stair_flights(total, tread=float(val["tread"]), width=width, landing=landing,
                                              flights=flights, riser_target=float(val["riser_target"]),
                                              max_riser=max_riser)))
    foot_d = max(p[2]["footprint_depth"] for p in pairs)
    x0, y0, x1, y1, sign = _stair_place(model, str(val["room"]), foot_w, foot_d, raw.get("position"))
    stairs: list[Stair] = []
    for n, total, design in pairs:
        parts = design["flights"]
        riser = design["riser_height"]
        tread = float(val["tread"])
        a_clear = max(0.0, math.floor(max(0.0, (model.storey_clear(n) - float(val["headroom"])) / riser)) * tread)
        a_clear = min(a_clear, parts[0]["run"])
        stairs.append(Stair(
            f"ST{n}", floor_level_id(n), floor_level_id(n + 1), round(total, 6), design["riser_count"], tread, width,
            (x0, y0, x1, y1), "y", sign, flights=[dict(p) for p in parts], landing=landing if flights == 2 else 0.0,
            shape="U" if flights == 2 else "straight",
            opening=_stair_opening((x0, y0, x1, y1), sign, width, flights, parts, tread, a_clear),
            headroom=float(val["headroom"]), status=dict(status),
            limits={"applied": limits["applied"], "unverified": limits["unverified"]}))
    return stairs


def _stair_opening(foot: tuple[float, float, float, float], sign: int, width: float, flights: int,
                   parts: list[dict[str, Any]], tread: float, a_clear: float) -> list[tuple[float, float, float, float]]:
    """Slab opening on the upper floor: the arrival side from where the arrival flight meets the floor, the other side
    from where the headroom above the first flight is reached. Rectangles in plan metres."""
    x0, y0, x1, y1 = foot
    near, far = (y0, y1) if sign > 0 else (y1, y0)

    def span(a: float, b: float) -> tuple[float, float]:
        return (min(a, b), max(a, b))

    if flights == 1:
        lo, hi = span(near + sign * a_clear, far)
        return [(x0, lo, x1, hi)]
    gap_to_top = (parts[0]["risers"] - parts[1]["risers"]) * tread  # the shorter arrival flight stops this far from the floor edge
    lo_b, hi_b = span(near + sign * gap_to_top, far)
    lo_a, hi_a = span(near + sign * a_clear, far)
    return [(x0 + width, lo_b, x1, hi_b), (x0, lo_a, x0 + width, hi_a)]


def _stair_place(model: HouseModel, room_kind: str, w: float, d: float, position: Any
                 ) -> tuple[float, float, float, float, int]:
    room = next((r for r in model.rooms if r.kind == room_kind and r.level_id == floor_level_id(1)), None)
    if room is None:
        raise HouseModelError(f"no '{room_kind}' room for the stair; the plan has "
                              f"{sorted({r.kind for r in model.rooms})}")
    rx0, ry0, rx1, ry1 = room.bbox
    m = 0.15  # half an exterior wall + a little clearance
    if rx1 - rx0 < w + 2 * m or ry1 - ry0 < d + 2 * m:
        raise HouseModelError(f"the stair needs {w:.2f} x {d:.2f} m but the {room_kind} room is only "
                              f"{rx1 - rx0:.2f} x {ry1 - ry0:.2f} m between wall centre lines; reduce stair width/tread "
                              "or use flights=2, or choose another room (stair.room)")
    first = model.floor_openings(1)

    def clear(x0: float, y0: float, x1: float, y1: float) -> bool:
        for o in first:
            if o.kind == "window":
                continue
            # door/passage: keep its swing zone (0.9 m) free
            if o.orient == "h":
                ox0, ox1, oy0, oy1 = o.a - 0.1, o.b + 0.1, o.line - 0.9, o.line + 0.9
            else:
                ox0, ox1, oy0, oy1 = o.line - 0.9, o.line + 0.9, o.a - 0.1, o.b + 0.1
            if x0 < ox1 and ox0 < x1 and y0 < oy1 and oy0 < y1:
                return False
        return True

    if isinstance(position, Mapping):
        px, py = float(position.get("x", rx0 + m)), float(position.get("y", ry0 + m))
        cands = [(px, py, 1 if position.get("from", "front") == "front" else -1)]
    else:
        cands = [(rx1 - m - w, ry1 - m - d, 1), (rx0 + m, ry1 - m - d, 1), (rx1 - m - w, ry0 + m, -1), (rx0 + m, ry0 + m, -1)]
    for x0, y0, sgn in cands:
        if x0 >= rx0 + m - 1e-9 and y0 >= ry0 + m - 1e-9 and x0 + w <= rx1 - m + 1e-9 and y0 + d <= ry1 - m + 1e-9 \
                and clear(x0, y0, x0 + w, y0 + d):
            return x0, y0, x0 + w, y0 + d, sgn
    raise HouseModelError(f"no clear corner in the {room_kind} room for a {w:.2f} x {d:.2f} m stair (doors and passages "
                          "would be blocked); pass stair.position {x, y} or stair.room")


def _assumption_list(model: HouseModel) -> list[dict[str, Any]]:
    provided = model.params.provided()
    out: list[dict[str, Any]] = []
    for key, (default, unit, why) in WORKING_DEFAULTS.items():
        if key in provided or (key == "storey_height" and "floor_heights" in provided):
            continue
        used = key in ("roof_type", "roof_slope_deg", "roof_overhang") and model.values["roof_type"] != "pitched" \
            and key != "roof_type"
        if key == "parapet_height" and model.values["roof_type"] != "flat":
            continue
        if used:
            continue
        out.append({"id": f"ASM-{key}", "field": key, "value": model.values[key], "unit": unit,
                    "source_status": "ASSUMED", "source": why})
    if model.floors > 1:
        out.append({"id": "ASM-upper-floors", "field": "upper_floor_layout", "value": f"{model.floors} floors",
                    "unit": "", "source_status": "ASSUMED",
                    "source": "upper floors repeat the ground-floor walls, rooms and interior openings; exterior doors "
                              "become windows; plan_house is a single-storey generator, not an architect's "
                              "multi-storey design"})
        for st in model.stairs[:1]:
            for key, how in st.status.items():
                if how == "ASSUMED":
                    out.append({"id": f"ASM-stair-{key}", "field": f"stair.{key}",
                                "value": STAIR_DEFAULTS[key][0] if key != "landing" else st.landing,
                                "unit": STAIR_DEFAULTS[key][1], "source_status": "ASSUMED",
                                "source": STAIR_DEFAULTS[key][2]})
            for key, info in st.limits.get("unverified", {}).items():
                out.append({"id": f"ASM-profile-{key}", "field": f"profile.{key}", "value": info["value"], "unit": "m",
                            "source_status": "ASSUMED", "source": info["source"]})
            if not st.limits.get("applied"):
                out.append({"id": "ASM-stair-limits", "field": "stair_limits", "value": None, "unit": "",
                            "source_status": "ASSUMED",
                            "source": "no verified profile limit (max_riser_height / min_tread_depth / min_stair_width) "
                                      "was given: the stair is sized with working defaults and NOT checked against "
                                      "any code limit"})
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


def _shape_box(sh: floorplan.Shape) -> tuple[float, float, float, float]:
    v = sh.values
    if sh.kind in ("circle", "arc"):
        return v[0] - v[2], v[1] - v[2], v[0] + v[2], v[1] + v[2]
    return min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])


def floor_plan(model: HouseModel, floor: int) -> floorplan.Plan:
    """The plan geometry to draw for `floor` (1-based). Floor 1 of a single-storey house is `model.plan` itself.

    For a multi-storey house: furniture that would lie under the stair is dropped on every floor; floors 2.. have no porch,
    exterior doors become windows (as in the model) and the labels and titles name the floor.
    """
    if model.floors == 1:
        return model.plan
    plan = model.plan
    furniture = list(plan.furniture)
    if model.stairs:
        x0, y0, x1, y1 = model.stairs[0].footprint
        furniture = [sh for sh in furniture
                     if not (_shape_box(sh)[0] < x1 + 0.1 and _shape_box(sh)[2] > x0 - 0.1
                             and _shape_box(sh)[1] < y1 + 0.1 and _shape_box(sh)[3] > y0 - 0.1)]
    if floor == 1:
        return replace(plan, furniture=furniture,
                       title=replace(plan.title, text=plan.title.text.replace("MẶT BẰNG NHÀ CẤP 4", "MẶT BẰNG TẦNG 1")))
    ops = [floorplan.Opening(o.orient, o.line, o.a, o.b, o.kind, o.swing, o.hinge) for o in model.floor_openings(floor)]
    n = floor
    labels = [replace(lb, text=lb.text.replace("\\P", f" (T{n})\\P", 1)) for lb in plan.labels]
    rows = [r for r in model.floor_rooms(floor) if r.listed]
    lines = [f"{i}. {r.name}: " + f"{r.area:.2f}".replace(".", ",") + " m²" for i, r in enumerate(rows, 1)]
    schedule = replace(plan.schedule, text=f"BẢNG DIỆN TÍCH TẦNG {n} (tim tường)\\P" + "\\P".join(lines))
    title = replace(plan.title, text=f"MẶT BẰNG TẦNG {n}\\PKích thước {plan.width:g} x {plan.depth:g} m")
    return replace(plan, openings=ops, furniture=furniture, porch_shapes=[], labels=labels, title=title,
                   schedule=schedule)
