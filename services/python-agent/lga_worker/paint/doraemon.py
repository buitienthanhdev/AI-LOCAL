"""Doraemon, template version 2 (docs/specs/NANG_CAP_LOGIC_VE_DORAEMON.txt).

What changed against version 1 (`templates.doraemon_v1`), and why:

* the figure is built in a *normalised design box* (100 high, `DESIGN_W` wide) and mapped to the canvas by ONE uniform
  scale (`FigureFrame`): it can not be stretched wider than tall when the canvas changes shape;
* the head is a rounded superellipse path with width/height 1.14 (version 1: an ellipse of 1.29), the whole figure is
  about 0.77 wide per unit of height (version 1: 0.98) and fills 87 % of the canvas height;
* round parts that are not ellipses are polygons sampled finely (head, face patch, torso, limbs, collar capsule);
  the smile and the pocket are cubic Beziers sampled at near-even spacing and drawn as ONE brush stroke each;
* every part has a stable semantic name (`ROLES`) and the layers come from an occlusion graph (`EDGES`), not from the
  order of a list: arms and feet are behind the torso, the head in front of its neck, the collar over head and
  torso, the bell over the collar, and all face details over the face patch;
* the vertical line from the nose ends ON the smile curve at its middle, not inside the mouth; exactly six whiskers;
* line widths come from one profile scaled with the canvas (`stroke_profile`), the outline of the silhouette is the
  thickest, left and right use the same width;
* `check_constraints` verifies the finished scene against the specification's ratios BEFORE Paint is touched.

Nothing here moves the mouse; the objects feed `geometry.plan_scene` and the usual executor.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from PIL import Image

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import curves, geometry, occlusion
from lga_worker.paint.model import Canvas, DrawingObject, Point

VERSION = "doraemon-v2.0"
DESIGN_W = 79.0
DESIGN_H = 100.0
AXIS = DESIGN_W / 2.0

# spec section 7: baseline colours; replaced only by colours measured on a trusted reference
BASELINE_PALETTE = {"body": "#0096E0", "belly": "#FFFFFF", "line": "#000000", "collar": "#E60012", "bell": "#FFD400"}

# spec section 4 ratios (constraints, not coordinates)
SILHOUETTE_ASPECT = (0.72, 0.86)
HEAD_ASPECT = (1.08, 1.20)
HEIGHT_FRACTION = (0.84, 0.90)
HEAD_SHARE = (0.44, 0.56)
WHISKERS = 6
MAX_WIDTH_FRACTION = 0.92  # portrait canvases: the figure is limited by width instead of height

MIN_VERTEX_GAP_PX = 6.0  # Paint reads polygon vertices closer than ~4 px as a double click
POLYGON_EDGE_PX = 15.0  # edge length of the sampled round outlines
SAMPLE_GAP_PX = 4.0  # spacing of the points of a brush stroke

STAGE_OF = {
    "foot_left": "A", "foot_right": "A", "arm_left": "A", "arm_right": "A", "torso": "A",
    "head_outer": "B", "face_patch": "B", "belly": "B", "hand_left": "B", "hand_right": "B", "pocket_outline": "B",
    "collar": "C", "bell": "C", "bell_groove": "C", "bell_dot": "C",
    "eye_left": "D", "eye_right": "D", "pupil_left": "D", "pupil_right": "D", "nose": "D", "nose_to_mouth": "D",
    "smile_curve": "D", "whisker_left_top": "D", "whisker_left_middle": "D", "whisker_left_bottom": "D",
    "whisker_right_top": "D", "whisker_right_middle": "D", "whisker_right_bottom": "D",
}
STAGE_FLOOR = {"A": 0, "B": 3, "C": 8, "D": 12}  # layer numbers an object may not drop below (spec section 6)
ROLES = tuple(STAGE_OF)

# (back, front): `front` is drawn after `back` and covers it where they meet
EDGES: tuple[occlusion.Edge, ...] = (
    ("foot_left", "torso"), ("foot_right", "torso"),
    ("arm_left", "torso"), ("arm_right", "torso"),                       # the body covers the root of each arm
    ("arm_left", "hand_left"), ("arm_right", "hand_right"), ("torso", "hand_left"), ("torso", "hand_right"),
    ("torso", "head_outer"), ("head_outer", "face_patch"), ("torso", "belly"), ("belly", "pocket_outline"),
    ("head_outer", "collar"), ("face_patch", "collar"), ("torso", "collar"), ("belly", "collar"),
    ("collar", "bell"), ("bell", "bell_groove"), ("bell", "bell_dot"),
    ("head_outer", "bell"), ("face_patch", "bell"),
    ("face_patch", "eye_left"), ("face_patch", "eye_right"),
    ("eye_left", "pupil_left"), ("eye_right", "pupil_right"),
    ("face_patch", "nose"), ("face_patch", "nose_to_mouth"), ("face_patch", "smile_curve"),
    ("smile_curve", "nose_to_mouth"),
    *(("face_patch", f"whisker_{side}_{row}") for side in ("left", "right") for row in ("top", "middle", "bottom")),
)
SILHOUETTE_PARTS = ("head_outer", "torso", "arm_left", "arm_right", "hand_left", "hand_right", "foot_left", "foot_right")
CLAMPS = {"head_aspect": HEAD_ASPECT, "height_fraction": HEIGHT_FRACTION, "hand_reach": (30.0, 36.5)}


@dataclass(frozen=True)
class Params:
    """The tunable numbers of the template (what learning from a screenshot may change, inside `CLAMPS`)."""

    head_aspect: float = 1.14
    height_fraction: float = 0.87
    hand_reach: float = 33.0  # distance of a hand centre from the axis, design units
    head_exponent: float = 2.3  # superellipse exponent of the head outline (2 = ellipse)
    body: str = BASELINE_PALETTE["body"]
    belly: str = BASELINE_PALETTE["belly"]
    line: str = BASELINE_PALETTE["line"]
    collar: str = BASELINE_PALETTE["collar"]
    bell: str = BASELINE_PALETTE["bell"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Params:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def clamped(self) -> Params:
        values = {k: min(max(getattr(self, k), lo), hi) for k, (lo, hi) in CLAMPS.items()}
        return replace(self, **values)

    def out_of_range(self) -> list[str]:
        return [k for k, (lo, hi) in CLAMPS.items() if not lo <= getattr(self, k) <= hi]


@dataclass(frozen=True)
class FigureFrame:
    """The ONE mapping from the design box to composition units: a uniform scale `k` and a position, centred."""

    k: float
    left: float
    top: float
    limited_by: str  # "height" or "width"

    @classmethod
    def fit(cls, canvas: Canvas, height_fraction: float) -> FigureFrame:
        by_height = height_fraction * canvas.unit_height
        by_width = MAX_WIDTH_FRACTION * 1000.0 * DESIGN_H / DESIGN_W
        height_units, limited = (by_height, "height") if by_height <= by_width else (by_width, "width")
        k = height_units / DESIGN_H
        return cls(k, (1000.0 - DESIGN_W * k) / 2.0, (canvas.unit_height - DESIGN_H * k) / 2.0, limited)

    def pt(self, x: float, y: float) -> Point:
        return self.left + x * self.k, self.top + y * self.k

    def box(self, x0: float, y0: float, x1: float, y1: float) -> tuple[Point, Point]:
        return self.pt(x0, y0), self.pt(x1, y1)

    def inverse(self, ux: float, uy: float) -> Point:
        return (ux - self.left) / self.k, (uy - self.top) / self.k


@dataclass(frozen=True)
class StrokeProfile:
    """Line widths in canvas px, one set for the whole figure, scaled with the canvas height (spec section 7)."""

    silhouette: int  # outline of head, torso, limbs, feet: the thickest group (a shape width Paint offers)
    region: int  # outline of face, belly, hands, collar, bell, eyes, nose (a shape width Paint offers)
    line: int  # brush strokes: smile, nose line, whiskers, pocket

    @classmethod
    def for_canvas(cls, canvas: Canvas) -> StrokeProfile:
        s = canvas.height / 648.0
        silhouette = geometry.snap_width(5.0 * s)
        region = min(geometry.snap_width(3.0 * s), silhouette)
        line = max(2, min(14, round(4.0 * s)))
        return cls(silhouette, region, line)


@dataclass
class Scene:
    objects: list[DrawingObject]
    params: Params
    frame: FigureFrame
    canvas: Canvas
    profile: StrokeProfile
    version: str = VERSION
    layers: dict[str, int] = field(default_factory=dict)

    def by_id(self) -> dict[str, DrawingObject]:
        return {o.id: o for o in self.objects}

    def landmarks(self) -> dict[str, Point]:
        """Landmarks in the design box divided by (DESIGN_W, DESIGN_H): the normalised coordinates of the figure."""
        out: dict[str, Point] = {}
        by = self.by_id()
        for name in ("nose", "bell", "eye_left", "eye_right", "pupil_left", "pupil_right", "bell_dot"):
            cx, cy = _box_centre(by[name].points)
            x, y = self.frame.inverse(cx, cy)
            out[name] = (round(x / DESIGN_W, 4), round(y / DESIGN_H, 4))
        for name in ("collar", "face_patch", "head_outer", "torso", "belly"):
            x, y = self.frame.inverse(*_centroid(by[name].points))
            out[name] = (round(x / DESIGN_W, 4), round(y / DESIGN_H, 4))
        return out


def _box_centre(points: Sequence[Point]) -> Point:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def _centroid(points: Sequence[Point]) -> Point:
    return _box_centre(points)


def _bounds(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------------------------------- building


def build_scene(canvas: Canvas, params: Params | None = None) -> Scene:
    """The 28 objects of the figure in composition units, layered by the occlusion graph."""
    p = params or Params()
    bad = p.out_of_range()
    if bad:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        "template parameters outside the allowed ratios: "
                        + ", ".join(f"{k}={getattr(p, k)} not in {CLAMPS[k]}" for k in bad))
    f = FigureFrame.fit(canvas, p.height_fraction)
    prof = StrokeProfile.for_canvas(canvas)
    px = canvas.px_per_unit
    gap_u = MIN_VERTEX_GAP_PX / px
    sample_u = SAMPLE_GAP_PX / px
    raw: dict[str, DrawingObject] = {}

    def poly_points(design_pts: Sequence[Point]) -> tuple[Point, ...]:
        mapped = [f.pt(x, y) for x, y in design_pts]
        return tuple(curves.simplify(mapped, min_gap=gap_u, flat_deg=0.4, closed=True))

    def shape(oid: str, kind: str, pts, *, fill: str | None, width: int) -> None:
        raw[oid] = DrawingObject(oid, kind, tuple(pts), fill=fill, stroke=p.line, layer=0, width_px=width)

    def stroke(oid: str, pts: Sequence[Point]) -> None:
        raw[oid] = DrawingObject(oid, "path", tuple(pts), fill=None, stroke=p.line, layer=0, width_px=prof.line)

    def per_cap(radius_design: float) -> int:
        return max(4, round(math.pi * radius_design * f.k * px / 8.0))

    def dense(a: Point, b: Point) -> list[Point]:
        n = max(1, math.ceil(math.dist(a, b) / sample_u))
        return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]

    mirror = lambda x: DESIGN_W - x  # noqa: E731 - the figure is symmetric about the axis

    head_h = 50.0
    head_w = head_h * p.head_aspect
    def count_for(rx: float, ry: float) -> int:
        """Vertices for a smooth outline whose edges are about POLYGON_EDGE_PX long: Paint drops a polygon whose first
        dragged edge is under ~12 px, so the spacing must stay well above that (the curve error stays under 0.3 px)."""
        perimeter = math.pi * (3 * (rx + ry) - math.sqrt((3 * rx + ry) * (rx + 3 * ry)))
        return max(24, round(perimeter * f.k * px / POLYGON_EDGE_PX))

    n_head = count_for(head_w / 2.0, head_h / 2.0)
    head = curves.superellipse(AXIS, head_h / 2.0, head_w / 2.0, head_h / 2.0, exponent=p.head_exponent, count=n_head)
    shape("head_outer", "polygon", poly_points(head), fill=p.body, width=prof.silhouette)

    face_rx = head_w * 0.43
    face = curves.superellipse(AXIS, 31.5, face_rx, 17.5, exponent=2.2, count=count_for(face_rx, 17.5))
    shape("face_patch", "polygon", poly_points(face), fill=p.belly, width=prof.region)

    torso = curves.superellipse(AXIS, 68.0, 19.5, 21.0, exponent=2.4, count=count_for(19.5, 21.0))
    shape("torso", "polygon", poly_points(torso), fill=p.body, width=prof.silhouette)

    for side, sign in (("left", -1.0), ("right", 1.0)):
        shoulder = (AXIS + sign * 15.5, 57.0)
        hand_c = (AXIS + sign * p.hand_reach, 73.5)
        arm = curves.rotated_capsule(shoulder, hand_c, 9.5, per_cap=per_cap(4.75))
        shape(f"arm_{side}", "polygon", poly_points(arm), fill=p.body, width=prof.silhouette)
        r = 5.5
        shape(f"hand_{side}", "ellipse", f.box(hand_c[0] - r, hand_c[1] - r, hand_c[0] + r, hand_c[1] + r),
              fill=p.belly, width=prof.region)

    shape("foot_left", "ellipse", f.box(16.5, 88.0, 38.5, 100.0), fill=p.belly, width=prof.silhouette)
    shape("foot_right", "ellipse", f.box(mirror(38.5), 88.0, mirror(16.5), 100.0), fill=p.belly, width=prof.silhouette)

    belly = curves.superellipse(AXIS, 70.5, 13.5, 15.5, exponent=2.0, count=count_for(13.5, 15.5))
    shape("belly", "polygon", poly_points(belly), fill=p.belly, width=prof.region)

    # the pocket: a flat mouth line and a soft U, one closed brush stroke without fill (the belly colour shows through)
    top_y, left_x, right_x = 64.5, 29.5, 49.5
    u_seg = (f.pt(left_x, top_y), f.pt(left_x - 0.4, 81.0), f.pt(right_x + 0.4, 81.0), f.pt(right_x, top_y))
    pocket = curves.sample_bezier(*u_seg, sample_u)
    pocket += dense(f.pt(right_x, top_y), f.pt(left_x, top_y))[1:]
    stroke("pocket_outline", pocket)

    collar = curves.capsule(AXIS - 20.0, 47.5, AXIS + 20.0, 55.0, per_cap=per_cap(3.75))
    shape("collar", "polygon", poly_points(collar), fill=p.collar, width=prof.region)

    bell_c, bell_r = (AXIS, 59.0), 4.6
    shape("bell", "ellipse", f.box(bell_c[0] - bell_r, bell_c[1] - bell_r, bell_c[0] + bell_r, bell_c[1] + bell_r),
          fill=p.bell, width=prof.region)
    raw["bell_groove"] = DrawingObject("bell_groove", "line", (f.pt(AXIS - 4.3, 58.2), f.pt(AXIS + 4.3, 58.2)),
                                       fill=None, stroke=p.line, layer=0, width_px=prof.region)
    shape("bell_dot", "ellipse", f.box(AXIS - 1.1, 59.6, AXIS + 1.1, 61.8), fill=p.line, width=prof.region)

    eye_y0, eye_y1 = 9.0, 28.0
    shape("eye_left", "ellipse", f.box(AXIS - 13.5, eye_y0, AXIS, eye_y1), fill=p.belly, width=prof.region)
    shape("eye_right", "ellipse", f.box(AXIS, eye_y0, AXIS + 13.5, eye_y1), fill=p.belly, width=prof.region)
    for side, sign in (("left", -1.0), ("right", 1.0)):
        cx, cy = AXIS + sign * 3.2, 19.5
        shape(f"pupil_{side}", "ellipse", f.box(cx - 1.9, cy - 2.6, cx + 1.9, cy + 2.6), fill=p.line, width=prof.region)

    nose_c, nose_r = (AXIS, 32.2), 3.2
    shape("nose", "ellipse", f.box(nose_c[0] - nose_r, nose_c[1] - nose_r, nose_c[0] + nose_r, nose_c[1] + nose_r),
          fill=p.collar, width=prof.region)

    smile_seg = (f.pt(21.5, 40.0), f.pt(27.5, 47.3), f.pt(51.5, 47.3), f.pt(57.5, 40.0))
    stroke("smile_curve", curves.sample_bezier(*smile_seg, sample_u))
    mid = curves.bezier_midpoint(*smile_seg)
    nose_bottom = f.pt(nose_c[0], nose_c[1] + nose_r)
    stroke("nose_to_mouth", dense(nose_bottom, mid))

    reach = head_w / 2.0  # the whisker ends follow the head width: 1.14 -> 28.5, a narrower head pulls them in
    whiskers = {"top": ((28.5, 29.5), (AXIS - 0.895 * reach, 24.0)), "middle": ((28.5, 33.0), (AXIS - 0.93 * reach, 32.5)),
                "bottom": ((28.5, 36.5), (AXIS - 0.86 * reach, 38.5))}
    for row, (start, end) in whiskers.items():
        stroke(f"whisker_left_{row}", dense(f.pt(*start), f.pt(*end)))
        stroke(f"whisker_right_{row}", dense(f.pt(mirror(start[0]), start[1]), f.pt(mirror(end[0]), end[1])))

    order = list(STAGE_OF)  # the order objects are listed in: stage A..D
    layers = occlusion.assign_layers(order, EDGES, floor={o: STAGE_FLOOR[STAGE_OF[o]] for o in order})
    objects = [replace(raw[oid], layer=layers[oid]) for oid in order]
    return Scene(objects=objects, params=p, frame=f, canvas=canvas, profile=prof, layers=layers)


# ------------------------------------------------------------------------------------------------ constraints


@dataclass
class Constraint:
    name: str
    ok: bool
    value: Any
    expected: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "value": self.value, "expected": self.expected, "note": self.note}


def _all_points(obj: DrawingObject) -> list[Point]:
    """The outline of a closed shape (ellipses sampled), or the points of an open one, in composition units."""
    if obj.kind == "ellipse":
        (xa, ya), (xb, yb) = obj.points[0], obj.points[1]
        return [(x, y) for x, y in geometry.ellipse_points((min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)))]
    if obj.kind == "rect":
        (xa, ya), (xb, yb) = obj.points[0], obj.points[1]
        return [(xa, ya), (xb, ya), (xb, yb), (xa, yb)]
    return list(obj.points)


def check_constraints(scene: Scene) -> list[Constraint]:
    """Specification section 4 / 5 / 6 / 7 as checks on the finished geometry (composition units)."""
    by = scene.by_id()
    canvas = scene.canvas
    out: list[Constraint] = []

    def add(name: str, ok: bool, value: Any, expected: str, note: str = "") -> None:
        out.append(Constraint(name, bool(ok), value, expected, note))

    missing = [r for r in ROLES if r not in by]
    add("named_objects", not missing, len(by), f"all {len(ROLES)} named objects", f"missing {missing}" if missing else "")
    if missing:
        return out

    sil = [pt for name in SILHOUETTE_PARTS for pt in _all_points(by[name])]
    x0, y0, x1, y1 = _bounds(sil)
    fw, fh = x1 - x0, y1 - y0
    aspect = fw / fh
    add("silhouette_aspect", SILHOUETTE_ASPECT[0] <= aspect <= SILHOUETTE_ASPECT[1], round(aspect, 3),
        f"{SILHOUETTE_ASPECT[0]}..{SILHOUETTE_ASPECT[1]} (width / height)")
    frac = fh / canvas.unit_height
    if scene.frame.limited_by == "height":
        add("figure_height_fraction", HEIGHT_FRACTION[0] - 0.005 <= frac <= HEIGHT_FRACTION[1] + 0.005, round(frac, 3),
            f"{HEIGHT_FRACTION[0]}..{HEIGHT_FRACTION[1]} of the canvas height")
    else:
        add("figure_height_fraction", fw / 1000.0 <= MAX_WIDTH_FRACTION + 0.005, round(fw / 1000.0, 3),
            f"<= {MAX_WIDTH_FRACTION} of the canvas width", "narrow canvas: limited by width")
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    off = max(abs(cx - 500.0) / 1000.0, abs(cy - canvas.unit_height / 2.0) / canvas.unit_height)
    add("centred", off <= 0.02, round(off, 4), "<= 2 % of the canvas size off centre")

    hx0, hy0, hx1, hy1 = _bounds(by["head_outer"].points)
    head_aspect = (hx1 - hx0) / (hy1 - hy0)
    add("head_aspect", HEAD_ASPECT[0] <= head_aspect <= HEAD_ASPECT[1], round(head_aspect, 3),
        f"{HEAD_ASPECT[0]}..{HEAD_ASPECT[1]} (width / height)")
    share = (hy1 - hy0) / fh
    add("head_share_of_height", HEAD_SHARE[0] <= share <= HEAD_SHARE[1], round(share, 3),
        f"{HEAD_SHARE[0]}..{HEAD_SHARE[1]} of the figure height")

    head_poly = list(by["head_outer"].points)
    face_poly = list(by["face_patch"].points)
    face_margin = min(curves.distance_to_polygon_edge(head_poly, p) for p in face_poly
                      if curves.polygon_contains(head_poly, p)) if all(curves.polygon_contains(head_poly, p) for p in face_poly) else -1.0
    add("face_inside_head", face_margin > 0, round(face_margin, 2), "every face point inside the head outline")

    axis_x = 500.0
    axis = f_axis = scene.frame.pt(AXIS, 0.0)[0]
    add("axis_is_figure_centre", abs(axis - cx) <= 0.01 * 1000.0, round(axis - cx, 2), "figure centred on its axis")

    el, er = _bounds(_all_points(by["eye_left"])), _bounds(_all_points(by["eye_right"]))
    sym = abs((el[0] + er[2]) / 2.0 - f_axis)
    add("eyes_symmetric", sym <= 0.3 and abs(el[1] - er[1]) <= 0.3 and abs((el[2] - el[0]) - (er[2] - er[0])) <= 0.3,
        round(sym, 3), "eyes mirror each other about the face axis, same height and size")
    gap = er[0] - el[2]
    add("eyes_touch", abs(gap) <= 1.5 * scene.frame.k, round(gap, 2), "eyes nearly touch in the middle (gap <= 1.5 design units)")
    pl, pr = _bounds(_all_points(by["pupil_left"])), _bounds(_all_points(by["pupil_right"]))
    inside = all(el[0] <= pl[0] and pl[2] <= el[2] and el[1] <= pl[1] and pl[3] <= el[3] for _ in (0,)) and \
        all(er[0] <= pr[0] and pr[2] <= er[2] and er[1] <= pr[1] and pr[3] <= er[3] for _ in (0,))
    small = (pl[2] - pl[0]) * (pl[3] - pl[1]) < 0.3 * (el[2] - el[0]) * (el[3] - el[1])
    inward = (pl[0] + pl[2]) / 2.0 > (el[0] + el[2]) / 2.0 and (pr[0] + pr[2]) / 2.0 < (er[0] + er[2]) / 2.0
    add("pupils", inside and small and inward, {"inside": inside, "small": small, "towards_centre": inward},
        "pupils inside the eyes, smaller than them, looking slightly towards the centre")

    nb = _bounds(_all_points(by["nose"]))
    nose_x = (nb[0] + nb[2]) / 2.0
    add("nose_on_axis", abs(nose_x - f_axis) <= 0.3 and nb[1] >= el[3] - 1.0, round(nose_x - f_axis, 3),
        "nose centred on the axis, below the eyes")

    smile = list(by["smile_curve"].points)
    s_first, s_last = smile[0], smile[-1]
    s_mid_x = (s_first[0] + s_last[0]) / 2.0
    add("smile_symmetric", abs(s_mid_x - f_axis) <= 0.3 and abs(s_first[1] - s_last[1]) <= 0.3, round(s_mid_x - f_axis, 3),
        "one U curve, both ends at the same height, centred")
    lowest = max(smile, key=lambda q: q[1])
    line = list(by["nose_to_mouth"].points)
    end = line[-1]
    on_curve = curves.distance_to_polyline(smile, end)
    nose_bottom = (nose_x, nb[3])
    add("nose_line_ends_on_smile", on_curve <= 0.6 and abs(end[0] - f_axis) <= 0.3 and end[1] <= lowest[1] + 0.3 and
        abs(line[0][0] - nose_bottom[0]) <= 0.3 and abs(line[0][1] - nose_bottom[1]) <= 0.6,
        {"gap_to_curve": round(on_curve, 3), "end_y": round(end[1], 2), "lowest_curve_y": round(lowest[1], 2)},
        "the line from the nose ends ON the smile curve at its middle, never inside the mouth")
    spacing = [math.dist(a, b) for a, b in zip(smile, smile[1:], strict=False)]
    add("smile_is_smooth", max(spacing) <= 1.6 * (SAMPLE_GAP_PX / canvas.px_per_unit), round(max(spacing), 2),
        "curve sampled at near-even small spacing (no long chords)")

    whisker_ids = [k for k in by if k.startswith("whisker_")]
    inside_head = []
    for wid in whisker_ids:
        for q in (by[wid].points[0], by[wid].points[-1]):
            inside_head.append(curves.polygon_contains(head_poly, q) and curves.distance_to_polygon_edge(head_poly, q) >= 0.5 * scene.frame.k)
    add("six_whiskers", len(whisker_ids) == WHISKERS and sum(1 for k in whisker_ids if "_left_" in k) == 3,
        len(whisker_ids), "exactly 3 whiskers on each side (6 in all)")
    add("whiskers_inside_head", all(inside_head), sum(inside_head), "every whisker ends inside the head outline")
    mirrored = all(abs(math.dist(by[f"whisker_left_{r}"].points[0], by[f"whisker_left_{r}"].points[-1])
                       - math.dist(by[f"whisker_right_{r}"].points[0], by[f"whisker_right_{r}"].points[-1])) <= 0.2
                   for r in ("top", "middle", "bottom"))
    add("whiskers_mirrored", mirrored, mirrored, "left and right whiskers have the same length")

    col = by["collar"]
    cb = _bounds(col.points)
    corners = [(cb[0], cb[1]), (cb[2], cb[1]), (cb[0], cb[3]), (cb[2], cb[3])]
    cut = min(math.dist(c, q) for c in corners for q in col.points)
    cap_r = (cb[3] - cb[1]) / 2.0
    add("collar_is_capsule", len(col.points) >= 12 and cut >= 0.3 * cap_r, {"vertices": len(col.points), "corner_cut": round(cut, 2)},
        "rounded ends (capsule), not a rectangle")
    belb = _bounds(_all_points(by["bell"]))
    bell_x = (belb[0] + belb[2]) / 2.0
    add("bell_on_collar", abs(bell_x - f_axis) <= 0.3 and belb[1] < cb[3] < belb[3], round(bell_x - f_axis, 3),
        "bell centred on the collar, overlapping its lower edge")
    groove = by["bell_groove"].points
    dot = _bounds(_all_points(by["bell_dot"]))
    add("bell_details", belb[0] <= groove[0][0] and groove[1][0] <= belb[2] and belb[1] < groove[0][1] < belb[3] and
        belb[1] < dot[1] and dot[3] < belb[3], True, "a horizontal groove and a small black dot inside the bell")

    pocket = list(by["pocket_outline"].points)
    pb = _bounds(pocket)
    belly_poly = list(by["belly"].points)
    add("pocket_soft_u", len(pocket) >= 20 and all(curves.polygon_contains(belly_poly, q) for q in pocket),
        len(pocket), "a flat mouth line and a smooth U base, all inside the belly, no fill",
        f"box {round(pb[2] - pb[0], 1)} x {round(pb[3] - pb[1], 1)}")

    layer = {o.id: o.layer for o in scene.objects}
    pos = {o.id: i for i, o in enumerate(scene.objects)}
    problems = occlusion.order_problems(layer, EDGES, positions=pos)
    add("layer_order", not problems, len(problems), "every occlusion relation respected", "; ".join(problems[:3]))
    add("occlusion_acyclic", occlusion.find_cycle(EDGES) is None, True, "no cycle in the occlusion graph")

    prof = scene.profile
    widths = {o.id: o.width_px for o in scene.objects}
    sym_w = all(widths[a] == widths[b] for a, b in (("arm_left", "arm_right"), ("hand_left", "hand_right"),
                                                      ("eye_left", "eye_right"), ("foot_left", "foot_right"),
                                                      ("pupil_left", "pupil_right"))) and \
        all(widths[f"whisker_left_{r}"] == widths[f"whisker_right_{r}"] for r in ("top", "middle", "bottom"))
    add("stroke_profile", prof.silhouette >= prof.region and widths["head_outer"] == prof.silhouette and
        widths["face_patch"] == prof.region and widths["smile_curve"] == prof.line and sym_w,
        {"silhouette": prof.silhouette, "region": prof.region, "line": prof.line},
        "silhouette outline thickest; both sides equal; widths from one profile")
    colours = {o.fill for o in scene.objects if o.fill} | {o.stroke for o in scene.objects if o.stroke}
    allowed = {scene.params.body, scene.params.belly, scene.params.line, scene.params.collar, scene.params.bell}
    add("palette", colours <= allowed, sorted(colours), "only the five palette colours")
    return out


def failing(constraints: Sequence[Constraint]) -> list[Constraint]:
    return [c for c in constraints if not c.ok]


# ---------------------------------------------------------------------------------------- preparing a drawing


@dataclass
class Prepared:
    scene: Scene
    constraints: list[Constraint]
    corrections: list[str]
    reference: dict[str, Any]


def prepare(canvas: Canvas, params: Params | None = None, *, options: Mapping[str, str] | None = None,
            reference: Mapping[str, Any] | None = None, max_rounds: int = 3) -> Prepared:
    """Build the scene, check the ratios, and correct the parameters that break them BEFORE Paint is touched.

    `options` carries colour overrides (body, belly, collar, bell). `reference` is the result of `measure_reference`
    (or None: OFFLINE_TEMPLATE). Raises `ToolError` when the constraints still fail after `max_rounds`.
    """
    p = params or Params()
    colours = {k: v for k, v in (options or {}).items() if k in ("body", "belly", "collar", "bell", "line")}
    if colours:
        p = replace(p, **{k: _hex(v) for k, v in colours.items()})
    info: dict[str, Any] = {"status": "OFFLINE_TEMPLATE", "source": f"local template {VERSION}",
                            "measured": [], "retrieved_at": None, "url": None}
    if reference:
        info.update({k: reference[k] for k in ("status", "source", "retrieved_at", "url", "measured") if k in reference})
        p = apply_reference(p, reference)
    p = p.clamped()
    corrections: list[str] = []
    for _ in range(max_rounds):
        scene = build_scene(canvas, p)
        constraints = check_constraints(scene)
        bad = failing(constraints)
        if not bad:
            return Prepared(scene, constraints, corrections, info)
        fixed = _correct(p, bad, corrections)
        if fixed == p:
            break
        p = fixed
    scene = build_scene(canvas, p)
    constraints = check_constraints(scene)
    bad = failing(constraints)
    if bad:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        "the Doraemon scene breaks its own proportions and was not drawn: "
                        + "; ".join(f"{c.name} ({c.value}, expected {c.expected})" for c in bad[:4]),
                        retryable=False, details={"constraints": [c.to_dict() for c in constraints]})
    return Prepared(scene, constraints, corrections, info)


def _hex(value: str) -> str:
    rgb = geometry.parse_color(value)
    return "#%02X%02X%02X" % rgb


def _correct(p: Params, bad: Sequence[Constraint], notes: list[str]) -> Params:
    names = {c.name for c in bad}
    new = p
    if "silhouette_aspect" in names:
        value = next(c.value for c in bad if c.name == "silhouette_aspect")
        step = -1.0 if value > SILHOUETTE_ASPECT[1] else 1.0
        new = replace(new, hand_reach=new.hand_reach + step)
        notes.append(f"hand_reach {p.hand_reach} -> {new.hand_reach} (silhouette aspect {value})")
    if "head_aspect" in names:
        value = next(c.value for c in bad if c.name == "head_aspect")
        target = HEAD_ASPECT[1] - 0.02 if value > HEAD_ASPECT[1] else HEAD_ASPECT[0] + 0.02
        new = replace(new, head_aspect=target)
        notes.append(f"head_aspect {p.head_aspect} -> {target} (measured {value})")
    if "figure_height_fraction" in names:
        new = replace(new, height_fraction=0.87)
        notes.append("height_fraction -> 0.87")
    return new.clamped()


# ----------------------------------------------------------------------------------------------- reference


def _dominant(arr, mask):  # noqa: ANN001 - numpy arrays, imported lazily
    import numpy as np
    count = int(mask.sum())
    if count < 200:
        return None
    return tuple(int(v) for v in np.median(arr[mask], axis=0)), count


def measure_reference(image: Image.Image, *, url: str | None = None, retrieved_at: str | None = None,
                      source: str | None = None) -> dict[str, Any]:
    """What can be measured on a front-view reference picture: the five palette colours, the silhouette aspect and the
    head aspect (from the blue mask). Every value is clamped to the allowed ratios by `apply_reference`; nothing here is
    called a match: the result only says which values came from the picture."""
    import numpy as np

    arr = np.asarray(image.convert("RGB")).astype(int)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    non_white = (arr.min(axis=2) < 235)
    measured: list[str] = []
    out: dict[str, Any] = {"status": "AVAILABLE", "source": source or url or "reference image", "url": url,
                           "retrieved_at": retrieved_at, "measured": measured, "image_size": list(image.size)}
    if not non_white.any():
        out.update(status="UNUSABLE", note="the reference picture is blank")
        return out
    ys, xs = np.where(non_white)
    bw, bh = int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
    out["silhouette_aspect"] = round(bw / bh, 3)
    measured.append("silhouette_aspect")
    blue = (b > r + 70) & (b > g + 10) & (b > 140)
    red = (r > 170) & (g < 70) & (b < 80)
    yellow = (r > 210) & (g > 170) & (b < 90)
    for key, mask in (("body", blue), ("collar", red), ("bell", yellow)):
        found = _dominant(arr, mask)
        if found:
            out[key] = "#%02X%02X%02X" % found[0]
            measured.append(key)
    if blue.any():
        top = int(ys.min())
        head_rows = blue[top: top + int(bh * 0.52)]
        hy, hx = np.where(head_rows)
        if len(hx) > 500:
            out["head_aspect"] = round(float((hx.max() - hx.min() + 1) / (hy.max() - hy.min() + 1)), 3)
            measured.append("head_aspect")
    out["confidence"] = round(0.35 + 0.1 * len(measured), 2)
    return out


def apply_reference(p: Params, ref: Mapping[str, Any]) -> Params:
    """Measured values replace the baseline ones, always inside the allowed ratios."""
    new = p
    for key in ("body", "collar", "bell"):
        if ref.get(key):
            new = replace(new, **{key: ref[key]})
    if ref.get("head_aspect"):
        lo, hi = HEAD_ASPECT
        new = replace(new, head_aspect=min(max(float(ref["head_aspect"]), lo), hi))
    if ref.get("silhouette_aspect"):
        lo, hi = SILHOUETTE_ASPECT
        aspect = min(max(float(ref["silhouette_aspect"]), lo + 0.01), hi - 0.01)
        new = replace(new, hand_reach=(aspect * DESIGN_H - 11.0) / 2.0)
    return new.clamped()


# ------------------------------------------------------------------------------------------ what a drawing records


def scene_info(prepared: Prepared, *, reviewed: bool = False) -> dict[str, Any]:
    """Everything `quality.character_fidelity` and the specification need to know about a template drawing."""
    scene = prepared.scene
    canvas = scene.canvas
    by = scene.by_id()
    px = canvas.px_per_unit

    def centre_px(name: str) -> tuple[float, float]:
        cx, cy = _box_centre(_all_points(by[name]))
        return cx * px, cy * px

    def rgb(value: str) -> tuple[int, int, int]:
        return geometry.parse_color(value)

    p = scene.params
    return {
        "template": "doraemon", "template_version": scene.version, "params": p.to_dict(), "reviewed": reviewed,
        "silhouette_aspect": SILHOUETTE_ASPECT, "height_fraction": HEIGHT_FRACTION, "limited_by": scene.frame.limited_by,
        "constraints": [c.to_dict() for c in prepared.constraints], "corrections": list(prepared.corrections),
        "reference": dict(prepared.reference), "landmarks": scene.landmarks(),
        "landmarks_px": {n: centre_px(n) for n in ("nose", "bell", "pupil_left", "pupil_right")},
        "landmark_colours": {"nose": {"rgb": rgb(p.collar), "window": 12}, "bell": {"rgb": rgb(p.bell), "window": 16},
                             "pupil_left": {"rgb": rgb(p.line), "window": 8, "tolerance": 60},
                             "pupil_right": {"rgb": rgb(p.line), "window": 8, "tolerance": 60}},
        "stroke_profile": asdict(scene.profile),
        "semantic_map": {oid: STAGE_OF[oid] for oid in ROLES},
        "colour_sources": {k: ("measured from the reference" if k in prepared.reference.get("measured", []) else
                               "baseline of the specification, not measured") for k in ("body", "belly", "collar", "bell", "line")},
        "frame": {"k": round(scene.frame.k, 4), "left": round(scene.frame.left, 2), "top": round(scene.frame.top, 2)},
    }
