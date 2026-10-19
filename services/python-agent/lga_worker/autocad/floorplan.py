"""House floor plans computed from a few numbers, so a small planner model never has to place walls by hand.

`plan_house` lays out a single-storey Vietnamese house ("nhà cấp 4") on a W x D footprint (metres, wall centre
lines, the front of the house at y = 0):

* a front porch (hiên) across the whole width;
* the living room behind it, with the main double door;
* a corridor from the living room to the back door;
* rooms in one column on each side of the corridor (one column on a narrow lot), front to back, each column a
  list of rows. The first room of a row opens onto the corridor (or the living room); an en-suite bathroom opens
  into its bedroom; the laundry opens to the yard; the kitchen-dining room is open to the corridor.

The result is pure geometry (rooms, wall pieces with real thickness, doors, windows, furniture, dimensions and
labels); `provider.draw_plan` turns it into AutoCAD entities. Nothing here talks to AutoCAD.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EXTERIOR_WALL = 0.2
INTERIOR_WALL = 0.1
EPS = 1e-6
MIN_WIDTH = 4.0
MIN_DEPTH = 8.0
WIDE_LOT = 9.0  # from this width the corridor runs down the middle with rooms on both sides

ENSUITE_DEPTH = 2.2
WINDOW_WIDTH = {"annex": 1.0, "wc": 0.6, "kitchen": 1.4, "bedroom": 1.4, "living": 1.6, "altar": 1.2, "study": 1.4,
                "laundry": 1.0, "storage": 0.8}


@dataclass
class Room:
    name: str
    kind: str
    x0: float
    y0: float
    x1: float
    y1: float
    side: str = ""  # "L" / "R": column; "" for living room and corridor
    extra_area: float = 0.0  # a bedroom's alcove beside its en-suite bathroom (an "annex" room, no wall between)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def depth(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.depth

    @property
    def total_area(self) -> float:
        return self.area + self.extra_area

    @property
    def center(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


@dataclass
class Opening:
    """A gap in a wall. orient "v": wall along y at x = line; "h": wall along x at y = line. [a, b] along the wall.

    kind: "door" (one leaf), "double" (two leaves), "window", "open" (passage). swing: the side the leaves open
    to (+1 = +x / +y). hinge: the end of the gap the single leaf turns on.
    """

    orient: str
    line: float
    a: float
    b: float
    kind: str
    swing: int = 1
    hinge: str = "a"


@dataclass
class Shape:
    """Drawing primitives: "rect" (x0, y0, x1, y1), "circle" (cx, cy, r), "line" (x0, y0, x1, y1) or
    "arc" (cx, cy, r, start, end), angles in radians, counter-clockwise from start to end."""

    kind: str
    values: tuple[float, ...]


@dataclass
class Dimension:
    p1: tuple[float, float]
    p2: tuple[float, float]
    text_at: tuple[float, float]


@dataclass
class Label:
    x: float
    y: float
    text: str
    height: float


@dataclass
class Plan:
    width: float
    depth: float
    porch: float
    rooms: list[Room]
    walls: list[tuple[float, float, float, float]]  # filled rectangles x0, y0, x1, y1
    openings: list[Opening]
    furniture: list[Shape]
    porch_shapes: list[Shape]
    dimensions: list[Dimension]
    labels: list[Label]
    title: Label
    schedule: Label
    text_height: float
    wall_thickness: dict[tuple[str, float], float] = field(default_factory=dict)

    @property
    def area(self) -> float:
        return self.width * self.depth


@dataclass
class _Cell:
    name: str
    kind: str
    width: float | None  # None: what is left of the column
    door: str  # "corridor" | "prev" | "outside" | "open"


@dataclass
class _Row:
    depth: float
    cells: list[_Cell]
    tag: str = ""


def _r(value: float, step: float = 0.05) -> float:
    return round(round(value / step) * step, 4)


def _fmt(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _bedroom_row(name: str, master: bool, colw: float) -> _Row:
    depth = 4.6 if master else 4.0
    ensuite = 2.2
    if colw >= (5.4 if master else 7.0):
        return _Row(depth, [_Cell(name, "bedroom", colw - ensuite, "corridor"), _Cell("WC riêng", "wc", None, "prev")],
                    "bedroom")
    return _Row(depth, [_Cell(name, "bedroom", None, "corridor")], "bedroom")


def _wc_row(colw: float) -> _Row:
    if colw >= 4.6:
        return _Row(2.4, [_Cell("WC chung", "wc", 2.6, "corridor"), _Cell("Giặt - phơi", "laundry", None, "outside")], "wc")
    return _Row(2.4, [_Cell("WC chung", "wc", None, "corridor")], "wc")


def resolve_size(width: float | None, depth: float | None, area: float | None) -> tuple[float, float]:
    if width and depth:
        w, d = float(width), float(depth)
    elif area and width:
        w, d = float(width), float(area) / float(width)
    elif area and depth:
        w, d = float(area) / float(depth), float(depth)
    elif area:
        side = math.sqrt(float(area))
        w = d = side
    else:
        raise ValueError("give width and depth, or the floor area")
    w, d = _r(w, 0.1), _r(d, 0.1)
    if w < MIN_WIDTH or d < MIN_DEPTH:
        raise ValueError(f"a house needs at least {MIN_WIDTH:g} m x {MIN_DEPTH:g} m (got {w:g} x {d:g} m)")
    return w, d


def plan_house(width: float | None = None, depth: float | None = None, area: float | None = None,
               bedrooms: int | None = None, altar: bool | None = None, study: bool | None = None,
               porch: float | None = None) -> Plan:
    w, d = resolve_size(width, depth, area)
    total = w * d
    n_bed = int(bedrooms) if bedrooms else (1 if total < 70 else 2 if total < 120 else 3 if total < 220 else 4)
    n_bed = max(1, min(n_bed, 8))
    want_altar = total >= 80 if altar is None else bool(altar)
    want_study = total >= 260 if study is None else bool(study)
    p = _r(porch if porch is not None else min(2.4, max(1.2, d * 0.1)), 0.1)
    wide = w >= WIDE_LOT
    c = 1.4 if w >= 12 else 1.2
    lw = _r((w - c) / 2) if wide else 0.0
    rw = w - c - lw
    f = _r(min(7.0, max(3.6, d * 0.27)), 0.1)
    living_x1 = lw + c if wide else w
    corridor_x0 = lw if wide else 0.0
    left_y0, right_y0 = p + f, (p if wide else p + f)

    pool: list[_Row] = [_bedroom_row("Phòng ngủ 1", True, rw)]
    for i in range(2, n_bed + 1):
        colw = rw if not wide else min(lw, rw)
        pool.append(_bedroom_row(f"Phòng ngủ {i}", False, colw))
    front: list[_Row] = []
    if want_altar:
        front.append(_Row(3.4, [_Cell("Phòng thờ", "altar", None, "corridor")], "altar"))
    if want_study:
        (front if wide and not front else pool).append(_Row(3.6, [_Cell("Phòng làm việc", "study", None, "corridor")], "study"))
    kitchen = _Row(5.0, [_Cell("Bếp + Phòng ăn", "kitchen", None, "open")], "kitchen")

    left: list[_Row] = []
    right: list[_Row] = list(front)
    if wide:
        left.append(kitchen)
        items = pool + [_wc_row(min(lw, rw))]
        for item in items:
            def fill(rows: list[_Row], avail: float) -> float:
                return (sum(r.depth for r in rows) + item.depth) / avail
            target = left if fill(left, d - left_y0) <= fill(right, d - right_y0) else right
            target.insert(len(target) - (1 if target is left else 0), item)
    else:
        right += pool + [_wc_row(rw), kitchen]
    # Fit the columns: too full => drop the rooms that were only added by default; too empty => add useful ones.
    for rows, y0 in ((left, left_y0), (right, right_y0)):
        if not rows:
            continue
        avail = d - y0
        for tag, auto in (("study", study is None), ("altar", altar is None)):
            if sum(r.depth for r in rows) / avail > 1.25 and auto:
                rows[:] = [r for r in rows if r.tag != tag]
        has_study = any(r.tag == "study" for r in left + right)
        if sum(r.depth for r in rows) / avail < 0.77 and not has_study and study is not False:
            rows.insert(len(rows) - (1 if rows[-1].tag == "kitchen" else 0),
                        _Row(3.6, [_Cell("Phòng làm việc", "study", None, "corridor")], "study"))
        if sum(r.depth for r in rows) / avail < 0.77:
            rows.insert(len(rows) - (1 if rows[-1].tag == "kitchen" else 0),
                        _Row(2.4, [_Cell("Kho", "storage", None, "corridor")], "storage"))
    for rows in (left, right):  # the shared bathroom between the rooms, not at the end of the column
        wc = next((r for r in rows if r.tag == "wc"), None)
        if wc is not None:
            rows.remove(wc)
            body = [r for r in rows if r.tag not in ("altar", "study", "kitchen")]
            anchor = body[len(body) // 2] if body else None
            rows.insert(rows.index(anchor) if anchor is not None else len(rows) - (1 if rows and rows[-1].tag == "kitchen" else 0), wc)

    rooms: list[Room] = [Room("Phòng khách", "living", 0.0, p, living_x1, p + f),
                         Room("Hành lang", "corridor", corridor_x0, p + f, corridor_x0 + c, d)]
    doors: list[tuple[Room, _Cell, int, list[Room]]] = []
    passages: list[Opening] = []

    def place(rows: list[_Row], side: str, y_start: float) -> None:
        if not rows:
            return
        depths = _distribute(rows, d - y_start)
        y = y_start
        for i, row in enumerate(rows):
            y_end = d if i == len(rows) - 1 else _r(y + depths[i])
            colw = lw if side == "L" else rw
            x_edge = lw if side == "L" else lw + c if wide else c
            row_rooms: list[Room] = []
            used = 0.0
            for j, cell in enumerate(row.cells):
                cw = (colw - used) if cell.width is None or j == len(row.cells) - 1 else min(cell.width, colw - used)
                if side == "L":
                    room = Room(cell.name, cell.kind, _r(x_edge - used - cw), y, _r(x_edge - used), y_end, side)
                else:
                    room = Room(cell.name, cell.kind, _r(x_edge + used), y, _r(x_edge + used + cw), y_end, side)
                used += cw
                if cell.door == "prev" and room.kind == "wc" and room.depth >= 2.0 + 1.2:
                    # En-suite bathroom in the back corner; the rest of the outer strip belongs to the bedroom,
                    # so the bedroom keeps an outside wall with a window.
                    bedroom = row_rooms[j - 1]
                    split = _r(y_end - max(2.0, min(ENSUITE_DEPTH, room.depth - 1.4)))
                    annex = Room(bedroom.name, "annex", room.x0, y, room.x1, split, side)
                    room.y0 = split
                    bedroom.extra_area += annex.area
                    rooms.append(annex)
                    line = bedroom.x0 if side == "L" else bedroom.x1
                    passages.append(Opening("v", line, y, split, "open"))
                rooms.append(room)
                row_rooms.append(room)
                doors.append((room, cell, j, row_rooms))
            y = y_end

    if wide:
        place(left, "L", left_y0)
    place(right, "R", right_y0)
    if not wide:  # narrow lot: the column starts at x = c
        pass

    openings: list[Opening] = list(passages)
    # Main entrance, passage living room -> corridor, back door.
    door_w = 1.4 if living_x1 >= 4.0 else 0.9
    mid = living_x1 / 2
    openings.append(Opening("h", p, _r(mid - door_w / 2), _r(mid + door_w / 2), "double" if door_w > 1 else "door", 1))
    openings.append(Opening("h", p + f, corridor_x0, corridor_x0 + c, "open"))
    openings.append(Opening("h", d, _r(corridor_x0 + (c - 0.9) / 2), _r(corridor_x0 + (c + 0.9) / 2), "door", -1))
    for room, cell, j, row_rooms in doors:
        dw = 0.8 if room.kind == "wc" else 0.9
        if cell.door == "open":
            line = room.x1 if room.side == "L" else room.x0
            length = min(2.4, room.depth - 0.6)
            openings.append(Opening("v", line, _r(room.y0 + 0.3), _r(room.y0 + 0.3 + length), "open"))
        elif cell.door == "corridor":
            line = room.x1 if room.side == "L" else room.x0
            a = room.y0 + 0.25
            openings.append(Opening("v", line, _r(a), _r(a + dw), "door", -1 if room.side == "L" else 1, "a"))
        elif cell.door == "prev":
            prev = row_rooms[j - 1]
            line = prev.x0 if room.side == "L" else prev.x1
            b = room.y1 - 0.25
            openings.append(Opening("v", line, _r(b - dw), _r(b), "door", -1 if room.side == "L" else 1, "b"))
        elif cell.door == "outside":
            line = 0.0 if room.side == "L" else w
            b = room.y1 - 0.25
            openings.append(Opening("v", line, _r(b - dw), _r(b), "door", 1 if room.side == "L" else -1, "b"))

    # Windows on outside walls, clear of the doors already there.
    def free(orient: str, line: float, a: float, b: float) -> bool:
        return not any(o.orient == orient and abs(o.line - line) < EPS and o.a < b + 0.3 and o.b > a - 0.3
                       for o in openings)

    for room in rooms:
        if room.kind == "corridor":
            continue
        ww = WINDOW_WIDTH.get(room.kind, 1.2)
        edges = []
        if abs(room.x0) < EPS:
            edges.append(("v", 0.0, room.y0, room.y1))
        if abs(room.x1 - w) < EPS:
            edges.append(("v", w, room.y0, room.y1))
        if abs(room.y1 - d) < EPS:
            edges.append(("h", d, room.x0, room.x1))
        if abs(room.y0 - p) < EPS:
            edges.append(("h", p, room.x0, room.x1))
        placed = 0
        for orient, line, a, b in edges:
            if placed >= 2:
                break
            spans = [(a, b)]
            blockers = sorted((o.a, o.b) for o in openings if o.orient == orient and abs(o.line - line) < EPS and o.a < b and o.b > a)
            if blockers:  # the parts of the wall beside its doors
                spans, start = [], a
                for ba, bb in blockers:
                    spans.append((start, ba))
                    start = bb
                spans.append((start, b))
            for sa, sb in sorted(spans, key=lambda s: s[0] - s[1]):
                if sb - sa < ww + 0.4:
                    continue
                cm = (sa + sb) / 2
                wa, wb = _r(cm - ww / 2), _r(cm + ww / 2)
                if free(orient, line, wa, wb):
                    openings.append(Opening(orient, line, wa, wb, "window"))
                    placed += 1
                    if room.kind != "living":
                        break

    walls, thickness = _walls(rooms, openings, w, d, p)
    h = 0.3 if w >= 12 else 0.25
    labels = [_label(room, h) for room in rooms if room.kind not in ("corridor", "annex")]
    furniture = [shape for room in rooms for shape in _furniture(room)]
    porch_shapes = _porch(w, p, mid, door_w)
    dims = _dimensions(rooms, w, d, p, f, wide, lw, c)
    title = Label(w / 2, -3.2, f"MẶT BẰNG NHÀ CẤP 4\\PKích thước {_fmt(w)} x {_fmt(d)} m - Diện tích {_fmt(total)} m²", h * 1.6)
    listed = [r for r in rooms if r.kind not in ("corridor", "annex")]
    lines = [f"{i}. {r.name}: {_fmt(r.total_area)} m²" for i, r in enumerate(listed, 1)]
    schedule = Label(w + 3.0, d, "BẢNG DIỆN TÍCH (tim tường)\\P" + "\\P".join(lines), h)
    return Plan(w, d, p, rooms, walls, openings, furniture, porch_shapes, dims, labels, title, schedule, h, thickness)


def _distribute(rows: list[_Row], avail: float) -> list[float]:
    """Row depths filling `avail` in proportion to the nominal ones; bathroom rows stay 1.8-2.8 m deep."""
    nominal = [r.depth for r in rows]
    scale = avail / sum(nominal)
    fixed = {i: min(max(n * scale, 1.8), 2.8) for i, (r, n) in enumerate(zip(rows, nominal)) if r.tag == "wc"}
    rest = avail - sum(fixed.values())
    free = sum(n for i, n in enumerate(nominal) if i not in fixed)
    if free <= 0 or rest <= 0:
        return [n * scale for n in nominal]
    return [fixed.get(i, n * rest / free) for i, n in enumerate(nominal)]


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _wall_lines(rooms: list[Room]) -> dict[tuple[str, float], list[tuple[float, float]]]:
    lines: dict[tuple[str, float], list[tuple[float, float]]] = {}
    for r in rooms:
        for key, iv in ((("v", r.x0), (r.y0, r.y1)), (("v", r.x1), (r.y0, r.y1)),
                        (("h", r.y0), (r.x0, r.x1)), (("h", r.y1), (r.x0, r.x1))):
            lines.setdefault((key[0], round(key[1], 4)), []).append(iv)
    return lines


def is_exterior(orient: str, line: float, w: float, d: float, p: float) -> bool:
    return (abs(line) < EPS or abs(line - w) < EPS) if orient == "v" else (abs(line - p) < EPS or abs(line - d) < EPS)


@dataclass
class WallRun:
    """One whole wall between two junctions (before the door/window gaps are cut out): the unit openings belong to."""

    orient: str  # "v": along y at x = line; "h": along x at y = line
    line: float
    a: float
    b: float
    thickness: float
    exterior: bool


def wall_runs(plan: Plan) -> list[WallRun]:
    """The wall centre-line runs behind `plan.walls`, with the same thickness constants (`EXTERIOR_WALL` / `INTERIOR_WALL`)."""
    runs: list[WallRun] = []
    for (orient, line), intervals in sorted(_wall_lines(plan.rooms).items()):
        outside = is_exterior(orient, line, plan.width, plan.depth, plan.porch)
        for a, b in _merge(intervals):
            runs.append(WallRun(orient, line, a, b, EXTERIOR_WALL if outside else INTERIOR_WALL, outside))
    return runs


def _walls(rooms: list[Room], openings: list[Opening], w: float, d: float, p: float
           ) -> tuple[list[tuple[float, float, float, float]], dict[tuple[str, float], float]]:
    lines = _wall_lines(rooms)
    pieces: list[tuple[float, float, float, float]] = []
    thickness: dict[tuple[str, float], float] = {}
    half_ext = EXTERIOR_WALL / 2
    for (orient, line), intervals in lines.items():
        t = EXTERIOR_WALL if is_exterior(orient, line, w, d, p) else INTERIOR_WALL
        thickness[(orient, line)] = t
        gaps = sorted((o.a, o.b) for o in openings if o.orient == orient and abs(o.line - line) < EPS)
        for a, b in _merge(intervals):
            segments, start = [], a
            for ga, gb in gaps:
                if gb <= a or ga >= b:
                    continue
                segments.append((start, ga))
                start = gb
            segments.append((start, b))
            for sa, sb in segments:
                if sb - sa < EPS:
                    continue
                at_gap_a = any(abs(sa - gb) < EPS for _, gb in gaps)
                at_gap_b = any(abs(sb - ga) < EPS for ga, _ in gaps)
                ea = sa if at_gap_a else sa - half_ext
                eb = sb if at_gap_b else sb + half_ext
                if orient == "v":
                    pieces.append((line - t / 2, ea, line + t / 2, eb))
                else:
                    pieces.append((ea, line - t / 2, eb, line + t / 2))
    return pieces, thickness


def _label(room: Room, h: float) -> Label:
    cx, cy = room.center
    if room.kind == "bedroom" and room.depth > 3.2:
        cy = (room.y0 + room.y1 - 2.2) / 2
    elif room.kind == "kitchen":
        cy = room.y0 + min(0.7, room.depth / 4)
    elif room.kind == "living":
        cx, cy = room.x0 + room.width * (0.72 if room.width < 7 else 0.6), room.y0 + 1.4
    elif room.kind == "wc":
        cy = room.y0 + room.depth * 0.32
    small = room.width < 2.0 or room.depth < 2.0
    height = h * (0.8 if small else 1.0)
    return Label(cx, cy, f"{room.name.upper()}\\P{_fmt(room.total_area)} m²", height)


def _furniture(r: Room) -> list[Shape]:
    m = 0.15  # clearance from the wall centre line
    out: list[Shape] = []
    rect = lambda x0, y0, x1, y1: out.append(Shape("rect", (x0, y0, x1, y1)))  # noqa: E731
    if r.kind == "bedroom" and r.width >= 2.8 and r.depth >= 3.2:
        bw = 1.8 if r.name.endswith(" 1") else 1.6
        cx = (r.x0 + r.x1) / 2
        top = r.y1 - m
        rect(cx - bw / 2, top - 2.0, cx + bw / 2, top)
        out.append(Shape("line", (cx - bw / 2, top - 0.45, cx + bw / 2, top - 0.45)))
        rect(cx - bw / 2 + 0.1, top - 0.4, cx - 0.05, top - 0.1)
        rect(cx + 0.05, top - 0.4, cx + bw / 2 - 0.1, top - 0.1)
        length = min(2.0, r.width - 1.4)
        if length >= 1.0:  # wardrobe on the front wall, away from the corridor door
            if r.side == "L":
                rect(r.x0 + m, r.y0 + m, r.x0 + m + length, r.y0 + m + 0.6)
            else:
                rect(r.x1 - m - length, r.y0 + m, r.x1 - m, r.y0 + m + 0.6)
    elif r.kind == "wc" and r.width >= 1.2 and r.depth >= 1.5:
        far_x = r.x0 + m if r.side == "L" else r.x1 - m - 0.45
        top = r.y1 - m
        rect(far_x, top - 0.2, far_x + 0.45, top)
        out.append(Shape("circle", (far_x + 0.225, top - 0.45, 0.2)))
        lx = r.x0 + m if r.side == "R" else r.x1 - m - 0.5
        if r.width >= 1.8:
            rect(lx, top - 0.45, lx + 0.5, top)
            out.append(Shape("circle", (lx + 0.25, top - 0.22, 0.13)))
    elif r.kind == "kitchen" and r.width >= 2.4 and r.depth >= 3.0:
        top = r.y1 - m
        rect(r.x0 + m, top - 0.6, r.x1 - m, top)
        out.append(Shape("circle", (r.x0 + m + 0.6, top - 0.3, 0.2)))
        out.append(Shape("circle", (r.x1 - m - 0.8, top - 0.3, 0.12)))
        out.append(Shape("circle", (r.x1 - m - 0.5, top - 0.3, 0.12)))
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + top - 0.6) / 2 + 0.2
        rect(cx - 0.8, cy - 0.45, cx + 0.8, cy + 0.45)
        for dx in (-0.55, 0.0, 0.55):
            rect(cx + dx - 0.2, cy + 0.55, cx + dx + 0.2, cy + 0.95)
            rect(cx + dx - 0.2, cy - 0.95, cx + dx + 0.2, cy - 0.55)
    elif r.kind == "living" and r.width >= 4.5 and r.depth >= 4.2:
        cy = (r.y0 + r.y1) / 2 + 0.3
        rect(r.x0 + m + 0.1, cy - 1.2, r.x0 + m + 0.9, cy + 1.2)
        rect(r.x0 + m + 0.1, cy + 1.2, r.x0 + m + 1.9, cy + 2.0)
        rect(r.x0 + m + 1.4, cy - 0.6, r.x0 + m + 2.0, cy + 0.6)
        rect(r.x0 + m + 3.2, cy - 0.9, r.x0 + m + 3.65, cy + 0.9)
    elif r.kind == "altar" and r.width >= 2.2:
        cx = (r.x0 + r.x1) / 2
        rect(cx - 0.8, r.y1 - m - 0.6, cx + 0.8, r.y1 - m)
    elif r.kind == "study" and r.width >= 2.4:
        cx = (r.x0 + r.x1) / 2
        rect(cx - 0.7, r.y1 - m - 0.6, cx + 0.7, r.y1 - m)
        rect(cx - 0.25, r.y1 - m - 1.15, cx + 0.25, r.y1 - m - 0.7)
    elif r.kind == "laundry" and r.width >= 1.2:
        x = r.x0 + m if r.side == "L" else r.x1 - m - 0.6
        rect(x, r.y0 + m, x + 0.6, r.y0 + m + 0.6)
        out.append(Shape("circle", (x + 0.3, r.y0 + m + 0.3, 0.22)))
    return out


def _porch(w: float, p: float, mid: float, door_w: float) -> list[Shape]:
    shapes = [Shape("line", (0.0, 0.0, w, 0.0)), Shape("line", (0.0, 0.0, 0.0, p)), Shape("line", (w, 0.0, w, p))]
    n = max(2, round(w / 4) + 1)
    for i in range(n):
        x = 0.15 + (w - 0.3) * i / (n - 1)
        shapes.append(Shape("rect", (x - 0.15, 0.0, x + 0.15, 0.3)))
    sw = max(2.4, door_w + 1.0)
    for k in range(1, 4):
        y = -0.3 * k
        shapes.append(Shape("line", (mid - sw / 2, y, mid + sw / 2, y)))
    shapes.append(Shape("line", (mid - sw / 2, 0.0, mid - sw / 2, -0.9)))
    shapes.append(Shape("line", (mid + sw / 2, 0.0, mid + sw / 2, -0.9)))
    return shapes


def _dimensions(rooms: list[Room], w: float, d: float, p: float, f: float, wide: bool, lw: float, c: float
                ) -> list[Dimension]:
    dims: list[Dimension] = []

    def chain_x(xs: list[float], y_obj: float, y_line: float) -> None:
        for a, b in zip(xs, xs[1:]):
            if b - a > EPS:
                dims.append(Dimension((a, y_obj), (b, y_obj), ((a + b) / 2, y_line)))

    def chain_y(ys: list[float], x_obj: float, x_line: float) -> None:
        for a, b in zip(ys, ys[1:]):
            if b - a > EPS:
                dims.append(Dimension((x_obj, a), (x_obj, b), (x_line, (a + b) / 2)))

    chain_x([0.0, lw, lw + c, w] if wide else [0.0, c, w], -0.9, -1.4)
    dims.append(Dimension((0.0, -0.9), (w, -0.9), (w / 2, -2.0)))
    rows = [r for r in rooms if r.kind != "annex" and r.name != "WC riêng"]  # row boundaries only
    left = sorted({0.0, p, p + f, d} | {r.y0 for r in rows if r.side == "L"})
    right = sorted({0.0, p, d} | ({p + f} if not wide else set()) | {r.y0 for r in rows if r.side == "R"})
    chain_y(left, 0.0, -1.2)
    dims.append(Dimension((0.0, 0.0), (0.0, d), (-2.0, d / 2)))
    chain_y(right, w, w + 1.2)
    return dims


def opening_shapes(op: Opening, thickness: float) -> list[Shape]:
    """Symbols drawn in a wall gap: a window's wall faces and glass line, a door's leaf and swing arc."""
    t = thickness
    if op.kind == "window":
        if op.orient == "v":
            x, a, b = op.line, op.a, op.b
            return [Shape("line", (x - t / 2, a, x - t / 2, b)), Shape("line", (x + t / 2, a, x + t / 2, b)),
                    Shape("line", (x, a, x, b)), Shape("line", (x - t / 2, a, x + t / 2, a)),
                    Shape("line", (x - t / 2, b, x + t / 2, b))]
        y, a, b = op.line, op.a, op.b
        return [Shape("line", (a, y - t / 2, b, y - t / 2)), Shape("line", (a, y + t / 2, b, y + t / 2)),
                Shape("line", (a, y, b, y)), Shape("line", (a, y - t / 2, a, y + t / 2)),
                Shape("line", (b, y - t / 2, b, y + t / 2))]
    if op.kind not in ("door", "double"):
        return []
    length = op.b - op.a
    leaves = [("a", length / 2), ("b", length / 2)] if op.kind == "double" else [(op.hinge, length)]
    shapes: list[Shape] = []
    for hinge, size in leaves:
        along = 1.0 if hinge == "a" else -1.0
        pos = op.a if hinge == "a" else op.b
        if op.orient == "v":
            hx, hy = op.line, pos
            leaf_dir, closed_dir = (float(op.swing), 0.0), (0.0, along)
        else:
            hx, hy = pos, op.line
            leaf_dir, closed_dir = (0.0, float(op.swing)), (along, 0.0)
        shapes.append(Shape("line", (hx, hy, hx + leaf_dir[0] * size, hy + leaf_dir[1] * size)))
        a_leaf = math.atan2(leaf_dir[1], leaf_dir[0]) % (2 * math.pi)
        a_closed = math.atan2(closed_dir[1], closed_dir[0]) % (2 * math.pi)
        quarter = abs(((a_closed - a_leaf) % (2 * math.pi)) - math.pi / 2) < 1e-6
        start, end = (a_leaf, a_closed) if quarter else (a_closed, a_leaf)
        shapes.append(Shape("arc", (hx, hy, size, start, end)))
    return shapes


def expected_entity_count(plan: Plan) -> int:
    """Entities `provider.draw_plan` creates for this plan: filled walls (outline + hatch), door/window symbols,
    furniture, porch, room labels, title, area table and dimensions."""
    count = 2 * len(plan.walls)
    for op in plan.openings:
        count += len(opening_shapes(op, plan.wall_thickness.get((op.orient, round(op.line, 4)), INTERIOR_WALL)))
    return count + len(plan.furniture) + len(plan.porch_shapes) + len(plan.labels) + 2 + len(plan.dimensions)
