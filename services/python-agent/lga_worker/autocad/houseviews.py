"""Elevations, sections, section markers and schedule tables of a `HouseModel`, as drawing primitives (spec 8-11).

Pure Python: every function returns `Prim` lists in view-local metres (h to the right, z up, +0.000 = floor datum), model
space 1:1, each primitive on a standard A-* layer name (the provider maps it with `layers.LayerMap`, so an office
template can rename layers). Colour, linetype and lineweight are never set per entity: everything is ByLayer.

Each view also returns a `record` that is MEASURED FROM THE PRIMITIVES it just built (opening rectangles, level lines and
their label texts, clear-height dimensions, table cells), not copied from the model. The validation rules compare these
records with the plan data, so a drawing step that moves or mislabels something is caught.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lga_worker.autocad import floorplan
from lga_worker.autocad.housemodel import (
    LEVEL_CEILING,
    LEVEL_FLOOR,
    LEVEL_GROUND,
    LEVEL_PARAPET,
    LEVEL_RIDGE,
    LEVEL_ROOF,
    SIDES,
    HouseModel,
    Opening,
    ceiling_level_id,
    floor_level_id,
    floor_number,
    plan_sheet_code,
)

EPS = 1e-6
SIDE_SHEET = {"front": "A-201", "rear": "A-202", "left": "A-203", "right": "A-204"}
SIDE_TITLE = {"front": "MẶT ĐỨNG CHÍNH", "rear": "MẶT ĐỨNG SAU", "left": "MẶT ĐỨNG TRÁI", "right": "MẶT ĐỨNG PHẢI"}
SECTION_SHEET = "A-301"
DOOR_SCHEDULE_SHEET = "A-501"
ROOM_SCHEDULE_SHEET = "A-503"
# AutoCAD MText attachment points
TL, TC, TR, ML, MC, MR, BL, BC, BR = range(1, 10)

WALL_HATCH = "ANSI31"
SLAB_HATCH = "SOLID"


@dataclass
class Prim:
    kind: str  # line | poly | circle | arc | text | dim | cut
    layer: str  # standard layer name (A-*)
    pts: tuple[float, ...]
    text: str = ""
    height: float = 0.0
    closed: bool = False
    attach: int = TL
    pattern: str = ""  # cut: hatch pattern of the closed polygon
    semantic: str = ""
    ref: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def expected_entities(self) -> int:
        return 2 if self.kind == "cut" else 1

    def xy(self) -> list[tuple[float, float]]:
        n = {"line": 4, "circle": 2, "arc": 2, "text": 2, "dim": 6}.get(self.kind)
        coords = self.pts[:n] if n else self.pts
        if self.kind == "dim":
            coords = self.pts
        return [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]


@dataclass
class View:
    id: str
    kind: str  # elevation | section | schedule | marker
    sheet: str
    title: str
    prims: list[Prim]
    record: dict[str, Any]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        pts = [p for prim in self.prims for p in prim.xy()]
        if not pts:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def expected_entities(self) -> int:
        return sum(p.expected_entities() for p in self.prims)

    def layers_used(self) -> list[str]:
        return sorted({p.layer for p in self.prims})


def level_label(z: float) -> str:
    z = 0.0 if abs(z) < 5e-4 else z
    return f"{z:+.3f}"


def parse_level_label(text: str) -> float | None:
    cleaned = text.strip().replace("±", "+").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def fmt2(value: float) -> str:
    """Two decimals with a decimal comma, the way the plan prints its areas."""
    return f"{value:.2f}".replace(".", ",")


def parse_number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


class _Draft:
    def __init__(self, text_h: float) -> None:
        self.prims: list[Prim] = []
        self.th = text_h

    def line(self, layer: str, x0: float, y0: float, x1: float, y1: float, **kw: Any) -> Prim:
        p = Prim("line", layer, (x0, y0, x1, y1), **kw)
        self.prims.append(p)
        return p

    def poly(self, layer: str, pts: Sequence[tuple[float, float]], closed: bool = False, **kw: Any) -> Prim:
        p = Prim("poly", layer, tuple(c for pt in pts for c in pt), closed=closed, **kw)
        self.prims.append(p)
        return p

    def rect(self, layer: str, x0: float, y0: float, x1: float, y1: float, **kw: Any) -> Prim:
        return self.poly(layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True, **kw)

    def cut(self, layer: str, pts: Sequence[tuple[float, float]], pattern: str, **kw: Any) -> Prim:
        p = Prim("cut", layer, tuple(c for pt in pts for c in pt), closed=True, pattern=pattern, **kw)
        self.prims.append(p)
        return p

    def text(self, layer: str, x: float, y: float, text: str, height: float | None = None, attach: int = TL,
             **kw: Any) -> Prim:
        p = Prim("text", layer, (x, y), text=text, height=height or self.th, attach=attach, **kw)
        self.prims.append(p)
        return p

    def dim(self, x1: float, y1: float, x2: float, y2: float, tx: float, ty: float, **kw: Any) -> Prim:
        p = Prim("dim", "A-DIMS", (x1, y1, x2, y2, tx, ty), **kw)
        self.prims.append(p)
        return p

    def circle(self, layer: str, cx: float, cy: float, r: float, **kw: Any) -> Prim:
        p = Prim("circle", layer, (cx, cy, r), **kw)
        self.prims.append(p)
        return p


def _bbox_of(prim: Prim) -> tuple[float, float, float, float]:
    pts = prim.xy()
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


# --------------------------------------------------------------------------- level marks (shared by elevation and section)

def _level_mark(d: _Draft, model: HouseModel, level_id: str, x_line_start: float, x_line_end: float) -> None:
    z = model.elevation(level_id)
    d.line("A-ANNO-LEVEL", x_line_start, z, x_line_end, z, semantic="level_line", ref=level_id)
    tx = x_line_start + 0.45
    d.poly("A-ANNO-LEVEL", [(tx - 0.15, z + 0.25), (tx + 0.15, z + 0.25), (tx, z)], True, semantic="level_symbol",
           ref=level_id)
    d.text("A-ANNO-LEVEL", tx + 0.25, z + 0.04, level_label(z), d.th * 0.9, BL, semantic="level_label", ref=level_id)


def _measure_levels(prims: Sequence[Prim]) -> list[dict[str, Any]]:
    lines = {p.ref: p for p in prims if p.semantic == "level_line"}
    labels = {p.ref: p for p in prims if p.semantic == "level_label"}
    out = []
    for level_id, line in lines.items():
        label = labels.get(level_id)
        out.append({"level_id": level_id, "elevation": round(line.pts[1], 6),
                    "label": label.text if label else None,
                    "label_value": parse_level_label(label.text) if label else None})
    return out


# --------------------------------------------------------------------------- elevations

def _hmap(side: str, w: float, d: float) -> Callable[[float, float], float]:
    return {"front": lambda x, y: x, "rear": lambda x, y: w - x, "left": lambda x, y: d - y,
            "right": lambda x, y: y}[side]


def _opening_h(model: HouseModel, o: Opening, side: str) -> tuple[float, float]:
    h = _hmap(side, model.width, model.depth)
    pair = (h(o.a, 0.0), h(o.b, 0.0)) if side in ("front", "rear") else (h(0.0, o.a), h(0.0, o.b))
    return min(pair), max(pair)


def elevation_view(model: HouseModel, side: str) -> View:
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    w, dep, p = model.width, model.depth, model.porch
    t = floorplan.EXTERIOR_WALL
    h = _hmap(side, w, dep)
    z_g = model.elevation(LEVEL_GROUND)
    z_e = model.elevation(LEVEL_ROOF)
    pitched = model.roof["type"] == "pitched"
    ov = model.roof["overhang"]
    d = _Draft(model.text_height * 0.9)
    gable = side in ("front", "rear")
    if gable:
        lo, hi = -t / 2, w + t / 2
    else:
        lo, hi = sorted((h(0.0, p - t / 2), h(0.0, dep + t / 2)))
    span_lo, span_hi = (-ov, w + ov) if gable else (-ov, dep + ov)
    mid = (lo + hi) / 2

    d.line("A-SITE-CONTEXT", min(lo, span_lo) - 3.4, z_g, max(hi, span_hi) + 3.4, z_g, semantic="ground")
    # body and roof
    if pitched and gable:
        z_r = model.elevation(LEVEL_RIDGE)
        d.poly("A-WALL-EXT", [(lo, z_g), (hi, z_g), (hi, model.roof_z_at(hi)), (w / 2, z_r), (lo, model.roof_z_at(lo))],
               True, semantic="body")
        d.poly("A-ROOF", [(-ov, model.roof_z_at(-ov)), (w / 2, z_r), (w + ov, model.roof_z_at(w + ov))],
               semantic="roof")
        top_level = LEVEL_RIDGE
    elif pitched:
        z_r = model.elevation(LEVEL_RIDGE)
        d.rect("A-WALL-EXT", lo, z_g, hi, z_e, semantic="body")
        d.rect("A-ROOF", span_lo, model.roof_z_at(-ov), span_hi, z_r, semantic="roof")
        top_level = LEVEL_RIDGE
    else:
        z_p = model.elevation(LEVEL_PARAPET)
        d.rect("A-WALL-EXT", lo, z_g, hi, z_p, semantic="body")
        d.line("A-ROOF", lo, z_e, hi, z_e, semantic="roof")
        top_level = LEVEL_PARAPET
    # lines of the floor slabs on a multi-storey facade
    for n in range(2, model.floors + 1):
        for lid_ in (ceiling_level_id(n - 1), floor_level_id(n)):
            zl = model.elevation(lid_)
            d.line("A-WALL-EXT", lo, zl, hi, zl, semantic="floor_line", ref=lid_)
    z_post = model.elevation(floor_level_id(2)) if model.floors > 1 else z_e
    # porch posts: visible in the front and the side views
    if side != "rear":
        for x0, x1 in model.porch_columns:
            if side == "front":
                a, b = x0, x1
            else:
                a, b = sorted((h(0.0, 0.0), h(0.0, 0.3)))
            d.rect("A-COLUMN-ARCH", a, z_g, b, z_post, semantic="porch_post")
            if side != "front":
                break
    # openings of this facade, from the same records as the plan symbols and the schedules
    for o in model.openings:
        if o.etype == "OPENING" or o.facade != side:
            continue
        a, b = _opening_h(model, o, side)
        layer = "A-WINDOW" if o.etype == "WINDOW" else "A-DOOR"
        zf = model.elevation(o.level_id)
        d.rect(layer, a, zf + o.sill, b, zf + o.head, semantic="opening", ref=o.code,
               data={"type": o.etype, "floor_z": zf, "level_id": o.level_id})
        if o.etype == "WINDOW":
            d.line("A-GLAZING", (a + b) / 2, zf + o.sill, (a + b) / 2, zf + o.head, semantic="glazing", ref=o.code)
        elif o.kind == "double":
            d.line("A-DOOR", (a + b) / 2, zf + o.sill, (a + b) / 2, zf + o.head, semantic="leaf_split", ref=o.code)
        d.text("A-ANNO-TEXT", (a + b) / 2, zf + o.head + 0.1, o.code, d.th * 0.85, BC, semantic="opening_tag", ref=o.code)
    # levels, on the left of the building
    x1 = lo - 0.15
    x0 = lo - 3.4
    for level_id in (LEVEL_GROUND, *model.floor_ids, LEVEL_ROOF, top_level):
        _level_mark(d, model, level_id, x0, x1)
    # dimensions
    xd = hi + 1.2
    z_top = model.elevation(top_level)
    chain = [z_g] + [model.elevation(i) for i in model.floor_ids] + [z_e, z_top]
    for za, zb in zip(chain, chain[1:]):
        if zb - za > 0.05:
            d.dim(hi + 0.3, za, hi + 0.3, zb, xd, (za + zb) / 2)
    d.dim(lo, z_g, hi, z_g, mid, z_g - 1.1)
    title_y = z_g - 2.4
    d.text("A-ANNO-TEXT", mid, title_y, f"{SIDE_TITLE[side]}\\PTỶ LỆ VẼ 1:1 (in 1:100) - {SIDE_SHEET[side]}",
           model.text_height * 1.4, TC, semantic="title")

    record = {"id": f"ELV-{side.upper()}", "kind": "elevation", "sheet": SIDE_SHEET[side], "side": side,
              "label": SIDE_TITLE[side], "levels": _measure_levels(d.prims), "openings": _measure_openings(d.prims, side)}
    return View(record["id"], "elevation", SIDE_SHEET[side], SIDE_TITLE[side], d.prims, record)


def _measure_openings(prims: Sequence[Prim], side: str) -> list[dict[str, Any]]:
    out = []
    for prim in prims:
        if prim.semantic != "opening":
            continue
        x0, z0, x1, z1 = _bbox_of(prim)
        out.append({"code": prim.ref, "type": prim.data.get("type"), "width": round(x1 - x0, 6),
                    "height": round(z1 - z0, 6), "sill_height": round(z0 - prim.data.get("floor_z", 0.0), 6),
                    "facade": side, "level_id": prim.data.get("level_id")})
    return out


# --------------------------------------------------------------------------- sections

@dataclass
class Cut:
    axis: str = "y"  # "y": the cut line runs along x at y = position; "x": along y at x = position
    position: float | None = None
    direction: int = 1  # +1 looks towards +y (axis y) or +x (axis x)
    letter: str = "A"
    sheet: str = SECTION_SHEET

    @property
    def view_id(self) -> str:
        return f"SEC-{self.letter}-{self.letter}"

    @property
    def code(self) -> str:
        return f"{self.letter}-{self.letter}"


def default_cut(model: HouseModel) -> Cut:
    if model.stairs:  # through the middle of the first flight, parallel to the run: the section shows every floor and the stair
        st = model.stairs[0]
        x0, y0, x1, y1 = st.footprint
        return Cut("x", round(x0 + st.width / 2, 4), 1)
    living = next((r for r in model.rooms if r.kind == "living"), model.rooms[0])
    x0, y0, x1, y1 = living.bbox
    return Cut("y", round((y0 + y1) / 2, 4), 1)


def check_cut(model: HouseModel, cut: Cut) -> Cut:
    """The cut with its position filled in; ValueError when it is not usable."""
    if cut.axis not in ("x", "y"):
        raise ValueError(f"cut axis must be 'x' or 'y', got {cut.axis!r}")
    if cut.direction not in (-1, 1):
        raise ValueError("cut direction must be +1 or -1")
    if not cut.letter.strip() or len(cut.letter) > 3:
        raise ValueError("cut letter must be 1 to 3 characters")
    pos = default_cut(model).position if cut.position is None else float(cut.position)
    if cut.position is None and cut.axis == "x" and not model.stairs:
        pos = model.width / 2
    if cut.position is None and cut.axis == "y" and model.stairs and default_cut(model).axis == "x":
        x0, y0, x1, y1 = model.stairs[0].footprint
        pos = round((y0 + y1) / 2, 4)
    limit = model.depth if cut.axis == "y" else model.width
    if not 0.0 < pos < limit:
        raise ValueError(f"cut position {pos:g} must lie inside the house (0 to {limit:g} m)")
    parallel = "h" if cut.axis == "y" else "v"
    for wl in model.walls:
        if wl.orient == parallel and abs(wl.line - pos) < wl.thickness / 2 + 0.05:
            raise ValueError(f"the cut at {cut.axis}={pos:g} runs inside the wall {wl.id} (centre line {wl.line:g}, "
                             f"thickness {wl.thickness:g}); move it by at least {wl.thickness / 2 + 0.05:g} m")
    return Cut(cut.axis, round(pos, 6), cut.direction, cut.letter.strip(), cut.sheet or SECTION_SHEET)


def _u_to_h(model: HouseModel, cut: Cut) -> Callable[[float], float]:
    w, dep = model.width, model.depth
    if cut.axis == "y":
        return (lambda u: u) if cut.direction > 0 else (lambda u: w - u)
    return (lambda u: dep - u) if cut.direction > 0 else (lambda u: u)


def _merge_intervals(items: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(items):
        if out and a <= out[-1][1] + EPS:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _opening_intervals(model: HouseModel, cut: Cut, pos: float, to_level: str) -> list[tuple[float, float]]:
    """Intervals along the cut (plan u coordinate) where the floor slab of `to_level` is open for a stair."""
    found: list[tuple[float, float]] = []
    for st in model.stairs:
        if st.to_level != to_level:
            continue
        for x0, y0, x1, y1 in st.opening:
            if cut.axis == "y" and y0 < pos < y1:
                found.append((x0, x1))
            elif cut.axis == "x" and x0 < pos < x1:
                found.append((y0, y1))
    return _merge_intervals(found)


def _subtract(lo: float, hi: float, holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pieces: list[tuple[float, float]] = []
    cur = lo
    for a, b in holes:
        if b <= cur + EPS or a >= hi - EPS:
            continue
        if a > cur + EPS:
            pieces.append((cur, a))
        cur = max(cur, b)
    if hi > cur + EPS:
        pieces.append((cur, hi))
    return pieces


def section_view(model: HouseModel, cut: Cut) -> View:
    cut = check_cut(model, cut)
    pos = cut.position
    assert pos is not None
    w, dep = model.width, model.depth
    H = _u_to_h(model, cut)
    perp = "v" if cut.axis == "y" else "h"
    z_g = model.elevation(LEVEL_GROUND)
    top_ceiling = ceiling_level_id(model.floors)
    z_c = model.elevation(top_ceiling)
    z_e = model.elevation(LEVEL_ROOF)
    slab = model.values["slab_thickness"]
    ov = model.roof["overhang"]
    pitched = model.roof["type"] == "pitched"
    d = _Draft(model.text_height * 0.9)

    crossing = [wl for wl in model.walls if wl.orient == perp and wl.a < pos < wl.b and wl.level_id == LEVEL_FLOOR]
    if not crossing:
        raise ValueError(f"the cut at {cut.axis}={pos:g} crosses no wall")
    lo_u = min(wl.line - wl.thickness / 2 for wl in crossing)
    hi_u = max(wl.line + wl.thickness / 2 for wl in crossing)

    def h_range(u0: float, u1: float) -> tuple[float, float]:
        a, b = H(u0), H(u1)
        return min(a, b), max(a, b)

    # slabs: ground slab, one slab per upper floor (open where a stair passes), roof slab
    fa, fb = h_range(lo_u, hi_u)
    d.cut("A-SECTION-CUT", [(fa, -slab), (fb, -slab), (fb, 0.0), (fa, 0.0)], SLAB_HATCH, semantic="slab_floor", ref="SLAB-FLOOR")
    for n in range(2, model.floors + 1):
        z_below = model.elevation(ceiling_level_id(n - 1))
        holes = _opening_intervals(model, cut, pos, floor_level_id(n))
        for u0, u1 in _subtract(lo_u, hi_u, holes):
            a, b = h_range(u0, u1)
            d.cut("A-SECTION-CUT", [(a, z_below), (b, z_below), (b, z_below + slab), (a, z_below + slab)], SLAB_HATCH,
                  semantic="slab_floor", ref=f"SLAB-F{n}")
    d.cut("A-SECTION-CUT", [(fa, z_c), (fb, z_c), (fb, z_e), (fa, z_e)], SLAB_HATCH, semantic="slab_roof", ref="SLAB-ROOF")
    # cut walls of every floor, split around the openings they carry
    for n in range(1, model.floors + 1):
        lid = floor_level_id(n)
        zf_n, zc_n = model.elevation(lid), model.elevation(ceiling_level_id(n))
        for wl in [wl for wl in model.walls if wl.orient == perp and wl.a < pos < wl.b and wl.level_id == lid]:
            a, b = h_range(wl.line - wl.thickness / 2, wl.line + wl.thickness / 2)
            gaps = sorted((zf_n + o.sill, zf_n + o.head, o.code) for o in model.openings
                          if o.level_id == lid and o.orient == wl.orient and abs(o.line - wl.line) < EPS and o.a < pos < o.b)
            z_bottom = z_g if (wl.exterior and n == 1) else zf_n
            z = z_bottom
            pieces: list[tuple[float, float]] = []
            for sill, head, _code in gaps:
                if sill > z + EPS:
                    pieces.append((z, sill))
                z = max(z, head)
            if zc_n > z + EPS:
                pieces.append((z, zc_n))
            for z0, z1 in pieces:
                d.cut("A-SECTION-CUT", [(a, z0), (b, z0), (b, z1), (a, z1)], WALL_HATCH, semantic="cut_wall", ref=wl.id)
            for sill, head, code in gaps:
                d.line("A-SECTION-PROJ", a, sill, b, sill, semantic="opening_sill", ref=code)
                d.line("A-SECTION-PROJ", a, head, b, head, semantic="opening_head", ref=code)
    # roof outline
    if pitched:
        if cut.axis == "y":
            z_r = model.elevation(LEVEL_RIDGE)
            d.poly("A-ROOF", [(H(-ov), model.roof_z_at(-ov)), (H(w / 2), z_r), (H(w + ov), model.roof_z_at(w + ov))],
                   semantic="roof")
        else:
            zr = model.roof_z_at(pos)
            d.line("A-ROOF", H(-ov), zr, H(dep + ov), zr, semantic="roof")
    else:
        z_p = model.elevation(LEVEL_PARAPET)
        t0 = crossing[0].thickness
        for u_edge, sign in ((lo_u, 1), (hi_u, -1)):
            a, b = h_range(u_edge, u_edge + sign * t0)
            d.cut("A-SECTION-CUT", [(a, z_e), (b, z_e), (b, z_p), (a, z_p)], WALL_HATCH, semantic="parapet")
    # rooms the plane passes through: name and clear height, on every floor
    stairs_out = _stairs(d, model, cut, H, pos)
    for room in model.rooms:
        x0, y0, x1, y1 = room.bbox
        inside = (y0 < pos < y1) if cut.axis == "y" else (x0 < pos < x1)
        if not inside:
            continue
        n = model.floor_of(room.level_id)
        zf_n, zc_n = model.elevation(room.level_id), model.elevation(ceiling_level_id(n))
        u0, u1 = (x0, x1) if cut.axis == "y" else (y0, y1)
        a, b = h_range(u0, u1)
        d.text("A-ANNO-TEXT", (a + b) / 2, zf_n + 0.55, room.name.upper(), d.th, MC, semantic="room_name", ref=room.code)
        xd = a + 0.45
        d.dim(xd, zf_n, xd, zc_n, xd, (zf_n + zc_n) / 2, semantic="clear_height", ref=room.code,
              data={"from_level": room.level_id, "to_level": ceiling_level_id(n)})
    # levels and dimensions
    ext_lo, ext_hi = h_range(lo_u, hi_u)
    x1 = ext_lo - 0.15
    x0 = ext_lo - 3.4
    for level_id in (LEVEL_GROUND, *model.floor_ids, top_ceiling, LEVEL_ROOF):
        _level_mark(d, model, level_id, x0, x1)
    d.line("A-SITE-CONTEXT", x0, z_g, ext_hi + 3.4, z_g, semantic="ground")
    d.dim(ext_lo, z_g, ext_hi, z_g, (ext_lo + ext_hi) / 2, z_g - 1.1)
    title = f"MẶT CẮT {cut.code}\\PTỶ LỆ VẼ 1:1 (in 1:100) - {cut.sheet}"
    d.text("A-ANNO-TEXT", (ext_lo + ext_hi) / 2, z_g - 2.4, title, model.text_height * 1.4, TC, semantic="title")

    marker = section_marker(model, cut)
    record = {"id": cut.view_id, "kind": "section", "sheet": cut.sheet, "section_code": cut.code,
              "label": f"MẶT CẮT {cut.code}",
              "cut": {"axis": cut.axis, "position": pos, "direction": cut.direction},
              "levels": _measure_levels(d.prims), "clear_heights": _measure_clear(d.prims),
              "cut_walls": sorted({p.ref for p in d.prims if p.semantic == "cut_wall"}),
              "slabs": list(dict.fromkeys(p.ref for p in d.prims if p.semantic.startswith("slab_"))),
              "slab_pieces": _measure_slabs(d.prims),
              "stairs": stairs_out, "marker": marker.record, "floors": model.floors}
    return View(cut.view_id, "section", cut.sheet, f"MẶT CẮT {cut.code}", d.prims, record)


def _measure_slabs(prims: Sequence[Prim]) -> list[dict[str, Any]]:
    out = []
    for p in prims:
        if p.semantic.startswith("slab_"):
            x0, z0, x1, z1 = _bbox_of(p)
            out.append({"ref": p.ref, "u0": round(x0, 6), "u1": round(x1, 6), "z0": round(z0, 6), "z1": round(z1, 6)})
    return out


def _measure_clear(prims: Sequence[Prim]) -> list[dict[str, Any]]:
    out = []
    for p in prims:
        if p.semantic == "clear_height":
            out.append({"room_id": p.ref, "height": round(abs(p.pts[3] - p.pts[1]), 6),
                        "from_level": p.data.get("from_level"), "to_level": p.data.get("to_level"),
                        "z_from": round(min(p.pts[1], p.pts[3]), 6)})
    return out


def _stair_profiles(model: HouseModel, s: Any) -> dict[str, Any]:
    """Section profiles of a designed stair in (u, z) with u measured along the run axis (plan metres).

    A: the first flight (from the near end to the landing); B: the return flight of a U stair (landing back towards the
    near end). Every riser is a vertical segment of the polyline, so the drawing can be counted back.
    """
    x0, y0, x1, y1 = s.footprint
    u_lo, u_hi = (y0, y1) if s.run_axis == "y" else (x0, x1)
    sign = s.run_sign
    u_n, u_f = (u_lo, u_hi) if sign > 0 else (u_hi, u_lo)
    zf, zt = model.elevation(s.from_level), model.elevation(s.to_level)
    r = (zt - zf) / s.riser_count
    tread = s.tread
    parts = s.flights
    n1 = parts[0]["risers"]
    pts_a: list[tuple[float, float]] = [(u_n, zf)]
    for i in range(1, n1 + 1):
        u_i = u_n + sign * (i - 1) * tread
        pts_a.append((u_i, zf + i * r))
        if i < n1:
            pts_a.append((u_i + sign * tread, zf + i * r))
    if abs(pts_a[-1][0] - u_f) > EPS:
        pts_a.append((u_f, zf + n1 * r))
    out: dict[str, Any] = {"A": pts_a, "u_n": u_n, "u_f": u_f, "zf": zf, "zt": zt, "r": r, "z_land": zf + n1 * r}
    if len(parts) == 2:
        n2 = parts[1]["risers"]
        u_b1 = u_f - sign * s.landing
        z_l = zf + n1 * r
        pts_b: list[tuple[float, float]] = [(u_b1, z_l)]
        for j in range(1, n2 + 1):
            u_j = u_b1 - sign * (j - 1) * tread
            pts_b.append((u_j, z_l + j * r))
            if j < n2:
                pts_b.append((u_j - sign * tread, z_l + j * r))
        if abs(pts_b[-1][0] - u_n) > EPS:
            pts_b.append((u_n, zt))
        out["B"] = pts_b
        out["u_b1"] = u_b1
    return out


def _risers_in(pts: Sequence[tuple[float, float]]) -> int:
    return sum(1 for (xa, za), (xb, zb) in zip(pts, pts[1:]) if abs(xa - xb) < EPS and zb > za + EPS)


def _stairs(d: _Draft, model: HouseModel, cut: Cut, H: Callable[[float], float], pos: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    slab = model.values["slab_thickness"]
    for s in model.stairs:
        x0, y0, x1, y1 = s.footprint
        crosses = (y0 < pos < y1) if cut.axis == "y" else (x0 < pos < x1)
        parallel = s.run_axis == ("x" if cut.axis == "y" else "y")
        if not crosses:
            continue
        zf, zt = model.elevation(s.from_level), model.elevation(s.to_level)
        u0, u1 = (x0, x1) if cut.axis == "y" else (y0, y1)
        if not parallel:
            a, b = sorted((H(u0), H(u1)))
            d.rect("A-STAIR", a, zf, b, zt, semantic="stair_end_view", ref=s.id)
            out.append({"id": s.id, "drawn_as": "end view (flight is perpendicular to the cut)",
                        "total_height": round(zt - zf, 6), "riser_count": None})
            continue
        if s.flights:
            out.append(_designed_stair_section(d, model, s, H, pos, cut, slab))
            continue
        n = s.riser_count
        rise = (zt - zf) / n
        run = (u1 - u0) / n
        start_u = u0 if s.run_sign > 0 else u1
        pts: list[tuple[float, float]] = [(H(start_u), zf)]
        for i in range(n):
            u_a = start_u + s.run_sign * run * i
            u_b = start_u + s.run_sign * run * (i + 1)
            pts.append((H(u_a), zf + rise * (i + 1)))
            if i < n - 1:
                pts.append((H(u_b), zf + rise * (i + 1)))
        d.poly("A-STAIR", pts, semantic="stair_profile", ref=s.id)
        end_u = start_u + s.run_sign * run * (n - 1)
        d.cut("A-STAIR", pts + [(H(end_u), zt - slab), (H(start_u), zf - slab)], SLAB_HATCH, semantic="stair_cut", ref=s.id)
        ys = [p[1] for p in pts]
        out.append({"id": s.id, "drawn_as": "profile", "total_height": round(max(ys) - min(ys), 6),
                    "riser_count": _risers_in(pts)})
    return out


def _designed_stair_section(d: _Draft, model: HouseModel, s: Any, H: Callable[[float], float], pos: float, cut: Cut,
                            slab: float) -> dict[str, Any]:
    prof = _stair_profiles(model, s)
    x0, y0, x1, y1 = s.footprint
    across0, across1 = (x0, x1) if cut.axis == "x" else (y0, y1)
    # which flight does the cut pass through? A is the first `width` of the footprint, B the last `width`
    in_a = pos < across0 + s.width
    in_b = len(s.flights) == 2 and pos > across1 - s.width
    if len(s.flights) == 1:
        in_a, in_b = True, False
    a_pts = [(H(u), z) for u, z in prof["A"]]
    b_pts = [(H(u), z) for u, z in prof["B"]] if "B" in prof else []
    u_n, u_f, zf, zt = prof["u_n"], prof["u_f"], prof["zf"], prof["zt"]
    if in_a:
        d.poly("A-STAIR", a_pts, semantic="stair_profile", ref=s.id)
        d.cut("A-STAIR", a_pts + [(H(u_f), prof["z_land"] - slab), (H(u_n), zf - slab)], SLAB_HATCH,
              semantic="stair_cut", ref=s.id)
    else:
        d.poly("A-STAIR", a_pts, semantic="stair_proj", ref=s.id)
    if b_pts:
        if in_b:
            d.poly("A-STAIR", b_pts, semantic="stair_profile", ref=s.id)
            d.cut("A-STAIR", b_pts + [(H(u_n), zt - slab), (H(prof["u_b1"]), prof["z_land"] - slab)], SLAB_HATCH,
                  semantic="stair_cut", ref=s.id)
        else:
            d.poly("A-STAIR", b_pts, semantic="stair_proj", ref=s.id)
        if not in_a:  # the landing spans both flights and is cut wherever the cut passes
            ua, ub = H(prof["u_b1"]), H(u_f)
            lo, hi = min(ua, ub), max(ua, ub)
            zl = prof["z_land"]
            d.cut("A-STAIR", [(lo, zl - slab), (hi, zl - slab), (hi, zl), (lo, zl)], SLAB_HATCH,
                  semantic="stair_landing", ref=s.id)
    risers = _risers_in(a_pts) + _risers_in(b_pts)
    ys = [p[1] for p in a_pts + b_pts]
    return {"id": s.id, "drawn_as": "profile" + (" (cut through flight A)" if in_a else " (cut through flight B)" if in_b
                                                 else " (cut between the flights: both seen beyond)"),
            "total_height": round(max(ys) - min(ys), 6), "riser_count": risers,
            "risers_per_flight": [_risers_in(a_pts)] + ([_risers_in(b_pts)] if b_pts else [])}


@dataclass
class Marker:
    prims: list[Prim]
    record: dict[str, Any]


def section_marker(model: HouseModel, cut: Cut) -> Marker:
    """The cut line, an arrow at each end showing the viewing direction, and a labelled bubble: plan coordinates."""
    pos = cut.position
    assert pos is not None
    d = _Draft(model.text_height * 1.1)
    along_max = model.width if cut.axis == "y" else model.depth
    u0, u1 = -2.6, along_max + 2.6
    r = 0.32

    def P(u: float, n: float) -> tuple[float, float]:
        return (u, n) if cut.axis == "y" else (n, u)

    d.line("A-SECTION-CUT", *P(u0, pos), *P(u1, pos), semantic="marker_line", ref=cut.view_id)
    for u, side in ((u0, -1), (u1, 1)):
        tri = [P(u - 0.18, pos), P(u + 0.18, pos), P(u, pos + cut.direction * 0.5)]
        d.poly("A-SECTION-CUT", tri, True, semantic="marker_arrow", ref=cut.view_id)
        cx, cy = P(u + side * (r + 0.1), pos)
        d.circle("A-DETAIL-MARKER", cx, cy, r, semantic="marker_bubble", ref=cut.view_id)
        d.text("A-DETAIL-MARKER", cx, cy + 0.02, cut.letter, d.th, MC, semantic="marker_letter", ref=cut.view_id)
        d.text("A-DETAIL-MARKER", cx, cy - r - 0.12, cut.sheet, d.th * 0.7, TC, semantic="marker_sheet", ref=cut.view_id)
    line = next(p for p in d.prims if p.semantic == "marker_line")
    start, end = (line.pts[0], line.pts[1]), (line.pts[2], line.pts[3])
    axis = "y" if abs(start[1] - end[1]) < EPS else "x" if abs(start[0] - end[0]) < EPS else "?"
    position = start[1] if axis == "y" else start[0]
    arrow = next(p for p in d.prims if p.semantic == "marker_arrow")
    ax = arrow.pts
    apex_n = ax[5] if axis == "y" else ax[4]
    base_n = ax[1] if axis == "y" else ax[0]
    sheet = next(p for p in d.prims if p.semantic == "marker_sheet").text
    letter = next(p for p in d.prims if p.semantic == "marker_letter").text
    record = {"start": list(start), "end": list(end), "axis": axis, "position": round(position, 6),
              "direction": 1 if apex_n > base_n else -1, "letter": letter, "target_sheet": sheet,
              "target_view": f"SEC-{letter}-{letter}"}
    return Marker(d.prims, record)


def section_marker_view(model: HouseModel, cut: Cut) -> View:
    cut = check_cut(model, cut)
    m = section_marker(model, cut)
    return View(f"MK-{cut.code}", "marker", cut.sheet, f"Section marker {cut.code}", m.prims, {"marker": m.record})


# --------------------------------------------------------------------------- schedule tables

TABLES: dict[str, dict[str, Any]] = {
    "door": {"title": "BẢNG CỬA ĐI", "cols": [("code", "Mã", 1.0), ("type", "Loại", 1.4), ("width", "Rộng (m)", 1.5),
                                              ("height", "Cao (m)", 1.5), ("sill_height", "Bậu (m)", 1.5),
                                              ("wall_id", "Tường", 1.9), ("facade", "Mặt", 1.4)]},
    "window": {"title": "BẢNG CỬA SỔ", "cols": [("code", "Mã", 1.0), ("width", "Rộng (m)", 1.5), ("height", "Cao (m)", 1.5),
                                                ("sill_height", "Bậu (m)", 1.5), ("head_height", "Đỉnh (m)", 1.5),
                                                ("wall_id", "Tường", 1.9), ("facade", "Mặt", 1.4)]},
    "room": {"title": "BẢNG PHÒNG VÀ DIỆN TÍCH (tim tường)", "cols": [("code", "Mã", 1.0), ("name", "Phòng", 4.2),
                                                                   ("area", "Diện tích (m²)", 2.4)]},
}
NUMERIC_COLS = {"width", "height", "sill_height", "head_height", "area"}
TOTAL_ROW = "Tổng"


def _table_rows(model: HouseModel, kind: str, floor: int | None = None) -> list[dict[str, Any]]:
    if kind == "door":
        rows = model.door_rows()
    elif kind == "window":
        rows = model.window_rows()
    else:
        rows = model.room_rows()
    if floor is not None:
        rows = [r for r in rows if r["level_id"] == floor_level_id(floor)]
    return rows


def schedule_views(model: HouseModel) -> list[View]:
    """Door, window and room tables as lines and text (local coordinates, the first table's top-left at 0,0).
    A multi-storey model gets one table of each kind per floor (ids SCH-DOOR-F2, ...)."""
    views: list[View] = []
    y = 0.0
    for kind, sheet in (("door", DOOR_SCHEDULE_SHEET), ("window", DOOR_SCHEDULE_SHEET), ("room", ROOM_SCHEDULE_SHEET)):
        x = 0.0
        lowest = y
        for n in range(1, model.floors + 1):  # the floors side by side, the kinds one below the other
            view = _table(model, kind, sheet, y, n if model.floors > 1 else None)
            _shift_view(view, x)
            views.append(view)
            x = view.bbox[2] + 1.5
            lowest = min(lowest, view.bbox[1])
        y = lowest - 2.6
    return views


def _shift_view(view: View, dx: float) -> None:
    """Move a table view right by dx (lines and texts only: that is all a table is made of)."""
    if not dx:
        return
    for prim in view.prims:
        pts = list(prim.pts)
        idx = (0, 2) if prim.kind == "line" else (0,) if prim.kind == "text" else ()
        for i in idx:
            pts[i] += dx
        prim.pts = tuple(pts)


def _table(model: HouseModel, kind: str, sheet: str, y_top: float, floor: int | None = None) -> View:
    spec = TABLES[kind]
    cols = spec["cols"]
    rows = _table_rows(model, kind, floor)
    title_text = spec["title"] + (f" - TẦNG {floor}" if floor else "")
    level_id = floor_level_id(floor or 1)
    d = _Draft(model.text_height * 0.8)
    row_h = 0.62
    pad = 0.12
    widths = [c[2] for c in cols]
    total_w = sum(widths)
    d.text("A-ANNO-TEXT", 0.0, y_top, title_text, model.text_height * 1.2, BL, semantic="table_title", ref=kind)
    top = y_top - 0.2
    body: list[list[str]] = [[c[1] for c in cols]]
    row_ids: list[str] = ["header"]
    for r in rows:
        body.append([_cell_text(r.get(key), key) for key, _label, _w in cols])
        row_ids.append(str(r["code"]))
    if kind == "room":
        total = sum(float(r["area"]) for r in rows)
        body.append([TOTAL_ROW if key == "code" else (fmt2(total) if key == "area" else "") for key, _l, _w in cols])
        row_ids.append("total")
    n = len(body)
    # grid
    for i in range(n + 1):
        d.line("A-ANNO-KEYNOTE", 0.0, top - i * row_h, total_w, top - i * row_h, semantic="table_grid", ref=kind)
    x = 0.0
    for wcol in [0.0] + widths:
        x += wcol
        d.line("A-ANNO-KEYNOTE", x, top, x, top - n * row_h, semantic="table_grid", ref=kind)
    for i, (cells, rid) in enumerate(zip(body, row_ids)):
        x = 0.0
        for (key, _label, wcol), text in zip(cols, cells):
            if text:
                d.text("A-ANNO-TEXT", x + pad, top - i * row_h - row_h / 2, text, d.th, ML, semantic="cell", ref=kind,
                       data={"row": rid, "col": key})
            x += wcol
    rid_ = f"SCH-{kind.upper()}" + (f"-F{floor}" if floor else "")
    record = {"id": rid_, "kind": "schedule", "schedule": kind, "sheet": sheet,
              "label": title_text, "level_id": level_id, "rows": _measure_table(d.prims, kind, level_id)}
    return View(record["id"], "schedule", sheet, title_text, d.prims, record)


def _cell_text(value: Any, key: str) -> str:
    if value is None:
        return ""
    if key in NUMERIC_COLS:
        return fmt2(float(value))
    return str(value)


def _measure_table(prims: Sequence[Prim], kind: str, level_id: str = "LV-F1") -> list[dict[str, Any]]:
    """Rows read back from the cell texts that were drawn (numbers parsed, decimal comma accepted)."""
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for p in prims:
        if p.semantic != "cell" or p.ref != kind:
            continue
        rid, col = p.data["row"], p.data["col"]
        if rid == "header":
            continue
        if rid not in rows:
            rows[rid] = {}
            order.append(rid)
        value: Any = p.text
        if col in NUMERIC_COLS:
            number = parse_number(p.text)
            value = number if number is not None else p.text
        rows[rid][col] = value
    out = []
    for rid in order:
        row = dict(rows[rid])
        if rid == "total":
            out.append({"code": row.get("code", TOTAL_ROW), "total": True, "area": row.get("area"),
                        "level_id": level_id})
        else:
            row["level_id"] = level_id
            out.append(row)
    return out


# --------------------------------------------------------------------------- stair symbol, slab opening and roof plan

def _rect_union_outline(d: _Draft, layer: str, rects: Sequence[tuple[float, float, float, float]], **kw: Any) -> None:
    for x0, y0, x1, y1 in rects:
        d.rect(layer, x0, y0, x1, y1, **kw)


def stair_plan_view(model: HouseModel, floor: int) -> View | None:
    """The stair symbol and the slab opening on the plan of `floor` (plan metres, same origin as the plan itself).

    * the stair that starts on this floor: outline, one line per riser face, landing edge, walking line with an arrow
      and the text LÊN (not on the top floor);
    * the stair that arrives on this floor (floor >= 2): the outline of the slab opening and the text XUỐNG at the
      arrival end. On the top floor the arriving flights are drawn too, because nothing climbs further.
    Returns None for a single-storey model.
    """
    if not model.stairs:
        return None
    lid = floor_level_id(floor)
    up = next((s for s in model.stairs if s.from_level == lid), None)
    arriving = next((s for s in model.stairs if s.to_level == lid), None)
    th = model.text_height
    d = _Draft(th * 0.8)

    def draw_flights(s: Any, role: str) -> None:
        x0, y0, x1, y1 = s.footprint
        d.rect("A-STAIR", x0, y0, x1, y1, semantic="stair_outline", ref=s.id, data={"role": role})
        if not s.flights:
            return
        sign = s.run_sign
        near, far = (y0, y1) if sign > 0 else (y1, y0)
        tread = s.tread
        n1 = s.flights[0]["risers"]
        xa0, xa1 = x0, (x0 + s.width if len(s.flights) == 2 else x1)
        for i in range(n1):  # riser faces of the first flight (face i is where riser i+1 stands)
            yy = near + sign * i * tread
            d.line("A-STAIR", xa0, yy, xa1, yy, semantic="stair_riser", ref=s.id, data={"role": role, "flight": 1})
        if len(s.flights) == 2:
            n2 = s.flights[1]["risers"]
            xb0, xb1 = x1 - s.width, x1
            y_b1 = far - sign * s.landing
            for j in range(n2):
                yy = y_b1 - sign * j * tread
                d.line("A-STAIR", xb0, yy, xb1, yy, semantic="stair_riser", ref=s.id, data={"role": role, "flight": 2})
            d.line("A-STAIR", x0, y_b1, x1, y_b1, semantic="stair_landing_edge", ref=s.id, data={"role": role})
            d.line("A-STAIR", x0 + s.width, near, x0 + s.width, far, semantic="stair_well", ref=s.id, data={"role": role})

    def arrow(s: Any, x: float, y_from: float, y_to: float, text: str, role: str, tx: float, ty: float) -> None:
        d.line("A-STAIR", x, y_from, x, y_to, semantic="stair_walkline", ref=s.id, data={"role": role})
        sg = 1 if y_to > y_from else -1
        d.poly("A-STAIR", [(x - 0.12, y_to - sg * 0.3), (x + 0.12, y_to - sg * 0.3), (x, y_to)], True,
               semantic="stair_arrow", ref=s.id, data={"role": role})
        d.text("A-ANNO-TEXT", tx, ty, text, th * 0.8, MC, semantic="stair_text", ref=s.id, data={"role": role, "text": text})

    if up is not None:
        draw_flights(up, "up")
        x0, y0, x1, y1 = up.footprint
        near, far = (y0, y1) if up.run_sign > 0 else (y1, y0)
        xa = x0 + up.width / 2
        end = far - up.run_sign * up.landing / 2 if len(up.flights) == 2 else far
        arrow(up, xa, near, end, "LÊN", "up", xa, near - up.run_sign * 0.35)
    if arriving is not None:
        x0, y0, x1, y1 = arriving.footprint
        near, far = (y0, y1) if arriving.run_sign > 0 else (y1, y0)
        if up is None:
            draw_flights(arriving, "arriving")
        if floor_number(arriving.to_level) == floor:  # slab opening of this floor
            _rect_union_outline(d, "A-HIDDEN", arriving.opening, semantic="slab_opening", ref=arriving.id)
        xb = x1 - arriving.width / 2 if len(arriving.flights) == 2 else x0 + arriving.width / 2
        d.line("A-STAIR", xb, near, xb, far - arriving.run_sign * (arriving.landing / 2 if len(arriving.flights) == 2 else 0.0),
               semantic="stair_walkline", ref=arriving.id, data={"role": "down"})
        d.text("A-ANNO-TEXT", xb, near - arriving.run_sign * 0.35, "XUỐNG", th * 0.8, MC, semantic="stair_text",
               ref=arriving.id, data={"role": "down", "text": "XUỐNG"})
    sheet = plan_sheet_code(floor)
    record = _stair_plan_record(d.prims, floor, lid, sheet)
    return View(f"STAIRPLAN-F{floor}", "plan_stair", sheet, f"Cầu thang tầng {floor}", d.prims, record)


def _stair_plan_record(prims: Sequence[Prim], floor: int, level_id: str, sheet: str) -> dict[str, Any]:
    stairs: dict[str, dict[str, Any]] = {}
    for p in prims:
        if p.semantic == "stair_outline":
            x0, y0, x1, y1 = _bbox_of(p)
            stairs.setdefault(p.ref, {"id": p.ref})["footprint"] = [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]
            stairs[p.ref]["role"] = p.data.get("role")
        elif p.semantic == "stair_riser":
            stairs.setdefault(p.ref, {"id": p.ref}).setdefault("riser_lines", 0)
            stairs[p.ref]["riser_lines"] += 1
        elif p.semantic == "stair_text":
            stairs.setdefault(p.ref, {"id": p.ref}).setdefault("texts", []).append(p.data.get("text"))
    openings: dict[str, list[list[float]]] = {}
    for p in prims:
        if p.semantic == "slab_opening":
            x0, y0, x1, y1 = _bbox_of(p)
            openings.setdefault(p.ref, []).append([round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)])
    return {"id": f"PLAN-F{floor}", "kind": "plan", "code": sheet, "level_id": level_id, "floor": floor,
            "stairs": list(stairs.values()),
            "slab_openings": [{"stair_id": k, "rects": v} for k, v in openings.items()]}


ROOF_PLAN_ID = "PLAN-ROOF"


def roof_plan_sheet_code(model: HouseModel) -> str:
    return plan_sheet_code(model.floors + 1)


def roof_plan_view(model: HouseModel) -> View:
    """Roof plan: eaves outline, ridge and fall arrows (pitched) or parapet outline (flat). Plan metres."""
    w, dep, p = model.width, model.depth, model.porch
    ov = model.roof["overhang"]
    d = _Draft(model.text_height * 0.9)
    x0, y0, x1, y1 = -ov, p - ov, w + ov, dep + ov
    d.rect("A-ROOF", x0, y0, x1, y1, semantic="roof_outline")
    d.rect("A-WALL-EXT", 0.0, p, w, dep, semantic="wall_below")
    sheet = roof_plan_sheet_code(model)
    if model.roof["type"] == "pitched":
        d.line("A-ROOF", w / 2, y0, w / 2, y1, semantic="roof_ridge")
        slope = model.roof["slope_deg"]
        for xa, xb in ((w * 0.25, 0.0), (w * 0.75, w)):
            ym = (p + dep) / 2
            d.line("A-ROOF", xa, ym, xb, ym, semantic="roof_fall")
            sg = -1 if xb < xa else 1
            d.poly("A-ROOF", [(xb - sg * 0.35, ym - 0.12), (xb - sg * 0.35, ym + 0.12), (xb, ym)], True, semantic="roof_arrow")
        d.text("A-ANNO-TEXT", w / 2, (p + dep) / 2 + 0.6, f"i = {slope:g}°", model.text_height * 0.9, BC, semantic="roof_slope")
        kind_text = f"MÁI DỐC, ĐỈNH NÓC {level_label(model.elevation(LEVEL_RIDGE))}"
    else:
        d.rect("A-ROOF", 0.1, p + 0.1, w - 0.1, dep - 0.1, semantic="roof_parapet")
        kind_text = f"MÁI BẰNG, LAN CAN {level_label(model.elevation(LEVEL_PARAPET))}"
    d.text("A-ANNO-TEXT", w / 2, y0 - 1.2, f"MẶT BẰNG MÁI\\P{kind_text} - {sheet}", model.text_height * 1.4, TC,
           semantic="title")
    ex0, ey0, ex1, ey1 = _bbox_of(next(q for q in d.prims if q.semantic == "roof_outline"))
    record = {"id": ROOF_PLAN_ID, "kind": "roof_plan", "code": sheet, "sheet": sheet, "roof_type": model.roof["type"],
              "eaves": [round(ex0, 6), round(ey0, 6), round(ex1, 6), round(ey1, 6)],
              "ridge_x": next((q.pts[0] for q in d.prims if q.semantic == "roof_ridge"), None),
              "slope_deg": model.roof["slope_deg"] if model.roof["type"] == "pitched" else None}
    return View(ROOF_PLAN_ID, "roof_plan", sheet, "Mặt bằng mái", d.prims, record)


def floor_plan_origin(model: HouseModel, floor: int, plan_origin: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    """Floor 1 at the plan origin, higher floors (and the roof plan) to its left, one plan-width + 22 m apart."""
    ox, oy = plan_origin
    return ox - (floor - 1) * (model.width + 22.0), oy


# --------------------------------------------------------------------------- placement

def _elevation_row(model: HouseModel) -> tuple[float, float]:
    """How far below the plan origin the elevations and the section are placed. A single-storey house keeps the original
    16 m / 38 m; a taller house is pushed down so that the top of its elevations (ridge) stays 6 m below the plan origin
    and the section stays 8 m below the lowest elevation."""
    if model.floors == 1:
        return 16.0, 38.0
    boxes = [elevation_view(model, side).bbox for side in SIDES]
    top, bottom = max(b[3] for b in boxes), min(b[1] for b in boxes)
    sec = section_view(model, check_cut(model, default_cut(model))).bbox
    elevations_y = 6.0 + top  # origin y offset: the elevation's local top lands 6 m below the plan origin
    section_y = 6.0 + (top - bottom) + 8.0 + sec[3]
    return elevations_y, section_y


def default_origin(model: HouseModel, view: str, plan_origin: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    """Where a view goes when the caller gives no origin: below the plan (elevations, then sections), tables to its right.

    `view` is an elevation side, 'section' or 'schedule'. The offsets keep the views apart for this house's size.
    """
    ox, oy = plan_origin
    w, dep = model.width, model.depth
    gap = 14.0
    if view in SIDES:
        x = 4.0
        for side in SIDES:
            if side == view:
                break
            x += (w if side in ("front", "rear") else dep) + gap
        return ox + x, oy - _elevation_row(model)[0]
    if view == "section":
        return ox + 4.0, oy - _elevation_row(model)[1]
    if view == "schedule":
        return ox + w + 14.0, oy + dep
    raise ValueError(f"unknown view {view!r}")
